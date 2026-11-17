#!/usr/bin/env python3
"""Update test of the integration 'Ambientika for Home Assistant' in a real Home Assistant.

    python sim/home-assistant/upgrade_test.py --old PATH --new PATH [--only a,b] [--hass HASS] [--keep]

PATH is a folder custom_components/ambientika: --old is the released version an
installation comes from, --new the version under test. Both run unchanged
except for DEFAULT_HOST, which points at sim/fake_cloud.py.

A scenario is a row of phases. In every phase one of the two versions is put
into the configuration folder and Home Assistant runs until the test driver
(upgrade/custom_components/simtest_upgrade) has finished its checks and stops
it. The configuration folder - and with it the entity and device registry -
stays the same for all phases of a scenario, exactly as on a real installation
that is updated. After every phase the log of Home Assistant is searched for
errors and warnings nobody expects.

Scenarios:
  plain                 five units; entity IDs, names, areas, disabled and hidden entities survive
  same-name             two units carry the same name (the old version gave only one its entities)
  renamed               a unit was renamed in the app while the old version was running
  renamed-no-restart    a unit was renamed in the app right before the update
  fresh                 new installation; second set-up of the same account; rename; same name
  downgrade             new version, back to the old one, forward again
  start-http-error      first start of the new version while the cloud answers with errors
  start-no-connection   first start of the new version without any connection to the cloud
  password-not-accepted first start of the new version while the cloud turns the password down
  sign-in-expires       the sign-in of the cloud expires while Home Assistant runs
  start-http-error-old-version, sign-in-expires-old-version
                        the same situations with the old version, to show what the new one fixes

Needs: hass (pip install homeassistant, plus the requirements in the manifest of
the integration) and python with aiohttp for the stand-in.
"""
import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
FLUR, WOHNEN = "A0B1C2D3E402", "A0B1C2D3E401"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Cloud:
    """The stand-in for the Ambientika cloud; it keeps its state over a restart."""

    def __init__(self, work, python):
        self.port, self.python = free_port(), python
        self.state = os.path.join(work, "cloud-state.json")
        self.url = "http://127.0.0.1:%d" % self.port
        self.proc = None
        self.start()

    def start(self):
        self.proc = subprocess.Popen([self.python, os.path.join(REPO, "sim", "fake_cloud.py"), str(self.port),
                                      self.state], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        end = time.time() + 15
        while time.time() < end:
            try:
                self.ctl("GET", "/_ctl/log")
                return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("the stand-in for the cloud did not start")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def ctl(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode() or "null")

    def rename(self, serial, name):
        self.ctl("POST", "/_ctl/unit", {"serial": serial, "name": name})

    def outage(self, mode):
        self.ctl("POST", "/_ctl/outage", {"mode": mode})


class Phase:
    def __init__(self, name, version, before=None, during=None, allow=(), require=(), forbid=(), timeout=420):
        self.name, self.version, self.before, self.during = name, version, before, during
        self.allow, self.require, self.forbid, self.timeout = list(allow), list(require), list(forbid), timeout


def return_when_asked(cloud, work):
    """Start the stand-in again once the driver has seen Home Assistant without it."""
    marker = os.path.join(work, "cloud-may-return")
    end = time.time() + 300
    while time.time() < end and not os.path.exists(marker):
        time.sleep(0.5)
    time.sleep(3)
    cloud.start()


MOVED = r"Moved the unique IDs of %d entities"
NOT_MOVED = r"Moved the unique IDs"
# lines the old version writes when two units share a name
OLD_SAME_NAME = [r"does not generate unique IDs"]
# lines that belong to a cloud that fails on purpose
FAILING_CLOUD = [r"Could not fetch status for device", r"Updating device \S+ failed", r"Writing to device \S+: failed",
                 r"press \S+: failed", r"signing in again failed", r"did not accept user name and password",
                 r"answered the sign-in with HTTP status 500"]
NOT_READY = [r"integration not ready yet: .*Ambientika cloud"]
# Home Assistant writes these with the traceback of the error that ended the set-up
OLD_SETUP_ERROR = [r"Error setting up entry"]
PASSWORD = [r"did not accept user name and password"]

SCENARIOS = {
    "plain": [
        Phase("old", "old"),
        Phase("new-first", "new", require=[MOVED % 30]),
        Phase("new-second", "new", forbid=[NOT_MOVED]),
    ],
    "same-name": [
        Phase("old", "old", before=lambda c, w: c.rename(WOHNEN, "Flur"), allow=OLD_SAME_NAME, require=OLD_SAME_NAME),
        Phase("new-first", "new", require=[MOVED % 25]),
        Phase("new-second", "new", forbid=[NOT_MOVED]),
    ],
    "renamed": [
        Phase("old", "old"),
        Phase("old-again", "old", before=lambda c, w: c.rename(FLUR, "Gang")),
        Phase("new-first", "new", require=[MOVED % 30]),
        Phase("new-second", "new", forbid=[NOT_MOVED]),
    ],
    "renamed-no-restart": [
        Phase("old", "old"),
        Phase("new-first", "new", before=lambda c, w: c.rename(FLUR, "Gang"), require=[MOVED % 30]),
        Phase("new-second", "new", forbid=[NOT_MOVED]),
    ],
    "fresh": [
        Phase("new", "new", allow=FAILING_CLOUD, forbid=[NOT_MOVED]),
        Phase("new-second", "new", forbid=[NOT_MOVED]),
    ],
    "downgrade": [
        Phase("new", "new"),
        Phase("old", "old"),
        Phase("new-again", "new", forbid=[NOT_MOVED]),
    ],
    "start-http-error": [
        Phase("old", "old"),
        Phase("new", "new", before=lambda c, w: c.outage("500"), require=NOT_READY + [MOVED % 30]),
    ],
    "start-no-connection": [
        Phase("old", "old"),
        Phase("new", "new", before=lambda c, w: c.stop(), during=return_when_asked, require=NOT_READY + [MOVED % 30]),
    ],
    "password-not-accepted": [
        Phase("old", "old"),
        Phase("new", "new", before=lambda c, w: c.outage("refuse-auth"), allow=PASSWORD,
              require=[r"Error setting up entry .* did not accept user name and password", MOVED % 30]),
    ],
    "sign-in-expires": [
        Phase("new", "new", allow=FAILING_CLOUD, require=[r"had expired, signed in again", r"signing in again failed"]),
    ],
    "start-http-error-old-version": [
        Phase("old", "old"),
        Phase("old-start", "old", before=lambda c, w: c.outage("500"), allow=OLD_SETUP_ERROR, require=OLD_SETUP_ERROR[:1]),
    ],
    "sign-in-expires-old-version": [
        Phase("old", "old", allow=FAILING_CLOUD),
    ],
}

# What newer versions of Home Assistant say about the old version of the
# integration (fixed in 1.3).
OLD_DEPRECATIONS = [r"deprecated constant CONN_CLASS_CLOUD_POLL",
                    r"provides the deprecated (temperature_unit|current_temperature)"]
# Lines of Home Assistant itself that say nothing about the integration.
HARMLESS = [r"We found a custom integration", r"custom_components\.simtest_upgrade",
            r"Error loading libturbojpeg"]


def problems(log_text, allow):
    """Errors and warnings about the integration that nobody expects.

    A line without a time stamp (a traceback) belongs to the record before it.
    """
    record = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d")
    found, state = [], "ok"
    for line in log_text.splitlines():
        if record.match(line):
            suspicious = " ERROR " in line or (" WARNING " in line and "ambientika" in line.lower())
            if " DEBUG " in line:
                state = "expected"  # details Home Assistant adds at debug level, tracebacks included
            elif not suspicious:
                state = "ok"
            elif any(re.search(p, line) for p in HARMLESS) or any(re.search(p, line) for p in allow):
                state = "expected"
            else:
                state = "problem"
                found.append(line[:400])
        elif state == "problem":
            if found[-1].count("\n") < 12:
                found[-1] += "\n      " + line[:200]
        elif state == "ok" and "Traceback (most recent call last)" in line:
            state = "problem"
            found.append(line[:400])
    return found


def install(config, source, cloud_url):
    """Put one version of the integration into the configuration folder."""
    target = os.path.join(config, "custom_components", "ambientika")
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
    const = os.path.join(target, "const.py")
    with open(const, encoding="utf-8") as fh:
        text = fh.read()
    text, n = re.subn(r'(?m)^DEFAULT_HOST = .*$', 'DEFAULT_HOST = "%s"' % cloud_url, text)
    if n != 1:
        raise RuntimeError("DEFAULT_HOST not found in %s" % const)
    with open(const, "w", encoding="utf-8") as fh:
        fh.write(text)
    with open(os.path.join(target, "manifest.json"), encoding="utf-8") as fh:
        return json.load(fh)["version"]


def run_scenario(name, args, rows):
    work = tempfile.mkdtemp(prefix="hacs-update-%s-" % name)
    os.chmod(work, 0o755)
    config = os.path.join(work, "config")
    shutil.copytree(os.path.join(HERE, "upgrade"), config, ignore=shutil.ignore_patterns("__pycache__"))
    conf = os.path.join(config, "configuration.yaml")
    with open(conf, encoding="utf-8") as fh:
        text = fh.read()
    with open(conf, "w", encoding="utf-8") as fh:
        fh.write(re.sub(r"server_port: \d+", "server_port: %d" % free_port(), text))
    cloud = Cloud(work, args.python)
    ok = True
    try:
        for phase in SCENARIOS[name]:
            label = "%s / %s" % (name, phase.name)
            cloud.ctl("POST", "/_ctl/clear", {})  # every phase sees only its own calls
            if phase.before:
                phase.before(cloud, work)
            version = install(config, args.old if phase.version == "old" else args.new, cloud.url)
            result = os.path.join(work, "result-%s.json" % phase.name)
            log_path = os.path.join(work, "hass-%s.log" % phase.name)
            env = dict(os.environ, SIM_SCENARIO=name, SIM_PHASE=phase.name, SIM_HA_RESULT=result, SIM_WORK=work,
                       SIM_CLOUD_URL=cloud.url)
            print("\n== %s (integration %s)" % (label, version), flush=True)
            if phase.during:
                threading.Thread(target=phase.during, args=(cloud, work), daemon=True).start()
            with open(log_path, "w", encoding="utf-8") as log:
                proc = subprocess.Popen([args.hass, "-c", config], stdout=log, stderr=subprocess.STDOUT, env=env,
                                        start_new_session=True)
                try:
                    proc.wait(phase.timeout)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
            checks = []
            try:
                with open(result, encoding="utf-8") as fh:
                    checks = json.load(fh)
            except (OSError, ValueError):
                checks = [{"name": "Home Assistant ran and the test driver wrote its result", "ok": False,
                           "detail": "no result file - see %s" % log_path}]
            with open(log_path, encoding="utf-8", errors="replace") as fh:
                log_text = fh.read()
            bad = problems(log_text, phase.allow + (OLD_DEPRECATIONS if phase.version == "old" else []))
            checks.append({"name": "no unexpected error or warning in the log of Home Assistant", "ok": not bad,
                           "detail": None if not bad else "\n      ".join(bad[:12])})
            for pattern in phase.require:
                checks.append({"name": "the log says: %s" % pattern, "ok": re.search(pattern, log_text) is not None,
                               "detail": "not found"})
            for pattern in phase.forbid:
                hit = re.search(pattern, log_text)
                checks.append({"name": "the log does not say: %s" % pattern, "ok": hit is None,
                               "detail": hit.group(0) if hit else None})
            for c in checks:
                observation = c["name"].startswith("OBSERVATION")
                print(("  PASS " if c["ok"] else "  FAIL ") + c["name"]
                      + ("  <<< " + str(c["detail"]) if (not c["ok"] or observation) else ""), flush=True)
                rows.append(dict(c, scenario=name, phase=phase.name, version=version))
                ok = ok and c["ok"]
            if not all(c["ok"] for c in checks):
                print("  (stopping this scenario; work folder %s)" % work, flush=True)
                break
    finally:
        cloud.stop()
        if ok and not args.keep:
            shutil.rmtree(work, ignore_errors=True)
        else:
            print("  work folder: %s" % work, flush=True)
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--old", required=True, help="custom_components/ambientika of the released version")
    ap.add_argument("--new", required=True, help="custom_components/ambientika of the version under test")
    ap.add_argument("--hass", default=os.environ.get("HASS", "hass"))
    ap.add_argument("--python", default=sys.executable, help="python with aiohttp, for the stand-in")
    ap.add_argument("--only", default="", help="comma-separated scenarios")
    ap.add_argument("--json", default="", help="write all results to this file")
    ap.add_argument("--keep", action="store_true", help="keep the work folders")
    args = ap.parse_args()
    names = [n for n in args.only.split(",") if n] or list(SCENARIOS)
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        ap.error("unknown scenario: %s" % ", ".join(unknown))
    rows, failed = [], []
    for name in names:
        if not run_scenario(name, args, rows):
            failed.append(name)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=1, ensure_ascii=False)
    bad = [r for r in rows if not r["ok"]]
    observations = [r for r in rows if r["name"].startswith("OBSERVATION")]
    print("\nRESULT: %d scenarios, %d checks (%d of them observations), %s"
          % (len(names), len(rows), len(observations), "ALL PASS" if not bad else "FAILED %d in %s" % (len(bad), failed)))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
