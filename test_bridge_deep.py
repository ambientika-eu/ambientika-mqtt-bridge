#!/usr/bin/env python3
"""Funktions-/Smoke-Test der bridge.py.

Laedt die echte bridge.py und prueft die Kernlogik gegen gemockte Ambientika-
Cloud + MQTT-Broker: HA-Discovery (inkl. zone_index/last_operating_mode),
Taupunkt-Mathe, Config-Parsing, NeuraCell-X (Radon/Taupunkt inkl. Baseline-
Restore + Prioritaet), der reale State-Payload-Pfad und das Command-Handling.

Standalone lauffaehig (CI):  python test_bridge_deep.py   (Exit != 0 bei Fehler)
Keine echte Cloud-/Broker-Verbindung noetig.
"""
import asyncio
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# Kommandos werden im Betrieb ueber ein kurzes Fenster zusammengefasst. Fuer die
# Einzelpruefungen unten ist das Fenster aus, damit sie deterministisch bleiben;
# das Zusammenfassen selbst prueft test_command_coalescing() gezielt.
os.environ.setdefault("COMMAND_COALESCE_MS", "0")
_spec = importlib.util.spec_from_file_location("bridge", os.path.join(HERE, "bridge.py"))
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)

OM = bridge.OperatingMode
FS = bridge.FanSpeed
HL = bridge.HumidityLevel
LS = bridge.LightSensorLevel
from returns.result import Success  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else "  <<< " + str(extra)))
    if not cond:
        FAILS.append(name)


# --------------------------------------------------------------------------- Fakes
class FakeClient:
    def __init__(self):
        self.pub = []

    def publish(self, t, p, qos=0, retain=False):
        self.pub.append((t, p))

    def subscribe(self, *a, **k):
        pass

    def username_pw_set(self, *a, **k):
        pass

    def tls_set(self, *a, **k):
        pass

    def connect(self, *a, **k):
        pass

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        pass


def mkstatus(op=OM.MasterSlaveFlow, last=OM.Night, fan=FS.Medium, hum=HL.Normal):
    return {
        "operating_mode": op, "fan_speed": fan, "humidity_level": hum,
        "light_sensor_level": LS.Off, "temperature": 22, "humidity": 55,
        "air_quality": "Good", "humidity_alarm": False, "filters_status": "Green",
        "night_alarm": False, "device_role": "Slave", "last_operating_mode": last,
        "packet_type": "P", "device_type": "SMART", "device_serial_number": "AMB-2",
    }


class FakeDevice:
    def __init__(self, serial="AMB-2", name="Kitchen", zone=2, status=None):
        self.serial_number = serial
        self.name = name
        self.zone_index = zone
        self.role = "Slave"
        self._status = status or mkstatus()
        self.mode_calls = []
        self.reset_calls = 0

    async def status(self):
        return Success(dict(self._status))

    async def change_mode(self, mode):
        self.mode_calls.append(mode)
        self._status["operating_mode"] = mode["operating_mode"]
        self._status["fan_speed"] = mode["fan_speed"]
        self._status["humidity_level"] = mode["humidity_level"]
        return Success(None)

    async def reset_filter(self):
        self.reset_calls += 1
        return Success(None)


# --------------------------------------------------------------------------- Tests
def test_discovery():
    cfg = bridge.BridgeConfig()
    ents = bridge.build_discovery_configs(cfg, "AMB-2", "Kitchen")
    topics = [t for t, _ in ents]
    payloads = [p for _, p in ents]
    uids = [p["unique_id"] for p in payloads]
    check("discovery: last_operating_mode sensor present",
          any("last_operating_mode" in t and "/sensor/" in t for t in topics), topics[:3])
    check("discovery: zone_index sensor present",
          any("zone_index" in t and "/sensor/" in t for t in topics))
    check("discovery: unique_ids unique", len(uids) == len(set(uids)))
    check("discovery: all payloads JSON-serialisable", all(json.dumps(p) for p in payloads))
    mode_sel = next((p for t, p in ents if t.endswith("AMB-2_operating_mode/config") and "/select/" in t), None)
    check("discovery: operating_mode select has all modes",
          mode_sel and mode_sel["options"] == [m.name for m in OM], mode_sel and len(mode_sel["options"]))
    btn = next((p for t, p in ents if t.endswith("AMB-2_reset_filter/config") and "/button/" in t), None)
    check("discovery: reset_filter button present + command_topic",
          bool(btn) and btn["command_topic"].endswith("/AMB-2/set/reset_filter"), btn)
    aqn = next((p for t, p in ents if t.endswith("AMB-2_air_quality_num/config") and "/sensor/" in t), None)
    check("discovery: air_quality_num sensor + state_class=measurement",
          bool(aqn) and aqn.get("state_class") == "measurement", aqn)


