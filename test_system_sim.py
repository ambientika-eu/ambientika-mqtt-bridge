#!/usr/bin/env python3
"""System test: the bridge as a real process (or container) between a local
stand-in for the Ambientika cloud (sim/fake_cloud.py, real HTTP) and a real
Mosquitto broker.

It covers what the in-process tests cannot: configuration from the environment
and from config.yaml / options.json, the HTTP client, signals, a start without
cloud or broker (the bridge waits and tries again), a password the cloud does
not accept, cloud outages, requests the cloud never answers, a broker restart
and the Last Will.

    python test_system_sim.py                      # bridge.py of this folder
    python test_system_sim.py --image TAG          # standalone image (docker, host network)
    python test_system_sim.py --addon-image TAG    # add-on image: /data/options.json + run.sh
    python test_system_sim.py --only hang,broker   # some groups only

--addon-config names the config.yaml whose default options the add-on image
gets (another repository's copy); SIM_TIME_FACTOR stretches every wait for an
image that runs emulated.

Needs mosquitto on the PATH (or its path in SIM_MOSQUITTO); the image modes
need docker. Where the system confines /usr/sbin/mosquitto to its own folders
(AppArmor on Ubuntu), point SIM_MOSQUITTO at a copy of the binary.
"""
import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import paho.mqtt.client as mqtt

HERE = os.path.dirname(os.path.abspath(__file__))
USER, PASSWORD = "kunde@example.com", "geheim#1 äöü"
MASTER, SLAVE, SLAVE2, SLAVE3, MASTER2 = ("A0B1C2D3E402", "A0B1C2D3E401", "A0B1C2D3E403",
                                          "A0B1C2D3E405", "A0B1C2D3E404")
SERIALS = [MASTER, SLAVE, SLAVE2, SLAVE3, MASTER2]
NAMES = {MASTER: "Flur", SLAVE: "Wohnzimmer", SLAVE2: "Kinderzimmer", SLAVE3: "Gäste-Bad",
         MASTER2: "Schlafzimmer Eltern"}
MOSQUITTO = os.environ.get("SIM_MOSQUITTO", "mosquitto")    # another path, e.g. an unconfined copy
POLL = 2            # seconds between polls in every scenario
CLOUD_TIMEOUT = 4   # seconds the bridge waits for one answer of the cloud here
START_RETRY = 2     # seconds until the start is tried again here (doubles; 10 s in real life)
SIGN_IN_RETRY = 3   # seconds until a refused sign-in is tried again here (doubles; 60 s in real life)
# Stretches every wait, for an image that runs emulated (e.g. arm on an x86 runner).
TIME_FACTOR = max(1.0, float(os.environ.get("SIM_TIME_FACTOR", "1") or 1))

ap = argparse.ArgumentParser()
ap.add_argument("--image")
ap.add_argument("--addon-image")
ap.add_argument("--addon-config", help="config.yaml of the add-on (default: ambientika_mqtt_bridge/config.yaml here)")
ap.add_argument("--only", default="")
ap.add_argument("--keep", action="store_true", help="keep the work folder with all logs")
ARGS = ap.parse_args()
ONLY = {x.strip() for x in ARGS.only.split(",") if x.strip()}
MODE = "addon" if ARGS.addon_image else "image" if ARGS.image else "process"

WORK = tempfile.mkdtemp(prefix="ambientika-sim-")
os.chmod(WORK, 0o755)     # mosquitto drops its privileges and still has to read its files here
FAILS, PASSES = [], 0


def check(name, cond, extra=""):
    global PASSES
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else "  <<< " + str(extra)[:600]), flush=True)
    if cond:
        PASSES += 1
    else:
        FAILS.append(name)
    return bool(cond)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait(cond, timeout=20, step=0.1):
    end = time.time() + timeout * TIME_FACTOR
    while time.time() < end:
        try:
            v = cond()
        except Exception:
            v = None
        if v:
            return v
        time.sleep(step)
    return None


class Proc:
    """A child process whose output goes to a log file."""

    def __init__(self, name, cmd, env=None, cwd=None):
        self.name = name
        self.path = os.path.join(WORK, "%s-%d.log" % (name, int(time.time() * 1000) % 10 ** 9))
        self.fh = open(self.path, "wb")
        full = dict(os.environ)
        full.update(env or {})
        self.p = subprocess.Popen(cmd, env=full, cwd=cwd, stdout=self.fh, stderr=subprocess.STDOUT)

    def log(self):
        self.fh.flush()
        with open(self.path, encoding="utf-8", errors="replace") as fh:
            return fh.read()

    def running(self):
        return self.p.poll() is None

    def stop(self, sig=signal.SIGTERM, timeout=15):
        if self.running():
            self.p.send_signal(sig)
            try:
                self.p.wait(timeout)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait(5)
        return self.p.returncode


