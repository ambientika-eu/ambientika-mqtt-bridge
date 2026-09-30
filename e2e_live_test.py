#!/usr/bin/env python3
"""Live end-to-end test: real Mosquitto broker, full bridge.run() loop,
simulated Ambientika radon meter (MQTT mode 4) with Last Will + keepalive,
Vorsmann layout (2x OFFICE cellar, 5x SMART sleeping floor)."""
import asyncio, importlib.util, json, os, socket, subprocess, sys, tempfile, threading, time
import paho.mqtt.client as mqtt

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("bridge", os.path.join(HERE, "bridge.py"))
bridge = importlib.util.module_from_spec(spec); spec.loader.exec_module(bridge)
tmp = tempfile.mkdtemp()
bridge.NEURACELL_STATE_FILE = os.path.join(tmp, "nc.json")
bridge.PENDING_RESET_FILE = os.path.join(tmp, "res.json")
from returns.result import Success
OM, FS, HL, LS = bridge.OperatingMode, bridge.FanSpeed, bridge.HumidityLevel, bridge.LightSensorLevel

PORT = 18830
FAILS = []
def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else "  <<< " + str(extra)), flush=True)
    if not cond: FAILS.append(name)

class Dev:
    def __init__(self, serial, name, typ, op):
        self.serial_number, self.name, self.zone_index, self.role = serial, name, 0, "Master"
        self._s = {"operating_mode": op, "fan_speed": FS.Medium, "humidity_level": HL.Normal,
                   "light_sensor_level": LS.Off, "temperature": 21, "humidity": 55, "air_quality": "Good",
                   "humidity_alarm": False, "filters_status": "Green", "night_alarm": False,
                   "device_role": "Master", "last_operating_mode": op, "packet_type": "P",
                   "device_type": typ, "device_serial_number": serial}
    async def status(self): return Success(dict(self._s))
    async def change_mode(self, m):
        for k in ("operating_mode", "fan_speed", "humidity_level"):
            if k in m and m[k] is not None: self._s[k] = m[k]
        return Success(None)
    async def reset_filter(self): return Success(None)

def make_devices():
    return {d.serial_number: d for d in [
        Dev("FCB467C4B1E4", "Ambientika Office_rechts", "OFFICE", OM.Smart),
        Dev("FCB467C6EABC", "Ambientika Office Keller_links", "OFFICE", OM.Smart),
        Dev("441BF633AE58", "Kind schlafen", "SMART", OM.MasterSlaveFlow),
        Dev("441BF633B678", "Kind spielen", "SMART", OM.MasterSlaveFlow),
        Dev("A0F262D56A3C", "Büro L", "SMART", OM.Smart),
        Dev("441BF633FDC8", "Büro M", "SMART", OM.Smart),
        Dev("441BF633FDD4", "Eltern", "SMART", OM.Night)]}

OFFICE = {"FCB467C4B1E4", "FCB467C6EABC"}

class Spy:
    """Plays Home Assistant: keeps the last retained/non-retained message per topic."""
    def __init__(self):
        self.msgs, self.lock = {}, threading.Lock()
        self.c = mqtt.Client(client_id="ha-spy")
        self.c.on_message = self._m
        self.c.connect("127.0.0.1", PORT); self.c.subscribe("#"); self.c.loop_start()
    def _m(self, c, u, m):
        with self.lock: self.msgs[m.topic] = (m.payload.decode(), m.retain)
    def get(self, t):
        with self.lock: return self.msgs.get(t, (None, None))
    def nc(self):
        p, _ = self.get("ambientika/neuracell/state")
        return json.loads(p) if p else {}

class Meter:
    """Ambientika radon meter in MQTT mode 4 (topics as seen at the customer)."""
    ID = "Radon_D48C4958ECCC"
    def __init__(self, keepalive=3):
        self.c = mqtt.Client(client_id=self.ID)
        self.c.will_set(f"radon/{self.ID}/availability/state", "offline", retain=True)
        self.c.connect("127.0.0.1", PORT, keepalive=keepalive); self.c.loop_start()
        time.sleep(0.3)
        self.c.publish(f"radon/{self.ID}/availability/state", "online", retain=True)
    def value(self, v):
        self.c.publish(f"radon/{self.ID}/state", json.dumps({"mittelwert": v}), retain=True)
    def hang(self):
        """Simulate the field fault: firmware stops talking, socket stays silent."""
        self.c.loop_stop()          # no more PINGREQ -> broker keepalive expiry -> Last Will
    def clean_stop(self):
        self.c.loop_stop(); self.c.disconnect()