def test_dewpoint():
    dp = bridge.dew_point_c(20.0, 50.0)
    check("dew_point_c(20,50) ~ 9.3", abs(dp - 9.27) < 0.15, dp)
    check("dew_point_c(25,100) ~ 25", abs(bridge.dew_point_c(25, 100) - 25) < 0.3)


def test_config():
    for k in ("AMBIENTIKA_USERNAME", "MQTT_PORT", "POLL_INTERVAL", "RADON_THRESHOLD",
              "RADON_PROTECTION_FAN", "DEWPOINT_DEVICE_BLOCK_MODES"):
        os.environ.pop(k, None)
    os.environ.update({
        "AMBIENTIKA_USERNAME": "u@x.de", "MQTT_PORT": "1885", "POLL_INTERVAL": "15",
        "RADON_THRESHOLD": "250", "RADON_PROTECTION_FAN": "Medium",
        "DEWPOINT_DEVICE_BLOCK_MODES": "Off,Night",
    })
    c = bridge.BridgeConfig.from_env()
    check("config: env username/port/poll/threshold",
          c.username == "u@x.de" and c.mqtt_port == 1885 and c.poll_interval == 15 and c.radon_threshold == 250,
          (c.username, c.mqtt_port, c.poll_interval, c.radon_threshold))
    check("config: radon_protection_fan_speed prop", c.radon_protection_fan_speed == FS.Medium)
    check("config: dewpoint_device_block_mode_set", c.dewpoint_device_block_mode_set == {OM.Off, OM.Night})


async def test_neuracell():
    cfg = bridge.BridgeConfig()
    cfg.radon_threshold = 300
    cfg.radon_hysteresis = 50
    b = bridge.AmbientikaBridge(cfg)
    b.client = FakeClient()
    b.loop = asyncio.get_running_loop()
    dev = FakeDevice()
    b.devices = {dev.serial_number: dev}
    nc = b.neuracell
    await nc.on_radon_value("500")
    check("neuracell: radon 500 -> radon_active", nc.radon_active is True)
    check("neuracell: device set to Intake/Low",
          dev.mode_calls and dev.mode_calls[-1]["operating_mode"] == bridge.RADON_PROTECTION_MODE
          and dev.mode_calls[-1]["fan_speed"] == FS.Low, dev.mode_calls[-1:])
    check("neuracell: baseline saved (MasterSlaveFlow)",
          nc._saved_modes.get("AMB-2", {}).get("operating_mode") == OM.MasterSlaveFlow)
    await nc.on_radon_value("100")
    check("neuracell: radon 100 -> radon_active False", nc.radon_active is False)
    check("neuracell: baseline restored", dev._status["operating_mode"] == OM.MasterSlaveFlow)
    check("neuracell: saved_modes cleared after restore", not nc._saved_modes)
    dev.mode_calls.clear()
    await nc.on_dewpoint_block("ON")
    check("neuracell: dewpoint block -> device Off", dev._status["operating_mode"] == OM.Off)
    await nc.on_dewpoint_block("OFF")
    check("neuracell: dewpoint release -> restore", dev._status["operating_mode"] == OM.MasterSlaveFlow)
    await nc.on_dewpoint_block("ON")
    await nc.on_radon_value("500")
    d = nc._desired(mkstatus())
    check("neuracell: radon has priority (desired=Intake)", d[0] == bridge.RADON_PROTECTION_MODE, d)