class Broker:
    def __init__(self, port=None, user=None, password=None):
        self.port = port or free_port()
        self.user, self.password = user, password
        self.conf = os.path.join(WORK, "mosquitto-%d.conf" % self.port)
        lines = ["listener %d 127.0.0.1" % self.port, "persistence false"]
        if user:
            pw = os.path.join(WORK, "passwd-%d" % self.port)
            subprocess.run(["mosquitto_passwd", "-b", "-c", pw, user, password], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            os.chmod(pw, 0o644)
            lines += ["allow_anonymous false", "password_file " + pw]
        else:
            lines += ["allow_anonymous true"]
        with open(self.conf, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        self.proc = None
        self.start()

    def start(self):
        self.proc = Proc("mosquitto", [MOSQUITTO, "-c", self.conf])
        ok = wait(lambda: self._open(), 10)
        if not ok:
            raise RuntimeError("mosquitto did not start: " + self.proc.log())

    def _open(self):
        try:
            socket.create_connection(("127.0.0.1", self.port), 0.3).close()
            return True
        except OSError:
            return False

    def stop(self):
        if self.proc:
            self.proc.stop(signal.SIGKILL)


class Cloud:
    def __init__(self, port=None):
        self.port = port or free_port()
        self.url = "http://127.0.0.1:%d" % self.port
        self.proc = Proc("cloud", [sys.executable, os.path.join(HERE, "sim", "fake_cloud.py"), str(self.port)])
        if not wait(lambda: self.state(), 15):
            raise RuntimeError("fake cloud did not start: " + self.proc.log())

    def _req(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(self.url + path, data=data, method=method,
                                   headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(r, timeout=5) as resp:
            return json.loads(resp.read().decode() or "null")

    def state(self):
        return self._req("GET", "/_ctl/state")

    def log(self, kind=None):
        rows = self._req("GET", "/_ctl/log")
        return [r for r in rows if kind is None or r["kind"] == kind]

    def clear(self):
        self._req("POST", "/_ctl/clear", {})

    def unit(self, serial, set=None, fault="-"):
        body = {"serial": serial}
        if set is not None:
            body["set"] = set
        if fault != "-":
            body["fault"] = fault
        self._req("POST", "/_ctl/unit", body)

    def outage(self, mode):
        self._req("POST", "/_ctl/outage", {"mode": mode})

    def expire(self):
        self._req("POST", "/_ctl/expire", {})

    def reset(self):
        self._req("POST", "/_ctl/reset", {})

    def stop(self):
        self.proc.stop(signal.SIGKILL)


class Spy:
    """Plays Home Assistant: records every message with its arrival time."""

    def __init__(self, port, user=None, password=None):
        self.msgs, self.lock = [], threading.Lock()
        self.c = mqtt.Client(client_id="spy-%d" % free_port())
        if user:
            self.c.username_pw_set(user, password)
        self.c.on_message = self._m
        self.c.on_connect = lambda c, u, f, rc: c.subscribe("#")
        self.c.connect("127.0.0.1", port)
        self.c.loop_start()

    def _m(self, c, u, m):
        with self.lock:
            self.msgs.append((time.time(), m.topic, m.payload.decode("utf-8", "replace"), bool(m.retain)))

    def all(self, topic=None, since=0.0):
        with self.lock:
            return [m for m in self.msgs if (topic is None or m[1] == topic) and m[0] >= since]

    def last(self, topic, since=0.0):
        rows = self.all(topic, since)
        return rows[-1][2] if rows else None

    def json(self, topic, since=0.0):
        v = self.last(topic, since)
        try:
            return json.loads(v) if v else None
        except ValueError:
            return None

    def state(self, serial, prefix="ambientika", since=0.0):
        return self.json("%s/%s/state" % (prefix, serial), since) or {}

    def pub(self, topic, payload, retain=False):
        self.c.publish(topic, payload, qos=1, retain=retain).wait_for_publish(timeout=5)

    def stop(self):
        try:
            self.c.loop_stop()
            self.c.disconnect()
        except Exception:
            pass


class Bridge:
    """The bridge under test: process, standalone image or add-on image."""

    def __init__(self, cloud, broker, env=None, yaml_text=None, options=None, name="bridge"):
        self.dir = tempfile.mkdtemp(prefix="bridge-", dir=WORK)
        base = {
            "AMBIENTIKA_USERNAME": USER, "AMBIENTIKA_PASSWORD": PASSWORD,
            "AMBIENTIKA_HOST": cloud.url, "MQTT_HOST": "127.0.0.1", "MQTT_PORT": str(broker.port),
            "POLL_INTERVAL": str(POLL),
        }
        # the same in every mode: time limit and pauses, shortened for the test
        pace = {"CLOUD_TIMEOUT": str(CLOUD_TIMEOUT), "START_RETRY_S": str(START_RETRY * TIME_FACTOR),
                "SIGN_IN_RETRY_S": str(SIGN_IN_RETRY * TIME_FACTOR)}
        base.update(pace)
        if broker.user:
            base["MQTT_USERNAME"], base["MQTT_PASSWORD"] = broker.user, broker.password
        if yaml_text is not None or options is not None:
            base = dict(pace)
        base.update(env or {})
        base = {k: v for k, v in base.items() if v is not None}
        self.container = None
        if MODE == "process":
            base.setdefault("NEURACELL_STATE_FILE", os.path.join(self.dir, "nc.json"))
            base.setdefault("PENDING_RESET_FILE", os.path.join(self.dir, "pr.json"))
            base.setdefault("FILTER_ACK_PATH", os.path.join(self.dir, "ack.json"))
            base.setdefault("BRIDGE_VERSION", "sim")
            if yaml_text is not None:
                path = os.path.join(self.dir, "config.yaml")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(yaml_text)
                base["CONFIG_YAML"] = path
            else:
                base["CONFIG_YAML"] = os.path.join(self.dir, "none.yaml")
            self.proc = Proc(name, [sys.executable, os.path.join(HERE, "bridge.py")], env=base, cwd=self.dir)
        else:
            self.container = "ambientika-sim-%d" % free_port()
            os.chmod(self.dir, 0o777)
            cmd = ["docker", "run", "--rm", "--name", self.container, "--network", "host",
                   "-v", self.dir + ":/data"]
            if MODE == "addon":
                opts = dict(options if options is not None else addon_options(cloud, broker))
                with open(os.path.join(self.dir, "options.json"), "w", encoding="utf-8") as fh:
                    json.dump(opts, fh)
                for k in pace:
                    cmd += ["-e", "%s=%s" % (k, base[k])]
                cmd.append(ARGS.addon_image)
            else:
                if yaml_text is not None:
                    with open(os.path.join(self.dir, "config.yaml"), "w", encoding="utf-8") as fh:
                        fh.write(yaml_text)
                    base["CONFIG_YAML"] = "/data/config.yaml"
                for k, v in base.items():
                    cmd += ["-e", "%s=%s" % (k, v)]
                cmd.append(ARGS.image)
            self.proc = Proc(name, cmd)

    def log(self):
        return self.proc.log()

    def running(self):
        return self.proc.running()

    def stop(self, sig=signal.SIGTERM, timeout=20):
        if self.container and self.proc.running():
            how = "kill" if sig == signal.SIGKILL else "stop"
            subprocess.run(["docker", how, self.container], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=40)
            try:
                self.proc.p.wait(timeout)
            except subprocess.TimeoutExpired:
                self.proc.p.kill()
            return self.proc.p.returncode
        return self.proc.stop(sig, timeout)

    def wait_exit(self, timeout=30):
        try:
            return self.proc.p.wait(timeout)
        except subprocess.TimeoutExpired:
            return None


def addon_options(cloud, broker, **over):
    """options.json as the Supervisor writes it: the defaults of config.yaml plus the user's values."""
    import yaml
    path = ARGS.addon_config or os.path.join(HERE, "ambientika_mqtt_bridge", "config.yaml")
    with open(path, encoding="utf-8") as fh:
        opts = dict(yaml.safe_load(fh)["options"])
    opts.update({
        "ambientika_username": USER, "ambientika_password": PASSWORD, "ambientika_host": cloud.url,
        "mqtt_host": "127.0.0.1", "mqtt_port": broker.port, "poll_interval": POLL,
        "mqtt_username": broker.user or "", "mqtt_password": broker.password or "",
    })
    opts.update(over)
    return opts


def started(spy, prefix="ambientika", timeout=40):
    """All five units have published a state and are online."""
    return wait(lambda: all(spy.last("%s/%s/availability" % (prefix, s)) == "online"
                            and spy.state(s, prefix) for s in SERIALS), timeout)


def group(name):
    if ONLY and name not in ONLY:
        return False
    print("\n[%s]" % name, flush=True)
    return True


def no_traceback(text):
    return "Traceback (most recent call last)" not in text


# ---------------------------------------------------------------------------
def scenario_start_and_values():
    cloud, broker = Cloud(), Broker()
    spy = Spy(broker.port)
    b = Bridge(cloud, broker)
    try:
        check("all five units publish a state and are online", started(spy), b.log()[-1500:])
        log = b.log()
        check("login at the cloud stand-in", any(r["ok"] for r in cloud.log("auth")), cloud.log())
        check("five units found", "Found 5 device(s)." in log, log[-800:])
        check("bridge availability online, retained",
              wait(lambda: spy.last("ambientika/bridge/availability") == "online", 5))
        def discovery():
            found = [m for m in spy.all() if m[1].startswith("homeassistant/") and m[1].endswith("/config") and m[2]]
            return found, {s: [m for m in found if "/%s_" % s in m[1]] for s in SERIALS}

        def complete():
            per = discovery()[1]
            return len({len(v) for v in per.values()}) == 1 and len(per[MASTER]) >= 30

        # the bridge announces one unit after the other; a slow machine is still at it
        complete_in_time = wait(complete, 20)
        cfgs, per_unit = discovery()
        check("discovery for every unit (same entity count each)", complete_in_time,
              {s: len(v) for s, v in per_unit.items()})
        names = {}
        for m in cfgs:
            try:
                d = json.loads(m[2])
                ids = d["device"]["identifiers"][0]
                names[ids] = d["device"]["name"]
            except Exception:
                check("discovery payload is JSON with a device", False, m[1])
        check("unit names from the app in discovery (umlauts, hyphen, space)",
              all(names.get(s) == n for s, n in NAMES.items()), names)
        st = spy.state(MASTER)
        check("state: values of the cloud", st.get("temperature") == 21 and st.get("humidity") == 55
              and st.get("operating_mode") == "Smart" and st.get("fan_speed") == "Medium"
              and st.get("air_quality") == "Good" and st.get("filters_status") == "Good", st)
        check("state: numeric twins", st.get("operating_mode_num") == 0 and st.get("fan_speed_num") == 2
              and st.get("air_quality_num") == 3 and st.get("filter_status_num") == 0, st)
        check("state and availability are retained",
              any(m[3] for m in Spy_late(broker).collect(1.5) if m[1] == "ambientika/%s/state" % MASTER))
        check("a Slave carries its role and zone", spy.state(SLAVE).get("device_role") == "SlaveEqualMaster"
              and spy.state(MASTER2).get("zone_index") == 1, spy.state(SLAVE))

        # values follow the cloud
        t0 = time.time()
        cloud.unit(MASTER, set={"temperature": 23.5, "humidity": 61, "airQuality": "Poor",
                                "filtersStatus": "Bad", "humidityAlarm": True, "nightAlarm": True,
                                "fanSpeed": "Night"})
        ok = wait(lambda: spy.state(MASTER, since=t0).get("temperature") == 23.5, 15)
        st = spy.state(MASTER, since=t0)
        check("new values arrive within a few polls", ok, st)
        check("air quality, filter, alarms follow", st.get("air_quality") == "Poor" and st.get("filters_status") == "Bad"
              and st.get("humidity_alarm") is True and st.get("night_alarm") is True
              and st.get("air_quality_num") == 1, st)
        check("read-only fan step Night is shown", st.get("fan_speed") == "Night", st)

        t0 = time.time()
        cloud.unit(SLAVE2, set={"humidity": 118})
        ok = wait(lambda: spy.state(SLAVE2, since=t0).get("humidity_raw") == 118, 15)
        st = spy.state(SLAVE2, since=t0)
        check("saturated sensor: 118 % is published as 100 %, raw value beside it",
              ok and st.get("humidity") == 100, st)

        t0 = time.time()
        cloud.unit(MASTER2, set={"operatingMode": "BrandNewMode", "fanSpeed": "Turbo"})
        ok = wait(lambda: spy.state(MASTER2, since=t0).get("fan_speed") == "Turbo", 15)
        st = spy.state(MASTER2, since=t0)
        check("a value the library does not know does not stop the poll", ok and b.running(), st)
        check("no traceback in the bridge log", no_traceback(b.log()), b.log()[-1500:])
        cloud.unit(MASTER2, set={"operatingMode": "Smart", "fanSpeed": "Medium"})
        cloud.unit(MASTER, set={"fanSpeed": "Medium", "nightAlarm": False})
    finally:
        b.stop(); spy.stop(); broker.stop(); cloud.stop()


class Spy_late:
    """A client that connects now: it sees what the broker has retained."""

    def __init__(self, broker):
        self.broker = broker

    def collect(self, seconds):
        s = Spy(self.broker.port, self.broker.user, self.broker.password)
        time.sleep(seconds)
        rows = s.all()
        s.stop()
        return rows


def scenario_commands():
    cloud, broker = Cloud(), Broker()
    spy = Spy(broker.port)
    b = Bridge(cloud, broker)
    try:
        if not check("start", started(spy), b.log()[-1500:]):
            return
        cloud.clear()
        t0 = time.time()
        spy.pub("ambientika/%s/set/operating_mode" % MASTER, "Night")
        ok = wait(lambda: cloud.log("change-mode"), 10)
        row = (cloud.log("change-mode") or [{}])[0]
        check("mode Night -> one change-mode call the cloud accepts", ok and row.get("ok") is True, row)
        body = row.get("body", {})
        check("call carries mode 3 and keeps fan / humidity / light as they were",
              str(body.get("operatingMode")) == "3" and body.get("fanSpeed") == 1
              and body.get("humidityLevel") == 1 and body.get("lightSensorLevel") == 2, body)
        check("state shows Night", wait(lambda: spy.state(MASTER, since=t0).get("operating_mode") == "Night", 10),
              spy.state(MASTER))

        cloud.clear()
        t0 = time.time()
        for attr, val in (("fan_speed", "High"), ("humidity_level", "Moist"), ("light_sensor_level", "Off")):
            spy.pub("ambientika/%s/set/%s" % (MASTER, attr), val)
        wait(lambda: cloud.log("change-mode"), 10)
        time.sleep(2.0)
        rows = cloud.log("change-mode")
        check("three commands in a row are combined into one call", len(rows) == 1, rows)
        body = rows[0]["body"] if rows else {}
        check("combined call: High / Moist / Off, mode kept",
              body.get("fanSpeed") == 2 and body.get("humidityLevel") == 2 and body.get("lightSensorLevel") == 1
              and str(body.get("operatingMode")) == "3", body)
        st = wait(lambda: (lambda s: s if s.get("fan_speed") == "High" else None)(spy.state(MASTER, since=t0)), 10) or {}
        check("state follows", st.get("humidity_level") == "Moist" and st.get("light_sensor_level") == "Off", st)

        cloud.clear()
        spy.pub("ambientika/%s/set" % MASTER2, json.dumps({"mode": "Expulsion", "fan_speed": "Low"}))
        ok = wait(lambda: cloud.log("change-mode"), 10)
        body = (cloud.log("change-mode") or [{}])[0].get("body", {})
        check("JSON command with several attributes -> one call",
              ok and str(body.get("operatingMode")) == "7" and body.get("fanSpeed") == 0, body)

        cloud.clear()
        for topic, payload in (
                ("ambientika/%s/set/operating_mode" % MASTER, "Hyperdrive"),
                ("ambientika/%s/set/fan_speed" % MASTER, "9"),
                ("ambientika/%s/set/humidity_level" % MASTER, ""),
                ("ambientika/%s/set/temperature" % MASTER, "30"),
                ("ambientika/%s/set" % MASTER, "{not json"),
                ("ambientika/%s/set" % MASTER, "[1,2]"),
                ("ambientika/FFFFFFFFFFFF/set/operating_mode", "Off"),
                ("ambientika/%s/set/operating_mode" % MASTER, "x" * 5000)):
            spy.pub(topic, payload)
        time.sleep(3.0)
        check("invalid commands never reach the cloud", cloud.log("change-mode") == [], cloud.log("change-mode"))
        check("bridge still running, no traceback", b.running() and no_traceback(b.log()), b.log()[-1500:])

        cloud.clear()
        spy.pub("ambientika/%s/set/fan_speed" % MASTER, "Turbo")
        ok = wait(lambda: cloud.log("change-mode"), 10)
        body = (cloud.log("change-mode") or [{}])[0].get("body", {})
        check("read-only step Turbo as a command is sent as High", ok and body.get("fanSpeed") == 2, body)

        # filter reset on a Master
        cloud.unit(MASTER, set={"filtersStatus": "Bad"})
        wait(lambda: spy.state(MASTER).get("filters_status_raw") == "Bad", 10)
        cloud.clear()
        t0 = time.time()
        spy.pub("ambientika/%s/set/reset_filter" % MASTER, "PRESS")
        ok = wait(lambda: cloud.log("reset-filter"), 10)
        check("filter reset -> documented reset-filter call for the unit",
              ok and cloud.log("reset-filter")[0]["serial"] == MASTER, cloud.log())
        check("only GET reset-filter, nothing unexpected", cloud.log("UNEXPECTED") == [], cloud.log("UNEXPECTED"))
        seen = lambda: [m[2] for m in spy.all("ambientika/%s/reset_state" % MASTER, since=t0)]
        ok = wait(lambda: "confirmed" in seen(), 40)
        check("reset status runs through to confirmed", ok and seen()[0] == "running", seen())
        check("filter shows Good again", wait(lambda: spy.state(MASTER).get("filters_status") == "Good", 10),
              spy.state(MASTER))
    finally:
        b.stop(); spy.stop(); broker.stop(); cloud.stop()


def scenario_outages():
    cloud, broker = Cloud(), Broker()
    spy = Spy(broker.port)
    b = Bridge(cloud, broker)
    av = lambda s, since=0.0: spy.last("ambientika/%s/availability" % s, since)
    try:
        if not check("start", started(spy), b.log()[-1500:]):
            return
        # one unit without status packet
        t0 = time.time()
        cloud.unit(SLAVE2, fault="404")
        ok = wait(lambda: av(SLAVE2, t0) == "offline", 30)
        took = time.time() - t0
        check("one unit without status packet goes offline after 3 polls (%.0f s)" % took,
              ok and took >= 2 * POLL - 1, took)
        check("the other units stay online", all(av(s) == "online" for s in SERIALS if s != SLAVE2))
        d = spy.json("ambientika/%s/diagnostics" % SLAVE2) or {}
        check("diagnostics count the failed polls", d.get("poll_failures_total", 0) >= 3
              and d.get("poll_failures_consecutive", 0) >= 3 and d.get("last_outage_start"), d)
        t1 = time.time()
        cloud.unit(SLAVE2, fault=None)
        check("unit back -> online", wait(lambda: av(SLAVE2, t1) == "online", 15))
        d = wait(lambda: (lambda x: x if x and x.get("poll_failures_consecutive") == 0 else None)(
            spy.json("ambientika/%s/diagnostics" % SLAVE2)), 10) or {}
        check("diagnostics: run of failures closed, outage end recorded", d.get("last_outage_end"), d)

        # broken answers
        for fault in ("garbage", "html", "500"):
            t0 = time.time()
            cloud.unit(MASTER2, fault=fault)
            ok = wait(lambda: av(MASTER2, t0) == "offline", 30)
            check("answer '%s' counts as failed poll -> offline, bridge keeps running" % fault,
                  ok and b.running(), b.log()[-800:])
            t1 = time.time()
            cloud.unit(MASTER2, fault=None)
            check("  ... and back online", wait(lambda: av(MASTER2, t1) == "online", 15))
        check("no traceback for broken answers", no_traceback(b.log()), b.log()[-2500:])

        # token expires
        cloud.clear()
        t0 = time.time()
        cloud.expire()
        ok = wait(lambda: any(r["ok"] for r in cloud.log("auth")), 20)
        check("expired token -> the bridge logs in again", ok, cloud.log()[-5:])
        time.sleep(3 * POLL + 1)
        check("no unit went offline over the new login",
              not [m for m in spy.all(since=t0) if m[1].endswith("/availability") and m[2] == "offline"])
        t1 = time.time()
        check("values keep arriving", wait(lambda: spy.all("ambientika/%s/state" % MASTER, since=t1), 10))

        # the cloud no longer accepts the password while the bridge runs
        cloud.clear()
        cloud.outage("refuse-auth")
        cloud.expire()
        quiet = (SIGN_IN_RETRY + 2 * SIGN_IN_RETRY) * TIME_FACTOR + 2 * POLL + 2   # room for attempts 1, 2 and 3
        time.sleep(quiet)
        auth = cloud.log("auth")
        check("password no longer accepted: the sign-in is tried again after growing pauses, "
              "not at every poll (%d attempts in %.0f s, %d polls)" % (len(auth), quiet, quiet // POLL),
              2 <= len(auth) <= 3 and not any(r["ok"] for r in auth), auth)
        check("  ... one line per attempt names user name and password, the bridge keeps running",
              b.log().count("does not accept user name and password") == len(auth) and b.running(), b.log()[-1200:])
        t1 = time.time()
        cloud.outage("ok")
        check("password accepted again -> signed in at the next attempt, all units online",
              wait(lambda: any(r["ok"] for r in cloud.log("auth")), 4 * SIGN_IN_RETRY + 10)
              and wait(lambda: all(av(s) == "online" for s in SERIALS), 30), (cloud.log("auth")[-3:], {s: av(s) for s in SERIALS}))

        # whole cloud answers 500
        t0 = time.time()
        cloud.outage("500")
        ok = wait(lambda: all(av(s, t0) == "offline" for s in SERIALS), 40)
        check("cloud answers 500 -> all units offline", ok, {s: av(s) for s in SERIALS})
        check("bridge itself stays online", spy.last("ambientika/bridge/availability") == "online" and b.running())
        t1 = time.time()
        cloud.outage("ok")
        check("cloud back -> all units online again", wait(lambda: all(av(s, t1) == "online" for s in SERIALS), 40),
              {s: av(s) for s in SERIALS})
        check("no traceback", no_traceback(b.log()), b.log()[-2500:])
    finally:
        b.stop(); spy.stop(); broker.stop(); cloud.stop()


def scenario_hang():
    """The cloud accepts a request and never answers."""
    cloud, broker = Cloud(), Broker()
    spy = Spy(broker.port)
    b = Bridge(cloud, broker)
    av = lambda s, since=0.0: spy.last("ambientika/%s/availability" % s, since)
    try:
        if not check("start", started(spy), b.log()[-1500:]):
            return
        t0 = time.time()
        cloud.unit(SLAVE, fault="hang")
        wait(lambda: cloud.log("hang"), 15)
        t_hang = time.time()
        time.sleep(6 * CLOUD_TIMEOUT)
        stamps = [m[0] for m in spy.all("ambientika/%s/state" % MASTER, since=t_hang)]
        gaps = [b2 - a for a, b2 in zip([t_hang] + stamps, stamps + [time.time()])]
        check("one unit never answers: the others keep being polled (longest gap %.0f s)" % max(gaps),
              len(stamps) >= 3 and max(gaps) <= 2 * CLOUD_TIMEOUT + POLL + 2, (len(stamps), [round(g, 1) for g in gaps]))
        ok = wait(lambda: av(SLAVE, t0) == "offline", 6 * CLOUD_TIMEOUT)
        check("the silent unit goes offline after 3 timeouts", ok, av(SLAVE))
        cloud.clear()
        spy.pub("ambientika/%s/set/fan_speed" % MASTER2, "High")
        check("commands for other units still go out meanwhile", wait(lambda: cloud.log("change-mode"), 10))
        t1 = time.time()
        cloud.unit(SLAVE, fault=None)
        check("unit answers again -> online", wait(lambda: av(SLAVE, t1) == "online", 6 * CLOUD_TIMEOUT))

        # the whole cloud hangs
        t0 = time.time()
        cloud.outage("hang")
        ok = wait(lambda: all(av(s, t0) == "offline" for s in SERIALS), 20 * CLOUD_TIMEOUT)
        took = time.time() - t0
        check("whole cloud hangs -> all units offline (%.0f s)" % took, ok, {s: av(s) for s in SERIALS})
        check("bridge alive and connected to the broker meanwhile",
              b.running() and spy.last("ambientika/bridge/availability") == "online")
        t1 = time.time()
        cloud.outage("ok")
        check("cloud answers again -> all units online",
              wait(lambda: all(av(s, t1) == "online" for s in SERIALS), 20 * CLOUD_TIMEOUT), {s: av(s) for s in SERIALS})
        check("no traceback", no_traceback(b.log()), b.log()[-2500:])

        # stop while a request hangs
        cloud.outage("hang")
        time.sleep(POLL + 1)
        t0 = time.time()
        rc = b.stop()
        check("SIGTERM during a hanging request: clean exit within %d s (%.1f s, code %s)"
              % (CLOUD_TIMEOUT + 6, time.time() - t0, rc), rc == 0 and time.time() - t0 <= CLOUD_TIMEOUT + 6, rc)
    finally:
        b.stop(); spy.stop(); broker.stop(); cloud.stop()


def scenario_broker():
    cloud, broker = Cloud(), Broker()
    spy = Spy(broker.port)
    b = Bridge(cloud, broker)
    try:
        if not check("start", started(spy), b.log()[-1500:]):
            return
        spy.stop()
        broker.stop()
        time.sleep(3 * POLL)
        check("broker gone: the bridge keeps running", b.running(), b.log()[-800:])
        broker.start()
        spy = Spy(broker.port)
        t0 = time.time()
        ok = wait(lambda: spy.last("ambientika/bridge/availability") == "online", 40)
        check("broker back (empty, no persistence): bridge reconnects (%.0f s)" % (time.time() - t0), ok,
              b.log()[-800:])
        check("discovery is published again",
              wait(lambda: [m for m in spy.all() if m[1].startswith("homeassistant/sensor/%s_" % MASTER)], 15))
        check("states and availability are published again", started(spy, timeout=20))
        cloud.clear()
        spy.pub("ambientika/%s/set/fan_speed" % MASTER, "Low")
        check("commands work again (subscriptions renewed)", wait(lambda: cloud.log("change-mode"), 10))

        # clean stop
        t0 = time.time()
        rc = b.stop()
        check("SIGTERM: exit code 0 within 10 s (%.1f s)" % (time.time() - t0), rc == 0 and time.time() - t0 < 10, rc)
        check("clean stop publishes bridge offline",
              wait(lambda: spy.last("ambientika/bridge/availability", since=t0) == "offline", 5),
              spy.all("ambientika/bridge/availability"))
        late = Spy_late(broker).collect(1.5)
        check("offline is retained for clients that connect later",
              any(m[1] == "ambientika/bridge/availability" and m[2] == "offline" and m[3] for m in late))

        # kill -> Last Will
        b = Bridge(cloud, broker)
        check("restart: online again", wait(lambda: spy.last("ambientika/bridge/availability", since=t0 + 0.5) == "online", 40),
              b.log()[-800:])
        time.sleep(POLL)
        t0 = time.time()
        b.stop(signal.SIGKILL)
        check("killed bridge: the broker publishes the Last Will offline",
              wait(lambda: spy.last("ambientika/bridge/availability", since=t0) == "offline", 15),
              spy.all("ambientika/bridge/availability")[-3:])
    finally:
        b.stop(); spy.stop(); broker.stop(); cloud.stop()


def scenario_start_failures():
    """What is missing when the bridge starts: it waits and tries again instead of ending."""
    cloud, broker = Cloud(), Broker()
    spy = Spy(broker.port)
    online = lambda since: spy.last("ambientika/bridge/availability", since) == "online"
    b = late = late_broker = spy2 = None

    def with_host(host):
        return Bridge(cloud, broker, env={"AMBIENTIKA_HOST": host}) if MODE != "addon" else \
            Bridge(cloud, broker, options=addon_options(cloud, broker, ambientika_host=host))

    def stopped_cleanly(what):
        t0 = time.time()
        rc = b.stop()
        check("  ... stopped %s: ends with exit code 0 within 10 s (%.1f s)" % (what, time.time() - t0),
              rc == 0 and time.time() - t0 < 10 * TIME_FACTOR, (rc, b.log()[-600:]))

    try:
        # wrong password
        cloud.clear()
        b = Bridge(cloud, broker, env={"AMBIENTIKA_PASSWORD": "falsch"}) if MODE != "addon" else \
            Bridge(cloud, broker, options=addon_options(cloud, broker, ambientika_password="falsch"))
        said = wait(lambda: "did not accept user name and password" in b.log(), 25)
        check("wrong app password: one line names user name and password, no traceback",
              said and no_traceback(b.log()), b.log()[-800:])
        window = (SIGN_IN_RETRY + 2 * SIGN_IN_RETRY) * TIME_FACTOR + 2      # room for attempts 1, 2 and 3
        time.sleep(window)
        auth = cloud.log("auth")
        check("  ... the bridge keeps running and tries again after growing pauses (%d sign-ins in %.0f s)"
              % (len(auth), window), b.running() and 2 <= len(auth) <= 4 and not any(r["ok"] for r in auth), auth)
        stopped_cleanly("while it waits for the next sign-in")

        # the cloud turns the right password down for a while
        cloud.outage("refuse-auth")
        cloud.clear()
        t0 = time.time()
        b = Bridge(cloud, broker)
        refused = wait(lambda: cloud.log("auth"), 25)
        cloud.outage("ok")
        check("the cloud turns the password down for a while: the bridge starts by itself once it is accepted",
              refused and wait(lambda: online(t0), 4 * SIGN_IN_RETRY + 20) and started(spy), b.log()[-1000:])
        b.stop()

        # no credentials at all
        b = Bridge(cloud, broker, env={"AMBIENTIKA_USERNAME": "", "AMBIENTIKA_PASSWORD": ""}) if MODE != "addon" else \
            Bridge(cloud, broker, options=addon_options(cloud, broker, ambientika_username="", ambientika_password=""))
        rc = b.wait_exit(40)
        check("no credentials: stops and names the options", rc not in (None, 0)
              and "username/password missing" in b.log(), b.log()[-800:])

        # nothing answers at the address of the cloud; later the cloud is there
        port = free_port()
        b = with_host("http://127.0.0.1:%d" % port)
        tried = wait(lambda: b.log().count("No connection to the Ambientika cloud") >= 2, 3 * START_RETRY + 20)
        log = b.log()
        check("cloud unreachable at start: the bridge keeps running and tries again, one line per attempt, no traceback",
              tried and b.running() and no_traceback(log) and "Next attempt in" in log, log[-800:])
        t0 = time.time()
        late = Cloud(port)
        came = wait(lambda: online(t0), 4 * START_RETRY + 30)
        check("  ... and starts by itself once the cloud can be reached (%.0f s)" % (time.time() - t0),
              came and "Found 5 device(s)." in b.log(), b.log()[-1000:])
        b.stop()
        late.stop()
        late = None

        # the cloud answers the sign-in with an error
        cloud.outage("500")
        b = Bridge(cloud, broker)
        said = wait(lambda: "The Ambientika cloud is not ready" in b.log(), 25)
        check("cloud answers the sign-in with an error: the bridge keeps running, one line, no traceback",
              said and b.running() and no_traceback(b.log()), b.log()[-800:])
        t0 = time.time()
        cloud.outage("ok")
        check("  ... and starts by itself once the cloud works", wait(lambda: online(t0), 4 * START_RETRY + 30),
              b.log()[-1000:])
        b.stop()

        # the cloud accepts the sign-in and never answers
        cloud.outage("hang")
        t0 = time.time()
        b = Bridge(cloud, broker)
        gave_up = wait(lambda: "No connection to the Ambientika cloud" in b.log(), CLOUD_TIMEOUT + 25)
        check("cloud never answers the sign-in: the attempt ends at the time limit (%.0f s), the bridge keeps running"
              % (time.time() - t0), gave_up and b.running() and no_traceback(b.log()), b.log()[-600:])
        wait(lambda: b.log().count("Authenticating with Ambientika API") >= 2, 3 * START_RETRY + 10)
        stopped_cleanly("while a sign-in is out")
        cloud.outage("ok")

        # nothing answers at the address of the broker; later the broker is there
        port = free_port()
        dead_broker = type("B", (), {"port": port, "user": None, "password": None})()
        b = Bridge(cloud, dead_broker)
        tried = wait(lambda: b.log().count("No connection to the MQTT broker") >= 2, 3 * START_RETRY + 25)
        log = b.log()
        check("broker unreachable at start: the bridge keeps running and tries again, no traceback",
              tried and b.running() and no_traceback(log), log[-800:])
        after = log.split("Connecting to MQTT broker")[-1]
        check("  ... and the message names the MQTT broker with its address, not the cloud",
              "MQTT broker 127.0.0.1:%d" % port in after and "Ambientika cloud" not in after, after[-600:])
        late_broker = Broker(port)
        spy2 = Spy(port)
        check("  ... and connects by itself once the broker is there: all five units online",
              started(spy2, timeout=4 * START_RETRY + 40), b.log()[-1000:])
        stopped_cleanly("in normal operation")
    finally:
        for thing in (b, spy, spy2, late, late_broker, broker, cloud):
            if thing is not None:
                try:
                    thing.stop()
                except Exception:
                    pass


def scenario_config():
    cloud = Cloud()
    broker = Broker(user="lüftung", password="p#w \"x' $1 ä")
    spy = Spy(broker.port, broker.user, broker.password)
    b = None
    try:
        if MODE == "addon":
            b = Bridge(cloud, broker, options=addon_options(cloud, broker, mqtt_topic_prefix="haus/lueftung",
                                                            availability_failure_threshold=1))
        else:
            yaml_text = (
                "ambientika:\n  username: %s\n  password: %s\n  host: %s\n"
                "mqtt:\n  host: 127.0.0.1\n  port: %d\n  username: %s\n  password: %s\n"
                "bridge:\n  topic_prefix: haus/lueftung\n  poll_interval: %d\n  availability_failure_threshold: 1\n"
                % (json.dumps(USER), json.dumps(PASSWORD, ensure_ascii=False), cloud.url, broker.port,
                   json.dumps(broker.user, ensure_ascii=False), json.dumps(broker.password, ensure_ascii=False), POLL))
            b = Bridge(cloud, broker, yaml_text=yaml_text)
        check("config file, broker with user and a password full of special characters, own prefix: units online",
              started(spy, "haus/lueftung"), b.log()[-1500:])
        check("nothing is published under the default prefix",
              not [m for m in spy.all() if m[1].startswith("ambientika/")])
        t0 = time.time()
        cloud.unit(MASTER, fault="404")
        ok = wait(lambda: spy.last("haus/lueftung/%s/availability" % MASTER, t0) == "offline", 15)
        check("availability_failure_threshold 1 from the file is used (offline after one poll, %.0f s)"
              % (time.time() - t0), ok and time.time() - t0 < 3 * POLL + 2)
        cloud.unit(MASTER, fault=None)
        cloud.clear()
        spy.pub("haus/lueftung/%s/set/operating_mode" % MASTER2, "Off")
        check("command under the own prefix reaches the cloud", wait(lambda: cloud.log("change-mode"), 10))
        b.stop()

        if MODE == "process":
            # environment wins over the file
            b = Bridge(cloud, broker, yaml_text=yaml_text, env={"MQTT_TOPIC_PREFIX": "umgebung"})
            check("a variable of the environment wins over config.yaml", started(spy, "umgebung"), b.log()[-800:])
            b.stop()
            # short names of docker-compose / .env.example
            b = Bridge(cloud, broker, env={"AMBIENTIKA_USERNAME": None, "AMBIENTIKA_PASSWORD": None,
                                           "MQTT_USERNAME": None, "AMBIENTIKA_USER": USER,
                                           "AMBIENTIKA_PASS": PASSWORD, "MQTT_USER": broker.user,
                                           "MQTT_TOPIC_PREFIX": "kurz"})
            check("short variable names of docker-compose (AMBIENTIKA_USER / _PASS, MQTT_USER) work",
                  started(spy, "kurz"), b.log()[-800:])
            b.stop()

        # wrong broker password
        bad = type("B", (), {"port": broker.port, "user": broker.user, "password": "falsch"})()
        b = Bridge(cloud, bad)
        ok = wait(lambda: "MQTT connection failed" in b.log(), 20)
        check("wrong broker password: reported in the log", ok, b.log()[-600:])
        time.sleep(3)
        check("  ... no traceback", no_traceback(b.log()), b.log()[-600:])
    finally:
        if b:
            b.stop()
        spy.stop(); broker.stop(); cloud.stop()


GROUPS = [
    ("start", scenario_start_and_values),
    ("commands", scenario_commands),
    ("outages", scenario_outages),
    ("hang", scenario_hang),
    ("broker", scenario_broker),
    ("startfail", scenario_start_failures),
    ("config", scenario_config),
]

if shutil.which(MOSQUITTO) is None:
    print("mosquitto is not installed")
    sys.exit(2)
print("System test of the bridge as %s, work folder %s" % (
    {"process": "process (bridge.py)", "image": "image " + str(ARGS.image),
     "addon": "add-on image " + str(ARGS.addon_image)}[MODE], WORK))
try:
    for name, fn in GROUPS:
        if group(name):
            fn()
finally:
    if not ARGS.keep and not FAILS:
        shutil.rmtree(WORK, ignore_errors=True)
print("\nRESULT: %d checks, %s" % (PASSES + len(FAILS), "ALL PASS" if not FAILS else "FAILED: %s" % FAILS))
if FAILS:
    print("logs kept in", WORK)
sys.exit(1 if FAILS else 0)