def retained(topic, timeout=3):
    """Read what a NEW subscriber (e.g. HA after restart) gets for a topic."""
    got = {}
    c = mqtt.Client(client_id="probe-%d" % time.time_ns())
    c.on_message = lambda cl, u, m: got.setdefault("m", (m.payload.decode(), m.retain))
    c.connect("127.0.0.1", PORT); c.subscribe(topic); c.loop_start()
    t0 = time.time()
    while "m" not in got and time.time() - t0 < timeout: time.sleep(0.05)
    c.loop_stop(); c.disconnect()
    return got.get("m", (None, None))

def wait(cond, timeout=15, step=0.2):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond(): return True
        time.sleep(step)
    return False

async def run_bridge(devs, stop_after):
    cfg = bridge.BridgeConfig()
    cfg.mqtt_host, cfg.mqtt_port, cfg.poll_interval = "127.0.0.1", PORT, 2
    cfg.radon_threshold, cfg.radon_hysteresis = 50, 10
    cfg.dewpoint_block_devices = "FCB467C4B1E4,FCB467C6EABC"
    b = bridge.AmbientikaBridge(cfg)
    async def login(): b.api = object()
    async def discover(): b.devices = devs
    b._login, b._discover_devices = login, discover
    task = asyncio.create_task(b.run())
    await stop_after(b)
    b.stop()
    try: await asyncio.wait_for(task, 10)
    except Exception: task.cancel()
    return b

def modes(devs): return {s: d._s["operating_mode"] for s, d in devs.items()}