async def test_neuracell_scoped():
    """Radon + Taupunktsperre nur fuer die Keller-OFFICE (Praxisfall).

    Radon hat Vorrang. Endet der Radonschutz, waehrend die TPS noch sperrt,
    muessen die uebrigen Geraete sofort auf ihren eigenen Betrieb zurueck -
    nicht erst, wenn auch die TPS freigibt.
    """
    from returns.result import Failure

    def setup():
        cfg = bridge.BridgeConfig()
        cfg.radon_threshold = 100
        cfg.radon_hysteresis = 10
        cfg.dewpoint_block_devices = "OFF1,OFF2"
        b = bridge.AmbientikaBridge(cfg)
        b.client = FakeClient()
        b.loop = asyncio.get_running_loop()
        o1 = FakeDevice("OFF1", "Keller links", 1, mkstatus(op=OM.Smart))
        o2 = FakeDevice("OFF2", "Keller rechts", 1, mkstatus(op=OM.Smart))
        sm = FakeDevice("SM1", "Eltern", 3, mkstatus(op=OM.ManualHeatRecovery, fan=FS.Low))
        b.devices = {d.serial_number: d for d in (o1, o2, sm)}
        return b, b.neuracell, o1, o2, sm

    def mode(d):
        return d._status["operating_mode"]

    RP = bridge.RADON_PROTECTION_MODE

    # A: TPS sperrt -> Radon -> Radon weg -> TPS frei
    b, nc, o1, o2, sm = setup()
    await nc.on_dewpoint_block("ON")
    check("scoped A: TPS-Sperre nur OFFICE aus", mode(o1) == OM.Off and mode(o2) == OM.Off)
    check("scoped A: SMART bleibt unberuehrt", mode(sm) == OM.ManualHeatRecovery and not sm.mode_calls)
    await nc.on_radon_value("150")
    check("scoped A: Radon -> alle Zuluft (Vorrang vor TPS)",
          mode(o1) == RP and mode(o2) == RP and mode(sm) == RP)
    await nc.on_radon_value("50")
    check("scoped A: Radon weg, TPS sperrt -> OFFICE aus", mode(o1) == OM.Off and mode(o2) == OM.Off)
    check("scoped A: Radon weg -> SMART sofort zurueck",
          mode(sm) == OM.ManualHeatRecovery and sm._status["fan_speed"] == FS.Low, sm._status)
    check("scoped A: nur OFFICE-Baseline bleibt gemerkt", set(nc._saved_modes) == {"OFF1", "OFF2"})
    await nc.on_dewpoint_block("OFF")
    check("scoped A: TPS frei -> OFFICE zurueck auf Smart", mode(o1) == OM.Smart and mode(o2) == OM.Smart)
    check("scoped A: keine Baseline mehr offen", not nc._saved_modes)

    # B: Radon -> TPS sperrt -> TPS frei (waehrend Radon) -> Radon weg
    b, nc, o1, o2, sm = setup()
    await nc.on_radon_value("150")
    await nc.on_dewpoint_block("ON")
    check("scoped B: TPS-Sperre waehrend Radon aendert nichts",
          mode(o1) == RP and mode(o2) == RP and mode(sm) == RP)
    await nc.on_dewpoint_block("OFF")
    check("scoped B: TPS-Freigabe waehrend Radon aendert nichts",
          mode(o1) == RP and mode(o2) == RP and mode(sm) == RP)
    await nc.on_radon_value("50")
    check("scoped B: Radon weg -> alle auf ihren Betrieb",
          mode(o1) == OM.Smart and mode(o2) == OM.Smart and mode(sm) == OM.ManualHeatRecovery)

    # C: Radon -> TPS sperrt -> Radon weg -> TPS frei
    b, nc, o1, o2, sm = setup()
    await nc.on_radon_value("150")
    await nc.on_dewpoint_block("ON")
    await nc.on_radon_value("50")
    check("scoped C: Radon weg -> OFFICE aus, SMART zurueck",
          mode(o1) == OM.Off and mode(o2) == OM.Off and mode(sm) == OM.ManualHeatRecovery)
    await nc.on_dewpoint_block("OFF")
    check("scoped C: TPS frei -> OFFICE Smart (Baseline von vor Radon)",
          mode(o1) == OM.Smart and mode(o2) == OM.Smart)

    # D: Wert-Alarm und expliziter Alarm schalten sich nicht gegenseitig ab
    b, nc, o1, o2, sm = setup()
    await nc.on_radon_value("150")
    await nc.on_radon_alarm("OFF")
    check("scoped D: Alarm OFF hebt Wert-Alarm nicht auf", nc.radon_active and mode(sm) == RP)
    await nc.on_radon_alarm("ON")
    await nc.on_radon_value("50")
    check("scoped D: Wert sicher hebt Alarm ON nicht auf", nc.radon_active and mode(sm) == RP)
    await nc.on_radon_alarm("OFF")
    check("scoped D: beide aus -> Radonschutz aus, alle zurueck",
          not nc.radon_active and mode(sm) == OM.ManualHeatRecovery and mode(o1) == OM.Smart)

    # E: Restore schlaegt fehl (offline) -> Retry beim naechsten Poll,
    #    ein neuer Benutzerbefehl wird dabei nicht ueberschrieben
    b, nc, o1, o2, sm = setup()
    await nc.on_dewpoint_block("ON")
    await nc.on_radon_value("150")
    orig = sm.change_mode

    async def offline(m):
        return Failure("offline")
    sm.change_mode = offline
    await nc.on_radon_value("50")
    check("scoped E: SMART offline -> Baseline bleibt fuer Retry", "SM1" in nc._saved_modes)
    sm.change_mode = orig
    await b._queue_command(sm, {"operating_mode": OM.Night})
    await asyncio.sleep(0.05)
    check("scoped E: Benutzerbefehl live angewandt", mode(sm) == OM.Night, sm._status)
    await nc.enforce()
    check("scoped E: Retry setzt Benutzerwahl, nicht alten Modus", mode(sm) == OM.Night, sm._status)
    check("scoped E: Baseline nach Retry erledigt", "SM1" not in nc._saved_modes)
    check("scoped E: OFFICE weiter gesperrt", mode(o1) == OM.Off and mode(o2) == OM.Off)