async def scenario():
    broker = None if os.environ.get("E2E_EXTERNAL_BROKER") else subprocess.Popen(
        ["mosquitto", "-p", str(PORT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)
    spy = Spy(); time.sleep(0.3)
    devs = make_devices(); before = modes(devs)

    async def steps(b):
        loop = asyncio.get_running_loop()
        aw = lambda f, t=15: loop.run_in_executor(None, wait, f, t)
        print("\n[1] Bridge start, HA discovery")
        check("bridge online", await aw(lambda: spy.get("ambientika/bridge/availability")[0] == "online"))
        check("bridge online retained for new subscribers", retained("ambientika/bridge/availability") == ("online", 1))
        disc = retained("homeassistant/binary_sensor/neuracell_radon_meter_connected/config")
        cfgd = json.loads(disc[0]) if disc[0] else {}
        check("HA discovery 'Radon Meter Connected' retained", disc[1] == 1 and cfgd.get("device_class") == "connectivity", disc)
        check("discovery points to neuracell/state", cfgd.get("state_topic") == "ambientika/neuracell/state")
        check("before any meter: connected = None", await aw(lambda: "radon_meter_connected" in spy.nc()) and spy.nc()["radon_meter_connected"] is None, spy.nc())

        print("\n[2] Meter comes online, sends start-up 0")
        m = await loop.run_in_executor(None, Meter)
        check("connected = True", await aw(lambda: spy.nc().get("radon_meter_connected") is True), spy.nc())
        m.value(0); await asyncio.sleep(1.5)
        check("start-up 0 ignored (no source value)", not spy.nc().get("radon_sources"), spy.nc())
        check("no protection", spy.nc().get("radon_protection") is False)

        print("\n[3] 97 Bq/m3 -> radon protection on ALL 7 units")
        m.value(97)
        check("radon_protection True", await aw(lambda: spy.nc().get("radon_protection") is True), spy.nc())
        check("all 7 units Intake", await aw(lambda: all(d._s["operating_mode"] == bridge.RADON_PROTECTION_MODE for d in devs.values())), modes(devs))
        check("radon_value_current True", spy.nc().get("radon_value_current") is True)

        print("\n[4] 45 Bq/m3 (inside hysteresis 40..50) -> stays ON")
        m.value(45); await asyncio.sleep(1.5)
        check("still protected at 45", spy.nc().get("radon_protection") is True, spy.nc())

        print("\n[5] 34 Bq/m3 -> OFF, exact restore")
        m.value(34)
        check("radon_protection False", await aw(lambda: spy.nc().get("radon_protection") is False), spy.nc())
        check("exact previous modes restored", await aw(lambda: modes(devs) == before), modes(devs))

        print("\n[6] Field fault: meter firmware hangs -> broker Last Will (keepalive 3 s)")
        m.value(120)
        check("120 -> protection ON", await aw(lambda: spy.nc().get("radon_protection") is True))
        t0 = time.time(); m.hang()
        ok = await aw(lambda: spy.nc().get("radon_meter_connected") is False, 20)
        check("connected = False after LWT (%.1f s)" % (time.time() - t0), ok, spy.nc())
        check("LWT retained offline on availability topic", retained(f"radon/{Meter.ID}/availability/state") == ("offline", 1))
        check("meter value dropped from sources", Meter.ID not in json.dumps(spy.nc().get("radon_sources", {})), spy.nc())
        check("protection state kept while blind (safe side)", spy.nc().get("radon_protection") is True)
        check("radon_value_current False", spy.nc().get("radon_value_current") is False, spy.nc())

        print("\n[7] Meter power-cycled -> reconnects")
        m2 = await loop.run_in_executor(None, Meter)
        check("connected = True again", await aw(lambda: spy.nc().get("radon_meter_connected") is True), spy.nc())
        m2.value(0); await asyncio.sleep(1.5)
        check("post-restart 0 ignored, protection kept", spy.nc().get("radon_protection") is True, spy.nc())
        m2.value(30)
        check("30 -> OFF and restore", await aw(lambda: spy.nc().get("radon_protection") is False and modes(devs) == before), modes(devs))

        print("\n[8] Dew-point block only on the 2 OFFICE")
        spy.c.publish("ambientika/dewpoint/block", "ON", retain=True)
        ok = await aw(lambda: all(devs[s]._s["operating_mode"] == OM.Off for s in OFFICE))
        check("both OFFICE Off", ok, modes(devs))
        check("5 SMART untouched", all(devs[s]._s["operating_mode"] == before[s] for s in devs if s not in OFFICE), modes(devs))

        print("\n[9] Radon during dew-point block -> radon has priority")
        m2.value(200)
        check("all 7 Intake (radon > dew point)", await aw(lambda: all(d._s["operating_mode"] == bridge.RADON_PROTECTION_MODE for d in devs.values())), modes(devs))
        m2.value(20)
        check("radon clears -> OFFICE back to Off (block still on)", await aw(lambda: all(devs[s]._s["operating_mode"] == OM.Off for s in OFFICE)), modes(devs))
        check("SMART restored", all(devs[s]._s["operating_mode"] == before[s] for s in devs if s not in OFFICE), modes(devs))
        spy.c.publish("ambientika/dewpoint/block", "OFF", retain=True)
        check("block released -> everything exactly as before", await aw(lambda: modes(devs) == before), modes(devs))

        print("\n[10] Clean meter disconnect (no LWT) keeps last availability")
        m2.clean_stop(); await asyncio.sleep(1)
        check("still online (clean disconnect sends no LWT)", spy.nc().get("radon_meter_connected") is True)

    await run_bridge(devs, steps)
    time.sleep(1)
    check("bridge offline after clean stop (retained)", retained("ambientika/bridge/availability") == ("offline", 1), retained("ambientika/bridge/availability"))

    print("\n[11] Bridge restart with retained meter 'offline' on broker")
    Meter().hang()               # meter connects then hangs -> LWT offline retained
    time.sleep(6)
    devs2 = make_devices()
    async def steps2(b):
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(None, wait, lambda: spy.nc().get("radon_meter_connected") is False, 15)
        check("after restart: retained offline seen -> connected False", ok, spy.nc())
        check("retained old value not used while offline", spy.nc().get("radon_protection") is False, spy.nc())
    await run_bridge(devs2, steps2)

    spy.c.loop_stop()
    if broker: broker.terminate()

asyncio.run(scenario())
print("\nRESULT:", "ALL PASS" if not FAILS else "FAILED: %s" % FAILS)
sys.exit(1 if FAILS else 0)