async def test_payload():
    cfg = bridge.BridgeConfig()
    cfg.neuracell_enabled = False
    cfg.dewpoint_enabled = False
    cfg.enable_discovery = False
    b = bridge.AmbientikaBridge(cfg)
    b.client = FakeClient()
    b.loop = asyncio.get_running_loop()
    dev = FakeDevice(zone=3, status=mkstatus(op=OM.MasterSlaveFlow, last=OM.Night))
    b.devices = {dev.serial_number: dev}
    b._stop_event = asyncio.Event()
    task = asyncio.create_task(b._poll_loop())
    await asyncio.sleep(0.3)
    b._stop_event.set()
    await task
    states = [json.loads(p) for t, p in b.client.pub if t.endswith("/state")]
    check("payload: one state message published", len(states) >= 1, len(states))
    s = states[-1] if states else {}
    check("payload: contains zone_index=3", s.get("zone_index") == 3, s.get("zone_index"))
    check("payload: last_operating_mode=Night", s.get("last_operating_mode") == "Night", s.get("last_operating_mode"))
    check("payload: operating_mode=MasterSlaveFlow (effective)", s.get("operating_mode") == "MasterSlaveFlow")
    check("payload: device_role present", s.get("device_role") == "Slave")
    check("payload: filters_status string present", s.get("filters_status") == "Green")
    # numerische Begleitwerte (Zahl je Textwert) im state-Payload
    check("payload: operating_mode_num=9 (MasterSlaveFlow)", s.get("operating_mode_num") == OM.MasterSlaveFlow.value, s.get("operating_mode_num"))
    check("payload: last_operating_mode_num=3 (Night)", s.get("last_operating_mode_num") == OM.Night.value, s.get("last_operating_mode_num"))
    check("payload: fan_speed_num=2 (Medium->+1)", s.get("fan_speed_num") == FS.Medium.value + 1, s.get("fan_speed_num"))
    check("payload: humidity_level_num=1 (Normal)", s.get("humidity_level_num") == HL.Normal.value, s.get("humidity_level_num"))
    check("payload: light_sensor_level_num=1 (Off)", s.get("light_sensor_level_num") == LS.Off.value, s.get("light_sensor_level_num"))
    check("payload: air_quality_num=3 (Good)", s.get("air_quality_num") == 3, s.get("air_quality_num"))
    check("payload: filter_status_num=0 (Green)", s.get("filter_status_num") == 0, s.get("filter_status_num"))


async def test_command():
    cfg = bridge.BridgeConfig()
    cfg.neuracell_enabled = False
    cfg.dewpoint_enabled = False
    b = bridge.AmbientikaBridge(cfg)
    b.client = FakeClient()
    b.loop = asyncio.get_running_loop()
    dev = FakeDevice()
    b.devices = {dev.serial_number: dev}
    await b._handle_command("AMB-2", "operating_mode", "Night")
    check("command: operating_mode=Night -> change_mode",
          dev.mode_calls and dev.mode_calls[-1]["operating_mode"] == OM.Night, dev.mode_calls[-1:])
    await b._handle_command("AMB-2", "fan_speed", "High")
    check("command: fan_speed=High -> change_mode", dev.mode_calls[-1]["fan_speed"] == FS.High)
    await b._handle_command("AMB-2", "operating_mode", "Bogus")  # invalid -> no crash
    check("command: invalid value does not crash", True)
    # Filter-Reset: wird behandelt (kein Crash) und loest KEINEN change_mode aus.
    # Methoden-/DELETE-Sicherheit + Verifikation deckt test_filter_reset_diag.py ab.
    n_modes = len(dev.mode_calls)
    await b._handle_command("AMB-2", "reset_filter", "PRESS")
    check("command: reset_filter wird ohne Crash behandelt", True)
    check("command: reset_filter loest keinen change_mode aus", len(dev.mode_calls) == n_modes, len(dev.mode_calls))
    await b._handle_command("AMB-2", "reset_filter", "anything")  # Payload egal
    check("command: reset_filter zweiter Aufruf ohne change_mode", len(dev.mode_calls) == n_modes, len(dev.mode_calls))


async def test_command_coalescing():
    """Mehrere Kommandos kurz hintereinander -> EIN change_mode mit allen Werten.

    Deckt den Wettlauf ab, den Issue #4 beschreibt: frueher fuellte jedes
    Kommando die uebrigen Attribute aus dem gerade gelesenen Cloud-Status, der
    die vorige Aenderung noch nicht kannte - das zweite Kommando schrieb den
    alten Modus zurueck.
    """
    cfg = bridge.BridgeConfig()
    cfg.neuracell_enabled = False
    cfg.dewpoint_enabled = False
    b = bridge.AmbientikaBridge(cfg)
    b.client = FakeClient()
    b.loop = asyncio.get_running_loop()
    dev = FakeDevice()
    b.devices = {dev.serial_number: dev}

    alt = bridge.COMMAND_COALESCE_S
    bridge.COMMAND_COALESCE_S = 0.2
    try:
        await b._handle_command("AMB-2", "operating_mode", "MasterSlaveFlow")
        await b._handle_command("AMB-2", "fan_speed", "High")
        await b._handle_command("AMB-2", "humidity_level", "Moist")
        check("coalescing: noch kein change_mode vor Ablauf des Fensters",
              len(dev.mode_calls) == 0, len(dev.mode_calls))
        await asyncio.sleep(0.6)
        check("coalescing: genau ein change_mode", len(dev.mode_calls) == 1, len(dev.mode_calls))
        last = dev.mode_calls[-1] if dev.mode_calls else {}
        check("coalescing: Modus uebernommen", last.get("operating_mode") == OM.MasterSlaveFlow, last)
        check("coalescing: Luefterstufe uebernommen", last.get("fan_speed") == FS.High, last)
        check("coalescing: Feuchtestufe uebernommen", last.get("humidity_level") == HL.Moist, last)

        # Sammelkommando auf <prefix>/<serial>/set
        n = len(dev.mode_calls)
        await b._handle_command_set("AMB-2", {"mode": "Auto", "fanSpeed": "Low"})
        await asyncio.sleep(0.6)
        check("kombiniert: ein change_mode", len(dev.mode_calls) == n + 1, len(dev.mode_calls))
        last = dev.mode_calls[-1]
        check("kombiniert: Kurzname mode verstanden", last.get("operating_mode") == OM.Auto, last)
        check("kombiniert: Kurzname fanSpeed verstanden", last.get("fan_speed") == FS.Low, last)

        # Ungueltiger Wert verwirft das ganze Sammelkommando
        n = len(dev.mode_calls)
        await b._handle_command_set("AMB-2", {"mode": "Auto", "fanSpeed": "Bogus"})
        await asyncio.sleep(0.6)
        check("kombiniert: ungueltiger Wert verwirft alles", len(dev.mode_calls) == n, len(dev.mode_calls))

        # Unbekanntes Attribut wird ignoriert, der Rest wirkt
        n = len(dev.mode_calls)
        await b._handle_command_set("AMB-2", {"temperature": 22, "fanSpeed": "High"})
        await asyncio.sleep(0.6)
        check("kombiniert: unbekanntes Attribut uebersprungen", len(dev.mode_calls) == n + 1, len(dev.mode_calls))
        check("kombiniert: uebriges Attribut angewandt",
              dev.mode_calls[-1].get("fan_speed") == FS.High, dev.mode_calls[-1])
    finally:
        bridge.COMMAND_COALESCE_S = alt


async def _async_suite():
    await test_neuracell()
    await test_neuracell_scoped()
    await test_payload()
    await test_command()
    await test_command_coalescing()


def main():
    FAILS.clear()
    test_discovery()
    test_dewpoint()
    test_config()
    asyncio.run(_async_suite())
    print("\nRESULT:", "ALL PASS" if not FAILS else f"{len(FAILS)} FAIL -> {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
