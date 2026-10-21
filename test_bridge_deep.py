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
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# Kommandos werden im Betrieb ueber ein kurzes Fenster zusammengefasst. Fuer die
# Einzelpruefungen unten ist das Fenster aus, damit sie deterministisch bleiben;
# das Zusammenfassen selbst prueft test_command_coalescing() gezielt.
os.environ.setdefault("COMMAND_COALESCE_MS", "0")
_spec = importlib.util.spec_from_file_location("bridge", os.path.join(HERE, "bridge.py"))
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)
import tempfile as _tempfile  # noqa: E402
# Tests must never write the real /data state file.
bridge.NEURACELL_STATE_FILE = os.path.join(_tempfile.mkdtemp(), "nc_state.json")
bridge.PENDING_RESET_FILE = os.path.join(_tempfile.mkdtemp(), "resets.json")

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
        self.pubr = []          # (topic, payload, retain)

    def publish(self, t, p, qos=0, retain=False):
        self.pub.append((t, p))
        self.pubr.append((t, p, retain))

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
        self._status = status or mkstatus()
        self.role = "Slave"
        self.mode_calls = []
        self.reset_calls = 0

    # Like a real unit: the role from discovery and the role the unit reports in
    # its status (device_role) agree unless a test sets them apart on purpose.
    @property
    def role(self):
        return self._role

    @role.setter
    def role(self, value):
        self._role = value
        self._status["device_role"] = value

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


async def test_neuracell_robust():
    """1.4.23: Races, offline-Geraete, Neustart waehrend Schutz, Alarm-Topic abschaltbar."""
    from returns.result import Failure
    grace = bridge.STARTUP_GRACE_S
    bridge.STARTUP_GRACE_S = 0.0

    def setup(state_file_keep=False):
        if not state_file_keep and os.path.exists(bridge.NEURACELL_STATE_FILE):
            os.remove(bridge.NEURACELL_STATE_FILE)
        cfg = bridge.BridgeConfig()
        cfg.radon_threshold = 100
        cfg.radon_hysteresis = 10
        cfg.dewpoint_block_devices = "OFF1"
        b = bridge.AmbientikaBridge(cfg)
        b.client = FakeClient()
        b.loop = asyncio.get_running_loop()
        o1 = FakeDevice("OFF1", "Keller", 1, mkstatus(op=OM.Smart))
        sm = FakeDevice("SM1", "Eltern", 3, mkstatus(op=OM.ManualHeatRecovery, fan=FS.Low))
        b.devices = {d.serial_number: d for d in (o1, sm)}
        return b, b.neuracell, o1, sm

    def mode(d):
        return d._status["operating_mode"]

    RP = bridge.RADON_PROTECTION_MODE

    # F: Benutzerbefehl waehrend der Restore gerade laeuft -> Benutzer gewinnt
    b, nc, o1, sm = setup()
    await nc.on_dewpoint_block("ON")
    await nc.on_radon_value("150")
    gate = asyncio.Event()
    orig = sm.change_mode

    async def slow(m):
        await gate.wait()
        return await orig(m)
    sm.change_mode = slow
    t = asyncio.create_task(nc.on_radon_value("50"))
    await asyncio.sleep(0.01)
    sm.change_mode = orig
    c = asyncio.create_task(b._queue_command(sm, {"operating_mode": OM.Night}))
    await asyncio.sleep(0.01)
    gate.set()
    await t
    await c
    await nc.enforce()
    check("robust F: Befehl waehrend Restore bleibt (Night)", mode(sm) == OM.Night, sm._status)
    check("robust F: keine offene Baseline fuer SM1", "SM1" not in nc._saved_modes)

    # G: offline bei Schutzbeginn, Befehl zurueckgestellt, Schutz endet offline
    b, nc, o1, sm = setup()

    async def off_status():
        return Failure("offline")

    async def off_change(m):
        return Failure("offline")
    s_status, s_change = sm.status, sm.change_mode
    sm.status, sm.change_mode = off_status, off_change
    await nc.on_radon_value("150")
    await b._queue_command(sm, {"operating_mode": OM.Auto})
    check("robust G: Befehl zurueckgestellt", nc._pending_manual.get("SM1", {}).get("operating_mode") == OM.Auto)
    await nc.on_radon_value("50")
    sm.status, sm.change_mode = s_status, s_change
    await nc.enforce()
    check("robust G: nach Rueckkehr Befehl angewandt (Auto)", mode(sm) == OM.Auto, sm._status)
    check("robust G: nichts mehr offen", not nc._pending_manual and not nc._saved_modes)

    # J: Cloud meldet nach Restore noch Intake -> Teilbefehl darf Intake nicht zurueckschreiben
    b, nc, o1, sm = setup()
    await nc.on_radon_value("150")
    await nc.on_radon_value("50")
    check("robust J: restauriert", mode(sm) == OM.ManualHeatRecovery)
    sm._status["operating_mode"] = RP          # Cloud hinkt hinterher
    await b._queue_command(sm, {"fan_speed": FS.High})
    check("robust J: Teilbefehl nutzt restaurierten Modus", mode(sm) == OM.ManualHeatRecovery
          and sm._status["fan_speed"] == FS.High, sm._status)
    # K: Benutzer waehlt danach selbst Intake -> bleibt Intake
    await b._queue_command(sm, {"operating_mode": OM.Intake})
    await b._queue_command(sm, {"fan_speed": FS.Medium})
    check("robust K: eigene Wahl Intake bleibt", mode(sm) == OM.Intake and sm._status["fan_speed"] == FS.Medium,
          sm._status)

    # H: Alarm-Topic abschaltbar + Quellen im Status
    c1 = bridge.BridgeConfig()
    c1._apply_extras({"radon_alarm_topic": "none"}.get)
    c1.apply_env_overrides()
    check("robust H: radon_alarm_topic 'none' -> aus", c1.radon_alarm_topic == "")
    c2 = bridge.BridgeConfig()
    c2._apply_extras({}.get)
    c2.apply_env_overrides()
    check("robust H: Standard-Alarm-Topic bleibt", c2.radon_alarm_topic == "ambientika/radon/alarm")
    b, nc, o1, sm = setup()
    await nc.on_radon_value("150")
    st = [json.loads(p) for t, p in b.client.pub if t.endswith("neuracell/state")][-1]
    check("robust H: Status zeigt Quelle", st.get("radon_value_alarm") is True and st.get("radon_signal_alarm") is False, st)

    # I: Neustart waehrend Radonschutz -> Baseline von Platte, Rueckkehr in den alten Modus
    b, nc, o1, sm = setup()
    await nc.on_radon_value("150")
    check("robust I: Baseline gespeichert", os.path.exists(bridge.NEURACELL_STATE_FILE))
    dev_state = {d.serial_number: dict(d._status) for d in (o1, sm)}
    b2, nc2, o1b, smb = setup(state_file_keep=True)          # "Neustart"
    o1b._status.update(dev_state["OFF1"]); smb._status.update(dev_state["SM1"])
    nc2.load_persisted()
    check("robust I: angewandter Schutz mitgespeichert", nc2._applied.get("SM1", (None,))[0] == RP, nc2._applied)
    check("robust I: Baseline geladen", nc2._saved_modes.get("SM1", {}).get("operating_mode") == OM.ManualHeatRecovery)
    await nc2.on_radon_value("150")
    await nc2.on_radon_value("50")
    check("robust I: nach Neustart zurueck auf alten Modus", mode(smb) == OM.ManualHeatRecovery
          and mode(o1b) == OM.Smart, (smb._status, o1b._status))
    # I2: Neustart, Geraet wurde inzwischen per App umgestellt -> alte Baseline verworfen
    b, nc, o1, sm = setup()
    await nc.on_radon_value("150")
    b3, nc3, o1c, smc = setup(state_file_keep=True)
    smc._status["operating_mode"] = OM.Night                 # Nutzer hat umgestellt
    o1c._status["operating_mode"] = RP
    o1c._status["fan_speed"] = FS.Low                          # noch im Radonschutz (Zuluft Stufe 1)
    nc3.load_persisted()
    await nc3.enforce()
    check("robust I2: Umstellung per App bleibt", mode(smc) == OM.Night, smc._status)
    check("robust I2: OFFICE aus Schutzmodus zurueck", mode(o1c) == OM.Smart, o1c._status)
    # I3: kaputte oder uralte Datei stoert nicht
    with open(bridge.NEURACELL_STATE_FILE, "w") as f:
        f.write("{kaputt")
    b4, nc4, _, _ = setup(state_file_keep=True)
    nc4.load_persisted()
    check("robust I3: kaputte Datei ignoriert", not nc4._saved_modes)
    with open(bridge.NEURACELL_STATE_FILE, "w") as f:
        json.dump({"ts": 1, "saved_modes": {"SM1": {"operating_mode": "Night", "fan_speed": "Low",
                                                      "humidity_level": "Normal"}}}, f)
    b5, nc5, _, _ = setup(state_file_keep=True)
    nc5.load_persisted()
    check("robust I3: alte Datei (>7 Tage) ignoriert", not nc5._saved_modes)

    # I4: waehrend Stillstand per App ausgeschaltet (Off statt Zuluft) -> bleibt aus
    b, nc, o1, sm = setup()
    await nc.on_radon_value("150")
    b6, nc6, o1d, smd = setup(state_file_keep=True)
    smd._status["operating_mode"] = OM.Off
    o1d._status.update({"operating_mode": RP, "fan_speed": FS.Low})
    nc6.load_persisted()
    await nc6.enforce()
    check("robust I4: per App ausgeschaltet bleibt aus", mode(smd) == OM.Off, smd._status)
    # I5: Schonfrist nach Neustart - Baseline von Platte wird nicht sofort restauriert
    bridge.STARTUP_GRACE_S = 60.0
    b, nc, o1, sm = setup()
    await nc.on_radon_value("150")
    b7, nc7, o1e, sme = setup(state_file_keep=True)
    sme._status.update({"operating_mode": RP, "fan_speed": FS.Low})
    nc7.load_persisted()
    await nc7.enforce()
    check("robust I5: in der Schonfrist bleibt Zuluft", mode(sme) == RP, sme._status)
    nc7._started -= 61
    await nc7.enforce()
    check("robust I5: nach der Schonfrist restauriert", mode(sme) == OM.ManualHeatRecovery, sme._status)
    bridge.STARTUP_GRACE_S = 0.0
    # I6: gueltiges JSON mit falscher Form darf den Start nicht abbrechen
    for bad in ("null", "[]", "\"x\"", "{\"ts\": 9e99, \"saved_modes\": []}"):
        with open(bridge.NEURACELL_STATE_FILE, "w") as f:
            f.write(bad)
        b8, nc8, _, _ = setup(state_file_keep=True)
        try:
            nc8.load_persisted()
            ok = not nc8._saved_modes
        except Exception:
            ok = False
        check("robust I6: Datei %s ignoriert" % bad[:12], ok)


    # O: Feuchte-Sollwert Dry (Wert 0) ueberlebt einen Neustart
    b, nc, o1, sm = setup()
    sm._status.update({"operating_mode": OM.Auto, "fan_speed": FS.High, "humidity_level": HL.Dry})
    await nc.on_radon_value("150")
    b9, nc9, o1f, smf = setup(state_file_keep=True)
    nc9.load_persisted()
    check("robust O: Dry bleibt Dry", nc9._saved_modes["SM1"]["humidity_level"] == HL.Dry, nc9._saved_modes["SM1"])
    # P: Neustart, Radon schon weg, Teilbefehl in der Schonfrist -> kein Haengenbleiben in Zuluft
    bridge.STARTUP_GRACE_S = 60.0
    b, nc, o1, sm = setup()
    await nc.on_radon_value("150")
    b10, nc10, o1g, smg = setup(state_file_keep=True)
    smg._status.update({"operating_mode": RP, "fan_speed": FS.Low})
    nc10.load_persisted()
    await b10._queue_command(smg, {"fan_speed": FS.Medium})
    check("robust P: Teilbefehl setzt alten Modus + neue Stufe", mode(smg) == OM.ManualHeatRecovery
          and smg._status["fan_speed"] == FS.Medium, smg._status)
    check("robust P: Baseline erledigt", "SM1" not in nc10._saved_modes)
    bridge.STARTUP_GRACE_S = 0.0
    # Q: Geraet beim Start nicht gefunden -> Eintrag bleibt in der Datei
    b, nc, o1, sm = setup()
    await nc.on_radon_value("150")
    b11, nc11, o1h, smh = setup(state_file_keep=True)
    del b11.devices["SM1"]
    nc11.load_persisted()
    await nc11.on_radon_value("150")
    with open(bridge.NEURACELL_STATE_FILE) as f:
        kept = json.load(f)
    check("robust Q: fehlendes Geraet bleibt gespeichert", "SM1" in kept.get("saved_modes", {}), kept)

    # R: Befehl laeuft, waehrend Radon anspringt -> Baseline bleibt (mit Benutzerwunsch)
    b, nc, o1, sm = setup()
    await nc.on_radon_value("150")
    orig_cm = sm.change_mode

    async def fail_cm(m):
        return Failure("offline")
    sm.change_mode = fail_cm
    await nc.on_radon_value("50")                     # Restore scheitert -> Baseline offen
    check("robust R: Baseline offen", "SM1" in nc._saved_modes)
    gate = asyncio.Event()

    async def slow_cm2(m):
        await gate.wait()
        r = await orig_cm(m)
        sm._status.update({"operating_mode": RP, "fan_speed": FS.Low})   # Cloud meldet noch Zuluft
        return r
    sm.change_mode = slow_cm2
    cmd = asyncio.create_task(b._queue_command(sm, {"fan_speed": FS.Medium}))
    await asyncio.sleep(0.01)
    rad = asyncio.create_task(nc.on_radon_value("150"))
    await asyncio.sleep(0.01)
    gate.set()
    await cmd
    await rad
    sm.change_mode = orig_cm
    check("robust R: Baseline bleibt mit Benutzerwunsch", nc._saved_modes.get("SM1", {}).get("fan_speed") == FS.Medium
          and nc._saved_modes["SM1"]["operating_mode"] == OM.ManualHeatRecovery, nc._saved_modes.get("SM1"))
    await nc.on_radon_value("50")
    check("robust R: danach richtiger Modus", mode(sm) == OM.ManualHeatRecovery and sm._status["fan_speed"] == FS.Medium,
          sm._status)
    # S: verwaiste Eintraege verfallen nach 7 Tagen
    with open(bridge.NEURACELL_STATE_FILE, "w") as f:
        json.dump({"ts": __import__("time").time(), "orphans_since": {"GONE": 1},
                   "saved_modes": {"GONE": {"operating_mode": "Smart", "fan_speed": "Low", "humidity_level": "Normal"}}}, f)
    b12, nc12, _, _ = setup(state_file_keep=True)
    nc12.load_persisted()
    check("robust S: alte Waise verworfen", "GONE" not in nc12._orphans)
    # L: Kompatibilitaetswert FanSpeed 'Night' landet nie in einem Befehl
    b, nc, o1, sm = setup()
    sm._status.update({"operating_mode": OM.Night, "fan_speed": FS.Night})
    await nc.on_radon_value("150")
    check("robust L: Baseline ohne FanSpeed Night", nc._saved_modes["SM1"]["fan_speed"] == FS.Low,
          nc._saved_modes["SM1"])
    await nc.on_radon_value("50")
    check("robust L: Restore sendet gueltigen Wert", sm.mode_calls[-1]["fan_speed"] == FS.Low
          and mode(sm) == OM.Night, sm.mode_calls[-1:])

    # M: Befehle an zwei Geraete laufen parallel, gleiches Geraet nacheinander
    b, nc, o1, sm = setup()
    for d in (o1, sm):
        orig_cm = d.change_mode

        async def slow_cm(m, _o=orig_cm):
            await asyncio.sleep(0.3)
            return await _o(m)
        d.change_mode = slow_cm
    t0 = asyncio.get_running_loop().time()
    await asyncio.gather(b._queue_command(o1, {"fan_speed": FS.High}), b._queue_command(sm, {"fan_speed": FS.High}))
    dt = asyncio.get_running_loop().time() - t0
    check("robust M: zwei Geraete parallel (< 0.5 s)", dt < 0.5, dt)

    # N: Befehl, der waehrend eines laufenden Flush ankommt, geht nicht verloren
    alt = bridge.COMMAND_COALESCE_S
    bridge.COMMAND_COALESCE_S = 0.05
    try:
        b, nc, o1, sm = setup()
        async with nc.unit_lock("SM1"):
            await b._queue_command(sm, {"operating_mode": OM.Night})
            await asyncio.sleep(0.1)
            await b._queue_command(sm, {"fan_speed": FS.High})
        await asyncio.sleep(0.3)
        check("robust N: beide Attribute angekommen", mode(sm) == OM.Night and sm._status["fan_speed"] == FS.High,
              sm._status)
        check("robust N: Warteschlange leer", not b._pending_cmds.get("SM1"), b._pending_cmds)
    finally:
        bridge.COMMAND_COALESCE_S = alt
    bridge.STARTUP_GRACE_S = grace


async def test_radon_meter():
    """1.4.24: Ambientika Radon-Meter (MQTT-Modus 4) direkt, JSON, Wildcards, mehrere Quellen."""
    pn, pt = bridge._payload_number, bridge._payload_truthy
    check("meter: Zahl", pn("62") == 62.0 and pn("62,5") == 62.5)
    check("meter: JSON mittelwert", pn('{"mittelwert":62}', "mittelwert") == 62.0)
    check("meter: JSON Text-Zahl", pn('{"x":1,"mittelwert":"97"}', "mittelwert") == 97.0)
    check("meter: JSON einziger Zahlenwert", pn('{"wert":34}', "mittelwert") == 34.0)
    check("meter: JSON mehrdeutig -> None", pn('{"a":1,"b":2}', "mittelwert") is None)
    check("meter: Unsinn -> None", pn("abc") is None and pn('{"mittelwert":null}', "mittelwert") is None)
    check("meter: truthy klassisch", pt("ON") and pt("true") and pt("1") and not pt("OFF") and not pt("0"))
    check("meter: truthy JSON", pt('{"block":true}', "block") and not pt('{"block":false}', "block")
          and pt('{"sperre":1}') and not pt('{"a":1,"b":0}'))
    check("meter: Wildcard", bridge._topic_match("radon/+/state", "radon/Radon_D48C4958ECCC/state")
          and not bridge._topic_match("radon/+/state", "radon/Radon_D48C4958ECCC/availability/state"))
    check("meter: Availability-Filter", bridge._availability_filter("radon/+/state") == "radon/+/availability/state")
    c = bridge.BridgeConfig()
    c._apply_extras({"radon_meter_topic": "none"}.get)
    c.apply_env_overrides()
    check("meter: abschaltbar", c.radon_meter_topic == "")
    c2 = bridge.BridgeConfig()
    c2._apply_extras({}.get)
    c2.apply_env_overrides()
    check("meter: Standard aktiv", c2.radon_meter_topic == "radon/+/state" and c2.radon_value_key == "mittelwert")

    cfg = bridge.BridgeConfig()
    cfg.radon_threshold, cfg.radon_hysteresis = 100, 10
    b = bridge.AmbientikaBridge(cfg)
    b.client = FakeClient()
    b.loop = asyncio.get_running_loop()
    dev = FakeDevice(status=mkstatus(op=OM.Smart))
    b.devices = {dev.serial_number: dev}
    nc = b.neuracell
    pending = []
    b._dispatch = lambda coro: pending.append(coro)

    class Msg:
        def __init__(self, t, p):
            self.topic, self.payload = t, p.encode()

    async def mq(t, p):
        b._on_mqtt_message(None, None, Msg(t, p))
        while pending:
            await pending.pop(0)

    MT = "radon/Radon_D48C4958ECCC/state"
    AV = "radon/Radon_D48C4958ECCC/availability/state"
    await mq(AV, "online")
    await mq(MT, '{"mittelwert":0}')
    check("meter: Startwert 0 ignoriert", MT not in nc._radon_values)
    await mq(MT, '{"mittelwert":150}')
    check("meter: 150 vom Messgeraet -> Radonschutz", nc.radon_active and dev._status["operating_mode"] == bridge.RADON_PROTECTION_MODE)
    # Start liegt lange zurueck (relativ, denn auf frischen CI-Maschinen ist monotonic() klein)
    nc._meter_online[MT] = (True, __import__("time").monotonic() - bridge.RADON_METER_BOOT_IGNORE_S - 1)
    await mq(MT, '{"mittelwert":0}')
    check("meter: echte 0 spaeter zaehlt -> Schutz aus", not nc.radon_active and dev._status["operating_mode"] == OM.Smart)
    # zwei Quellen: hoechster aktueller Wert zaehlt
    await mq(MT, '{"mittelwert":50}')
    await mq("ambientika/radon/value", "150")
    check("meter: zweite Quelle hoeher -> Schutz an", nc.radon_active)
    await mq(MT, '{"mittelwert":40}')
    check("meter: hoeherer Wert der anderen Quelle zaehlt weiter", nc.radon_active and nc.last_radon == 150)
    v, t = nc._radon_values["value"]
    nc._radon_values["value"] = (v, t - bridge.RADON_VALUE_MAX_AGE_S - 1)   # veraltet
    await mq(MT, '{"mittelwert":40}')
    check("meter: veralteter Wert zaehlt nicht mehr -> Schutz aus", not nc.radon_active and nc.last_radon == 40)
    await mq(AV, "offline")
    check("meter: offline -> Wert entfernt", MT not in nc._radon_values)
    st = [json.loads(p) for t, p in b.client.pub if t.endswith("neuracell/state")][-1]
    check("meter: Status mit Quellen", "radon_sources" in st, st)
    check("meter: Verbindung offline im Status", st.get("radon_meter_connected") is False, st)
    await mq(AV, "online")
    st = [json.loads(p) for t, p in b.client.pub if t.endswith("neuracell/state")][-1]
    check("meter: Verbindung wieder online im Status", st.get("radon_meter_connected") is True, st)
    disc = [t for t, _c in bridge.build_neuracell_discovery(b.cfg)]
    check("meter: HA-Sensor Radon Meter Connected", any("neuracell_radon_meter_connected" in t for t in disc))
    check("1.4.28: NeuraCell-Entitaeten an die Bridge-Verfuegbarkeit gebunden",
          all(cfgp.get("availability_topic") == bridge.bridge_avail_topic(b.cfg.topic_prefix)
              for _t, cfgp in bridge.build_neuracell_discovery(b.cfg)))

    # Pruefer-Funde: NaN/Inf/Riesenzahlen, strenge Wahrheitswerte, Schluessel am Messgeraet
    check("meter: NaN/Inf abgelehnt", pn("nan") is None and pn("inf") is None
          and pn('{"mittelwert": NaN}', "mittelwert") is None and pn('{"mittelwert": Infinity}', "mittelwert") is None)
    check("meter: Riesenzahl abgelehnt", pn('{"mittelwert": ' + "9" * 400 + '}', "mittelwert") is None)
    check("meter: truthy wie frueher", not pt("2") and not pt("1.0") and not pt("-1") and not pt("NaN") and pt("1"))
    await mq(MT, '{"co2":800}')
    check("meter: fremdes JSON ohne mittelwert ignoriert", not nc.radon_active)
    await mq("ambientika/radon/value", "nan")
    await mq(MT, '{"mittelwert":400}')
    check("meter: NaN verdeckt keinen hohen Wert", nc.radon_active and nc.last_radon == 400)
    await mq(MT, '{"mittelwert":20}')
    # radon_topic versehentlich auf das Messgeraet gesetzt -> trotzdem Messgeraet-Regeln
    cfg.radon_topic = MT
    await mq(AV, "online")
    await mq(MT, '{"mittelwert":0}')
    check("meter: Routing - Messgeraet vor radon_topic (Startwert ignoriert)",
          nc._radon_values.get(MT, (None,))[0] != 0)
    cfg.radon_topic = "radon/#"
    await mq(AV, "offline")
    check("meter: radon/# verschluckt Availability nicht", nc._meter_online.get(MT, (None,))[0] is False)
    cfg.radon_topic = "ambientika/radon/value"
    # JSON auf Alarm- und Sperr-Topic
    await mq("ambientika/radon/alarm", '{"alarm":true}')
    check("meter: Alarm als JSON", nc._radon_signal_alarm)
    await mq("ambientika/radon/alarm", "OFF")
    cfg.dewpoint_block_key = "block"
    await mq("ambientika/dewpoint/block", '{"block":true,"temp":12.5}')
    check("meter: Sperre als JSON mit Schluessel", nc.dewpoint_block)
    await mq("ambientika/dewpoint/block", '{"block":false,"temp":12.5}')
    check("meter: Sperre JSON aus", not nc.dewpoint_block)


async def test_dewpoint_watch():
    """1.4.26: Verbindungsueberwachung der Taupunktsteuerung (LWT, Zeitgrenze, Cloud)."""
    import time as _t
    # Config: Standard aus, Werte, Abschalten, ungueltig
    c = bridge.BridgeConfig()
    c._apply_extras({}.get)
    c.apply_env_overrides()
    check("tps: Standard aus", c.dewpoint_availability_topic == "" and c.dewpoint_signal_timeout == 0
          and c.dewpoint_lost_action == "keep")
    c._apply_extras({"dewpoint_availability_topic": "taupunkt/TP_X/availability/state",
                     "dewpoint_signal_timeout": "15", "dewpoint_lost_action": "Release"}.get)
    check("tps: Werte uebernommen", c.dewpoint_availability_topic == "taupunkt/TP_X/availability/state"
          and c.dewpoint_signal_timeout == 15 and c.dewpoint_lost_action == "release")
    c._apply_extras({"dewpoint_availability_topic": "none", "dewpoint_signal_timeout": -5,
                     "dewpoint_lost_action": "explode"}.get)
    check("tps: none/negativ/ungueltig abgefangen", c.dewpoint_availability_topic == ""
          and c.dewpoint_signal_timeout == 0 and c.dewpoint_lost_action == "keep")
    os.environ["DEWPOINT_SIGNAL_TIMEOUT"] = "20"
    os.environ["DEWPOINT_LOST_ACTION"] = "block"
    os.environ["DEWPOINT_AVAILABILITY_TOPIC"] = "tp/+/availability/state"
    c3 = bridge.BridgeConfig.from_env()
    for k in ("DEWPOINT_SIGNAL_TIMEOUT", "DEWPOINT_LOST_ACTION", "DEWPOINT_AVAILABILITY_TOPIC"):
        del os.environ[k]
    check("tps: Umgebungsvariablen", c3.dewpoint_signal_timeout == 20 and c3.dewpoint_lost_action == "block"
          and c3.dewpoint_availability_topic == "tp/+/availability/state")
    import tempfile as _tf
    with _tf.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump({"ambientika_username": "u", "dewpoint_availability_topic": "taupunkt/+/availability/state",
                   "dewpoint_signal_timeout": 0, "dewpoint_lost_action": "release"}, fh)
    c4 = bridge.BridgeConfig.from_ha_options(fh.name)
    os.unlink(fh.name)
    check("tps: Add-on-Optionen (options.json)", c4.dewpoint_availability_topic == "taupunkt/+/availability/state"
          and c4.dewpoint_lost_action == "release" and c4.dewpoint_signal_timeout == 0)
    disc = [t for t, _c in bridge.build_neuracell_discovery(c)]
    check("tps: HA-Sensor Dew Point Controller Connected",
          any("neuracell_dewpoint_controller_connected" in t for t in disc))

    class Msg:
        def __init__(self, t, p, retain=False):
            self.topic, self.payload, self.retain = t, p.encode(), retain

    def mkbridge(**kw):
        cfg = bridge.BridgeConfig()
        cfg.dewpoint_block_devices = "OFF-1"
        for k, v in kw.items():
            setattr(cfg, k, v)
        b = bridge.AmbientikaBridge(cfg)
        b.client = FakeClient()
        b.loop = asyncio.get_running_loop()
        office = FakeDevice(serial="OFF-1", name="Office", status=mkstatus(op=OM.Smart))
        smart = FakeDevice(serial="SM-1", name="Eltern", status=mkstatus(op=OM.Night))
        b.devices = {office.serial_number: office, smart.serial_number: smart}
        pending = []
        b._dispatch = lambda coro: pending.append(coro)

        async def mq(t, p, retain=False):
            b._on_mqtt_message(None, None, Msg(t, p, retain))
            while pending:
                await pending.pop(0)
        return b, office, smart, mq

    def state(b):
        return [json.loads(p) for t, p in b.client.pub if t.endswith("neuracell/state")][-1]

    def silent_for(nc, minutes):
        """Die TPS hat seit `minutes` nichts gesendet (Verbindungsaufbau lag davor)."""
        nc._tps_last_seen = _t.monotonic() - minutes * 60
        nc._tps_watch_start = min(nc._tps_watch_start, nc._tps_last_seen)

    BT, AV = "ambientika/dewpoint/block", "taupunkt/TP_D48C4956EC10/availability/state"

    # A) ohne Ueberwachung: Verhalten wie bisher, Status None
    b, office, smart, mq = mkbridge()
    await mq(BT, "ON")
    check("tps: ohne Watch Sperre wirkt", office._status["operating_mode"] == OM.Off)
    check("tps: ohne Watch Status None", state(b).get("dewpoint_controller_connected") is None, state(b))

    # B) LWT, Standard keep: Sperre bleibt, Status False, wieder online -> True
    b, office, smart, mq = mkbridge(dewpoint_availability_topic=AV)
    await mq(AV, "online")
    check("tps: LWT online -> True", state(b).get("dewpoint_controller_connected") is True, state(b))
    await mq(BT, "ON")
    await mq(AV, "offline")
    check("tps: LWT offline -> False", state(b).get("dewpoint_controller_connected") is False, state(b))
    check("tps: keep -> Sperre bleibt", b.neuracell.dewpoint_block and office._status["operating_mode"] == OM.Off)
    await mq(AV, "online")
    check("tps: wieder online -> True", state(b).get("dewpoint_controller_connected") is True, state(b))
    await mq(BT, "OFF")
    check("tps: danach normale Freigabe, exakt wie vorher", office._status["operating_mode"] == OM.Smart
          and smart._status["operating_mode"] == OM.Night)

    # C) LWT + release: Sperre wird freigegeben, OFFICE zurueck
    b, office, smart, mq = mkbridge(dewpoint_availability_topic=AV, dewpoint_lost_action="release")
    await mq(AV, "online")
    await mq(BT, "ON")
    check("tps: release - Sperre aktiv", office._status["operating_mode"] == OM.Off)
    await mq(AV, "offline")
    check("tps: release - bei Verlust freigegeben", not b.neuracell.dewpoint_block
          and office._status["operating_mode"] == OM.Smart, office._status)
    check("tps: release - Schlafetage unberuehrt", smart._status["operating_mode"] == OM.Night)
    await mq(BT, "ON", retain=True)
    check("tps: release - gespeicherte Sperre bei offline ignoriert", not b.neuracell.dewpoint_block
          and office._status["operating_mode"] == OM.Smart)
    check("tps: release - Status bleibt getrennt", state(b).get("dewpoint_controller_connected") is False)
    await mq(BT, "ON")
    check("tps: release - LIVE-Nachricht bei LWT offline wird angewendet, Status bleibt getrennt",
          b.neuracell.dewpoint_block and office._status["operating_mode"] == OM.Off
          and state(b).get("dewpoint_controller_connected") is False, state(b))
    await mq(AV, "online")
    check("tps: release - online -> verbunden, Sperre bleibt", b.neuracell.dewpoint_block
          and state(b).get("dewpoint_controller_connected") is True)
    await mq(BT, "OFF")
    check("tps: release - OFF gibt frei", not b.neuracell.dewpoint_block and office._status["operating_mode"] == OM.Smart)
    # Neustart-Fall: Broker liefert erst LWT offline, dann die alte gespeicherte Sperre (retain)
    b, office, smart, mq = mkbridge(dewpoint_availability_topic=AV, dewpoint_lost_action="release")
    await mq(AV, "offline")
    await mq(BT, "ON", retain=True)
    check("tps: Neustart - gespeicherte Sperre einer toten TPS greift nicht",
          not b.neuracell.dewpoint_block and office._status["operating_mode"] == OM.Smart)
    await mq(AV, "online")
    check("tps: Neustart - TPS wieder online -> ihre gespeicherte Sperre gilt",
          b.neuracell.dewpoint_block and office._status["operating_mode"] == OM.Off)
    # Neustart mit block: gespeicherte Freigabe einer toten TPS -> trotzdem gesperrt
    b, office, smart, mq = mkbridge(dewpoint_availability_topic=AV, dewpoint_lost_action="block")
    await mq(AV, "offline")
    await mq(BT, "OFF", retain=True)
    check("tps: Neustart block - tote TPS -> gesperrt", b.neuracell.dewpoint_block and office._status["operating_mode"] == OM.Off)
    await mq(AV, "online")
    check("tps: Neustart block - wieder online -> gespeicherte Freigabe gilt",
          not b.neuracell.dewpoint_block and office._status["operating_mode"] == OM.Smart)
    # Ueberlappende Filter: Sperr-Topic exakt, Verfuegbarkeit als Wildcard -> Sperre gewinnt
    b, office, smart, mq = mkbridge(dewpoint_availability_topic="ambientika/#", dewpoint_lost_action="release")
    b._subscribe_all(FakeClient())
    await mq("ambientika/tps/availability", "online")
    await mq(BT, "OFF")
    check("tps: Ueberlappung - OFF ist Freigabe, nicht offline",
          b.neuracell._tps_avail is True and not b.neuracell.dewpoint_block)
    await mq(BT, "ON")
    check("tps: Ueberlappung - ON sperrt", b.neuracell.dewpoint_block and office._status["operating_mode"] == OM.Off)
    check("tps: exakter Filter erkannt", bridge._is_exact_filter("a/b/c") and not bridge._is_exact_filter("a/+/c")
          and not bridge._is_exact_filter("a/#") and not bridge._is_exact_filter(""))
    # Payload-Varianten der Verfuegbarkeit
    pa = bridge.NeuraCellXController._parse_availability
    check("tps: Verfuegbarkeit Varianten", pa("online") is True and pa("true") is True and pa("1") is True
          and pa('{"state":"online"}') is True and pa("offline") is False and pa("false") is False
          and pa('{"state":"offline"}') is False and pa("kaputt") is None)
    b, office, smart, mq = mkbridge(dewpoint_availability_topic=AV, dewpoint_lost_action="release")
    await mq(AV, "kaputt")
    check("tps: unbekannte Verfuegbarkeit aendert nichts", b.neuracell._tps_avail is None)
    await mq(AV, "true")
    check("tps: 'true' zaehlt als online", b.neuracell.dewpoint_controller_connected() is True)
    # LWT online + Zeitgrenze: Zeitgrenze greift nicht, solange LWT online
    b, office, smart, mq = mkbridge(dewpoint_availability_topic=AV, dewpoint_signal_timeout=10,
                                    dewpoint_lost_action="release")
    await mq(AV, "online")
    await mq(BT, "ON")
    silent_for(b.neuracell, 60)
    await b.neuracell.check_dewpoint_link()
    check("tps: LWT online schlaegt Zeitgrenze", b.neuracell.dewpoint_block
          and b.neuracell.dewpoint_controller_connected() is True)
    # keep: wie bisher, Nachricht zaehlt (gespeichert und live)
    b, office, smart, mq = mkbridge(dewpoint_availability_topic=AV)
    await mq(AV, "offline")
    await mq(BT, "ON", retain=True)
    check("tps: keep - gespeicherte Sperre wirkt wie bisher", b.neuracell.dewpoint_block
          and state(b).get("dewpoint_controller_connected") is False)
    await mq(BT, "OFF")
    check("tps: keep - live-Nachricht wirkt, LWT bleibt massgeblich", not b.neuracell.dewpoint_block
          and state(b).get("dewpoint_controller_connected") is False)
    # Zeitgrenze: gespeicherte Nachricht beim Start zaehlt nicht als Lebenszeichen
    b, office, smart, mq = mkbridge(dewpoint_signal_timeout=10)
    await mq(BT, "ON", retain=True)
    check("tps: Zeitgrenze - gespeicherte Nachricht -> Status unbekannt, Sperre wirkt",
          b.neuracell.dewpoint_controller_connected() is None and b.neuracell.dewpoint_block)

    # D) LWT + block: bei Verlust Sperre an
    b, office, smart, mq = mkbridge(dewpoint_availability_topic=AV, dewpoint_lost_action="block")
    await mq(AV, "online")
    await mq(BT, "OFF")
    await mq(AV, "offline")
    check("tps: block - bei Verlust gesperrt", b.neuracell.dewpoint_block and office._status["operating_mode"] == OM.Off)
    check("tps: block - Schlafetage unberuehrt", smart._status["operating_mode"] == OM.Night)

    # E) Radon hat Vorrang auch bei Verlust mit block
    await mq("ambientika/radon/value", str(b.cfg.radon_threshold + 50))
    check("tps: Radon vor Verlust-Sperre", office._status["operating_mode"] == bridge.RADON_PROTECTION_MODE)
    await mq("ambientika/radon/value", "0")

    # F) Zeitgrenze ohne LWT
    b, office, smart, mq = mkbridge(dewpoint_signal_timeout=10)
    nc = b.neuracell
    check("tps: Zeitgrenze - am Anfang unbekannt", nc.dewpoint_controller_connected() is None)
    await mq(BT, "ON")
    check("tps: Zeitgrenze - Nachricht -> True", state(b).get("dewpoint_controller_connected") is True, state(b))
    silent_for(nc, 11)
    await nc.check_dewpoint_link()
    check("tps: Zeitgrenze abgelaufen -> False", state(b).get("dewpoint_controller_connected") is False, state(b))
    check("tps: Zeitgrenze keep -> Sperre bleibt", nc.dewpoint_block)
    await mq(BT, "ON")
    check("tps: neue Nachricht -> wieder True", state(b).get("dewpoint_controller_connected") is True, state(b))
    # nie gemeldet: ab Bridge-Start gerechnet
    b, office, smart, mq = mkbridge(dewpoint_signal_timeout=10)
    b.neuracell._tps_watch_start = _t.monotonic() - 11 * 60
    await b.neuracell.check_dewpoint_link()
    check("tps: nie gemeldet -> nach Zeitgrenze False", b.neuracell.dewpoint_controller_connected() is False)

    # G) Poll-Schleife prueft die Zeitgrenze selbst
    b, office, smart, mq = mkbridge(dewpoint_signal_timeout=10, dewpoint_lost_action="release")
    b.cfg.poll_interval = 1
    await mq(BT, "ON")
    silent_for(b.neuracell, 11)
    b._stop_event = asyncio.Event()
    task = asyncio.create_task(b._poll_loop())
    await asyncio.sleep(0.4)
    b._stop_event.set()
    await task
    check("tps: Poll-Schleife erkennt Verlust und gibt frei", not b.neuracell.dewpoint_block
          and office._status["operating_mode"] == OM.Smart, office._status)

    # I) Zeitgrenze: nach Verlust liefert der Broker beim Reconnect die alte Sperre nach (retain)
    b, office, smart, mq = mkbridge(dewpoint_signal_timeout=10, dewpoint_lost_action="release")
    nc = b.neuracell
    await mq(BT, "ON")
    silent_for(nc, 11)
    await nc.check_dewpoint_link()
    check("tps: Zeitgrenze - Verlust gibt frei", not nc.dewpoint_block and office._status["operating_mode"] == OM.Smart)
    b._on_mqtt_connect(b.client, None, None, 0)          # Reconnect zum Broker
    await mq(BT, "ON", retain=True)
    check("tps: Zeitgrenze - nachgelieferte alte Sperre nach Reconnect greift nicht",
          not nc.dewpoint_block and office._status["operating_mode"] == OM.Smart
          and state(b).get("dewpoint_controller_connected") is False, state(b))
    await mq(BT, "ON")
    check("tps: Zeitgrenze - live-Nachricht -> verbunden und Sperre gilt",
          nc.dewpoint_block and state(b).get("dewpoint_controller_connected") is True)
    # Zeitgrenze + block: nachgelieferte alte Freigabe greift nicht
    b, office, smart, mq = mkbridge(dewpoint_signal_timeout=10, dewpoint_lost_action="block")
    nc = b.neuracell
    await mq(BT, "OFF")
    silent_for(nc, 11)
    await nc.check_dewpoint_link()
    check("tps: Zeitgrenze block - Verlust sperrt", nc.dewpoint_block)
    await mq(BT, "OFF", retain=True)
    check("tps: Zeitgrenze block - nachgelieferte Freigabe greift nicht", nc.dewpoint_block)

    # J) Verfuegbarkeits-Topic + Zeitgrenze: Verlust ueber Zeitgrenze, dann meldet sich die TPS online
    b, office, smart, mq = mkbridge(dewpoint_availability_topic=AV, dewpoint_signal_timeout=10,
                                    dewpoint_lost_action="release")
    nc = b.neuracell
    await mq(BT, "ON")
    silent_for(nc, 11)
    await nc.check_dewpoint_link()
    check("tps: Zeitgrenze ohne LWT - Verlust gibt frei", not nc.dewpoint_block)
    await mq(AV, "online")
    check("tps: online nach Zeitgrenzen-Verlust -> gespeicherte Sperre gilt wieder",
          nc.dewpoint_block and office._status["operating_mode"] == OM.Off
          and state(b).get("dewpoint_controller_connected") is True, state(b))

    # K) Broker-Ausfall der Bridge selbst darf die Zeitgrenze nicht ausloesen
    b, office, smart, mq = mkbridge(dewpoint_signal_timeout=10, dewpoint_lost_action="block")
    nc = b.neuracell
    await mq(BT, "OFF")
    check("tps: Broker - vorher verbunden", nc.dewpoint_controller_connected() is True)
    b.client.is_connected = lambda: False
    silent_for(nc, 60)
    await nc.check_dewpoint_link()
    check("tps: Broker weg - Status eingefroren, keine Sperre", nc.dewpoint_controller_connected() is True
          and not nc.dewpoint_block and office._status["operating_mode"] == OM.Smart)
    b.client.is_connected = lambda: True
    b._on_mqtt_connect(b.client, None, None, 0)          # Reconnect: neue Frist
    check("tps: Broker zurueck - volle Frist, weiter verbunden", nc.dewpoint_controller_connected() is True)
    nc._tps_watch_start = _t.monotonic() - 11 * 60
    await nc.check_dewpoint_link()
    check("tps: nach Frist ohne Nachricht -> getrennt und gesperrt", nc.dewpoint_controller_connected() is False
          and nc.dewpoint_block)
    b._on_mqtt_connect(b.client, None, None, 0)
    check("tps: Reconnect weckt eine verlorene TPS nicht auf", nc.dewpoint_controller_connected() is False)
    # nie gemeldet: Reconnect verschiebt den Start der Frist
    b, office, smart, mq = mkbridge(dewpoint_signal_timeout=10)
    b.neuracell._tps_watch_start = _t.monotonic() - 9 * 60
    b._on_mqtt_connect(b.client, None, None, 0)
    b.neuracell._tps_watch_start -= 5 * 60     # 5 min nach dem Reconnect
    check("tps: nie gemeldet - Frist zaehlt ab Reconnect", b.neuracell.dewpoint_controller_connected() is None)

    # H) Quelle device (Cloud): Fehlversuche wie bei den Lueftern
    b, office, smart, mq = mkbridge(dewpoint_source="device", availability_failure_threshold=3)
    tps = FakeDevice(serial="TPS-1", name="TPS", status=mkstatus(op=OM.Smart))
    calls = {"fail": False}
    real = b.read_status

    async def rs(dev):
        return None if (dev is tps and calls["fail"]) else await real(dev)
    b.read_status = rs
    nc = b.neuracell
    check("tps: device - vor erstem Poll None", nc.dewpoint_controller_connected() is None)
    await nc.poll_dewpoint_device(tps)
    check("tps: device - erreichbar -> True", nc.dewpoint_controller_connected() is True)
    calls["fail"] = True
    await nc.poll_dewpoint_device(tps)
    await nc.poll_dewpoint_device(tps)
    check("tps: device - 2 Fehlversuche noch True", nc.dewpoint_controller_connected() is True)
    await nc.poll_dewpoint_device(tps)
    check("tps: device - 3 Fehlversuche -> False", state(b).get("dewpoint_controller_connected") is False, state(b))
    calls["fail"] = False
    await nc.poll_dewpoint_device(tps)
    check("tps: device - wieder erreichbar -> True", state(b).get("dewpoint_controller_connected") is True)


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


async def test_readonly_values():
    """1.4.29: Nur-Lese-Werte (FanSpeed Night/Turbo, Unbekanntes) nie an die Cloud.

    Kundenfall: Das Schlafetagen-Paket sendete nur den Modus, die Bridge fuellte
    die Luefterstufe aus dem Cloud-Status - und zwei Geraete meldeten gerade eine
    Stufe, die die Bridge nur als Kompatibilitaetswert (interne Nummer >= 900)
    kennt. Die Cloud antwortete HTTP 500 "Value was either too large or too
    small for an unsigned byte". Jetzt ersetzt die Bridge solche Werte durch den
    zuletzt akzeptierten Wert des Geraets, sonst durch den naechstliegenden.
    """
    # Turbo ist seit 1.4.29 ein bekannter Nur-Lese-Wert (API-Schema: Low, Medium,
    # High, Night, Turbo), Night wie bisher.
    check("readonly: FanSpeed.Turbo registriert (4)", getattr(FS, "Turbo", None) is not None and int(FS.Turbo) == 4)
    check("readonly: Turbo ist Kompatibilitaetswert", bridge.is_compat_member(FS, FS.Turbo))
    check("readonly: Turbo nicht als Befehl", bridge.strict_enum_lookup(FS, "Turbo") is None)
    check("readonly: fan_speed_num Night=4, Turbo=5",
          bridge._fan_speed_num(FS.Night) == 4 and bridge._fan_speed_num(FS.Turbo) == 5)
    # Unbekannte Werte: Text und Zahl (undefinierter Enum-Wert als JSON-Zahl)
    hl_unknown = HL["VeryMoist"]
    ls_num = LS[7]
    check("readonly: unbekannter Text registriert (>= 900)", int(hl_unknown) >= 900 and bridge.is_compat_member(HL, hl_unknown))
    check("readonly: Zahl als Schluessel crasht nicht, zweiter Zugriff gleicher Member",
          LS[7] is ls_num and LS["7"] is ls_num, (ls_num, LS["7"]))
    check("readonly: *_num fuer Platzhalter None", bridge._enum_num(hl_unknown) is None and bridge._enum_num(ls_num) is None)
    # Discovery: die Auswahl zeigt den Live-Wert, also stehen Night/Turbo mit drin
    opts = {p["name"]: p["options"] for t, p in bridge.build_discovery_configs(bridge.BridgeConfig(), "AMB-2", "K")
            if "/select/" in t}
    check("readonly: Auswahl Fan Speed enthaelt Low/Medium/High + Night/Turbo",
          {"Low", "Medium", "High", "Night", "Turbo"} <= set(opts.get("Fan Speed", [])), opts.get("Fan Speed"))

    cfg = bridge.BridgeConfig()
    cfg.neuracell_enabled = False
    cfg.dewpoint_enabled = False
    b = bridge.AmbientikaBridge(cfg)
    b.client = FakeClient()
    b.loop = asyncio.get_running_loop()

    # A) Geraet im Smart-Modus auf Turbo, noch nie eine andere Stufe gesehen:
    #    Modus-Befehl ohne Stufe -> keine 900 mehr, naechstliegende Stufe High.
    dev = FakeDevice(status=mkstatus(op=OM.Smart, fan=FS.Turbo))
    b.devices = {dev.serial_number: dev}
    await b._handle_command("AMB-2", "operating_mode", "ManualHeatRecovery")
    sent = dev.mode_calls[-1] if dev.mode_calls else {}
    check("readonly A: Befehl gesendet", bool(dev.mode_calls))
    check("readonly A: alle Werte Byte-tauglich (< 256)",
          all(0 <= int(v) < 256 for v in sent.values()), {k: int(v) for k, v in sent.items()})
    check("readonly A: Turbo -> High (kein frueherer Wert)", sent.get("fan_speed") == FS.High, sent.get("fan_speed"))
    check("readonly A: Modus wie befohlen", sent.get("operating_mode") == OM.ManualHeatRecovery)

    # B) Zuletzt akzeptierte Stufe bekannt (aus dem Poll): die wird genommen.
    dev = FakeDevice(status=mkstatus(op=OM.Smart, fan=FS.Low))
    b.devices = {dev.serial_number: dev}
    b._stop_event = asyncio.Event()
    task = asyncio.create_task(b._poll_loop())
    await asyncio.sleep(0.2)
    b._stop_event.set()
    await task
    check("readonly B: Poll merkt sich akzeptierte Stufe", b._last_sendable.get("AMB-2", {}).get("fan_speed") == FS.Low,
          b._last_sendable.get("AMB-2"))
    dev._status["fan_speed"] = FS.Night          # Geraet ist auf die Nachtstufe gegangen
    await b._handle_command("AMB-2", "operating_mode", "Smart")
    check("readonly B: Night -> zuletzt akzeptierte Stufe Low", dev.mode_calls[-1]["fan_speed"] == FS.Low, dev.mode_calls[-1])
    # Night ohne Vorwissen -> Low (leiseste sendbare Stufe)
    dev2 = FakeDevice(serial="AMB-3", status=mkstatus(op=OM.Night, fan=FS.Night))
    b.devices["AMB-3"] = dev2
    await b._handle_command("AMB-3", "humidity_level", "Dry")
    check("readonly B: Night ohne Vorwissen -> Low", dev2.mode_calls[-1]["fan_speed"] == FS.Low, dev2.mode_calls[-1])
    check("readonly B: befohlener Wert unveraendert", dev2.mode_calls[-1]["humidity_level"] == HL.Dry)

    # C) Ausdruecklich befohlene Stufe hat Vorrang vor allem.
    dev._status["fan_speed"] = FS.Turbo
    await b._handle_command_set("AMB-2", {"operating_mode": "Smart", "fan_speed": "Medium"})
    check("readonly C: befohlene Stufe gewinnt", dev.mode_calls[-1]["fan_speed"] == FS.Medium, dev.mode_calls[-1])
    check("readonly C: Erfolg aktualisiert Merker", b._last_sendable["AMB-2"]["fan_speed"] == FS.Medium)

    # D) Unbekannte Feuchte-/Lichtstufe im Status -> Normal / zuletzt gesehen bzw. Off
    dev3 = FakeDevice(serial="AMB-4", status=mkstatus(op=OM.Smart))
    dev3._status["humidity_level"] = hl_unknown
    dev3._status["light_sensor_level"] = LS["Bright"]
    b.devices["AMB-4"] = dev3
    await b._handle_command("AMB-4", "fan_speed", "High")
    sent = dev3.mode_calls[-1]
    check("readonly D: unbekannte Feuchtestufe -> Normal", sent["humidity_level"] == HL.Normal, sent)
    check("readonly D: unbekannte Lichtstufe -> Off", sent["light_sensor_level"] == LS.Off, sent)
    check("readonly D: alle Werte Byte-tauglich", all(0 <= int(v) < 256 for v in sent.values()))
    check("readonly D: Platzhalter nie in _last_light", "AMB-4" not in b._last_light, b._last_light)

    # E) Unbekannter Betriebsmodus im Status, Befehl nennt keinen Modus:
    #    ohne frueheren Wert wird nicht geraten (kein Befehl), mit -> dieser.
    dev4 = FakeDevice(serial="AMB-5", status=mkstatus(op=OM.Smart))
    dev4._status["operating_mode"] = OM["Holiday"]
    b.devices["AMB-5"] = dev4
    await b._handle_command("AMB-5", "fan_speed", "Low")
    check("readonly E: unbekannter Modus ohne Vorwissen -> kein Befehl", not dev4.mode_calls, dev4.mode_calls)
    b._last_sendable["AMB-5"] = {"operating_mode": OM.Auto}
    await b._handle_command("AMB-5", "fan_speed", "Low")
    check("readonly E: mit Vorwissen -> letzter Modus", dev4.mode_calls and dev4.mode_calls[-1]["operating_mode"] == OM.Auto,
          dev4.mode_calls[-1:])

    # G) Befehl MIT Nur-Lese-Wert (Home-Assistant-Szene stellt den Stand "Turbo"/
    #    "Night" wieder her): wird angenommen und beim Senden ersetzt, ein
    #    wirklich unbekannter Name wird weiterhin verworfen.
    dev6 = FakeDevice(serial="AMB-7", status=mkstatus(op=OM.MasterSlaveFlow, fan=FS.High))
    b.devices["AMB-7"] = dev6
    await b._handle_command_set("AMB-7", {"operating_mode": "Smart", "fan_speed": "Turbo"})
    sent = dev6.mode_calls[-1] if dev6.mode_calls else {}
    check("readonly G: Szene mit Turbo wird ausgefuehrt", bool(sent), dev6.mode_calls)
    check("readonly G: Turbo im Befehl -> High", sent.get("fan_speed") == FS.High and sent.get("operating_mode") == OM.Smart, sent)
    n = len(dev6.mode_calls)
    await b._handle_command("AMB-7", "fan_speed", "Night")
    check("readonly G: Night im Befehl -> Low", dev6.mode_calls[-1]["fan_speed"] == FS.Low and len(dev6.mode_calls) == n + 1, dev6.mode_calls[-1:])
    n = len(dev6.mode_calls)
    await b._handle_command("AMB-7", "fan_speed", "Bogus")
    check("readonly G: unbekannter Name weiterhin verworfen", len(dev6.mode_calls) == n)
    await b._handle_command_set("AMB-7", {"operating_mode": "Smart", "fan_speed": "Bogus"})
    check("readonly G: kombiniert mit unbekanntem Namen verworfen", len(dev6.mode_calls) == n)
    await b._handle_command("AMB-7", "light_sensor_level", "Bright")   # auto-registrierter Platzhalter
    check("readonly G: Platzhalter-Lichtstufe -> Off", dev6.mode_calls[-1]["light_sensor_level"] == LS.Off, dev6.mode_calls[-1])
    check("readonly G: Platzhalter nicht in _last_light", "AMB-7" not in b._last_light, b._last_light.get("AMB-7"))
    check("readonly G: Enum nicht erweitert", "Bogus" not in FS._member_map_ and "Bogus" not in bridge.COMPAT_ENUM_MEMBERS.get(FS, ()))

    # F) NeuraCell-Pfad (set_device_mode): Nur-Lese-Werte werden ebenso ersetzt.
    dev5 = FakeDevice(serial="AMB-6", status=mkstatus(op=OM.Smart, fan=FS.Turbo))
    dev5._status["light_sensor_level"] = LS["Bright"]
    b.devices["AMB-6"] = dev5
    ok = await b.set_device_mode(dev5, OM.Intake, FS.Night, HL.Normal)
    sent = dev5.mode_calls[-1] if dev5.mode_calls else {}
    check("readonly F: set_device_mode sendet", ok and bool(sent))
    check("readonly F: Night -> Low, Licht -> Off", sent.get("fan_speed") == FS.Low and sent.get("light_sensor_level") == LS.Off, sent)
    check("readonly F: Byte-tauglich", all(0 <= int(v) < 256 for v in sent.values()))


async def test_meter_watch():
    """1.4.27: Radon-Messgeraet - Messwert als Lebenszeichen, Zeitgrenze, Liste ueber Neustart."""
    import time as _t
    c = bridge.BridgeConfig()
    c._apply_extras({}.get)
    c.apply_env_overrides()
    check("mw: Standard 30 min", c.radon_meter_timeout == 30)
    c._apply_extras({"radon_meter_timeout": "45"}.get)
    check("mw: Wert uebernommen", c.radon_meter_timeout == 45)
    c._apply_extras({"radon_meter_timeout": -3}.get)
    check("mw: negativ -> 0 (aus)", c.radon_meter_timeout == 0)
    c._apply_extras({"radon_meter_timeout": "abc"}.get)
    check("mw: Unsinn -> unveraendert", c.radon_meter_timeout == 0)
    os.environ["RADON_METER_TIMEOUT"] = "12"
    c3 = bridge.BridgeConfig.from_env()
    del os.environ["RADON_METER_TIMEOUT"]
    check("mw: Umgebungsvariable", c3.radon_meter_timeout == 12)
    import tempfile as _tf
    with _tf.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump({"ambientika_username": "u", "radon_meter_timeout": 20}, fh)
    c4 = bridge.BridgeConfig.from_ha_options(fh.name)
    os.unlink(fh.name)
    check("mw: Add-on-Option", c4.radon_meter_timeout == 20)
    check("mw: Listen-Topic", bridge.neuracell_meters_topic("ambientika") == "ambientika/neuracell/meters")

    class Msg:
        def __init__(self, t, p, retain=False):
            self.topic, self.payload, self.retain = t, p.encode(), retain

    def mkbridge(**kw):
        cfg = bridge.BridgeConfig()
        cfg.radon_threshold, cfg.radon_hysteresis = 100, 10
        for k, v in kw.items():
            setattr(cfg, k, v)
        b = bridge.AmbientikaBridge(cfg)
        b.client = FakeClient()
        b.loop = asyncio.get_running_loop()
        dev = FakeDevice(status=mkstatus(op=OM.Smart))
        b.devices = {dev.serial_number: dev}
        pending = []
        b._dispatch = lambda coro: pending.append(coro)

        async def mq(t, p, retain=False):
            b._on_mqtt_message(None, None, Msg(t, p, retain))
            while pending:
                await pending.pop(0)
        return b, dev, mq

    def state(b):
        return [json.loads(p) for t, p in b.client.pub if t.endswith("neuracell/state")][-1]

    def meters_list(b):
        pubs = [p for t, p in b.client.pub if t.endswith("neuracell/meters")]
        return json.loads(pubs[-1]) if pubs else None

    MT = "radon/Radon_D48C4958ECCC/state"
    AV = "radon/Radon_D48C4958ECCC/availability/state"
    LT = "ambientika/neuracell/meters"

    # A) kein Messgeraet bekannt -> None, Watch tut nichts
    b, dev, mq = mkbridge()
    b.publish_neuracell_state()
    check("mw: kein Messgeraet -> None", state(b).get("radon_meter_connected") is None and state(b).get("radon_meters") == [])
    await b.neuracell.check_radon_meter_link()
    check("mw: Watch ohne Messgeraet ohne Wirkung", b.neuracell._meter_connected_reported is None)

    # B) Messwert ohne Anmeldung = Lebenszeichen (z. B. Neustart der Bridge bei laufendem Messgeraet)
    await mq(MT, '{"mittelwert":62}')
    check("mw: Messwert ohne 'online' -> verbunden", state(b).get("radon_meter_connected") is True, state(b))
    check("mw: Messgeraet in der Liste veroeffentlicht", meters_list(b) and MT in meters_list(b)["radon_meters"], meters_list(b))
    check("mw: Liste retained", any(t.endswith("neuracell/meters") for t, _p in b.client.pub))
    check("mw: Wert zaehlt", b.neuracell.last_radon == 62)
    await mq(MT, '{"mittelwert":0}')
    check("mw: erste 0 nach Lebenszeichen ignoriert", b.neuracell.last_radon == 62)

    # C) laeuft, dann still: Zeitgrenze -> getrennt, Wert verworfen, Schutzzustand bleibt
    await mq(MT, '{"mittelwert":150}')
    check("mw: 150 -> Radonschutz", b.neuracell.radon_active)
    nc = b.neuracell
    nc._meter_seen[MT] = nc._meter_ref[MT] = _t.monotonic() - 31 * 60
    nc._meter_online[MT] = (True, _t.monotonic() - 40 * 60)
    await nc.check_radon_meter_link()
    check("mw: 31 min still -> getrennt", state(b).get("radon_meter_connected") is False, state(b))
    check("mw: Einzelstatus gemeldet", nc._meter_state_reported.get(MT) is False)
    check("mw: Wert nach Zeitgrenze verworfen", MT not in nc._radon_values)
    check("mw: Schutzzustand bleibt (sichere Seite)", nc.radon_active)
    await nc.check_radon_meter_link()
    check("mw: keine Doppelmeldung", state(b).get("radon_meter_connected") is False)
    await mq(MT, '{"mittelwert":20}')
    check("mw: neuer Wert -> wieder verbunden", state(b).get("radon_meter_connected") is True, state(b))
    check("mw: 20 -> Schutz aus", not nc.radon_active)

    # D) Last Will offline bleibt sofort wirksam; ein Wert danach zaehlt wieder als online
    await mq(AV, "offline")
    check("mw: LWT offline -> getrennt", state(b).get("radon_meter_connected") is False)
    await mq(MT, '{"mittelwert":55}')
    check("mw: Wert nach offline -> verbunden (altes offline ueberstimmt)", state(b).get("radon_meter_connected") is True
          and nc._meter_online[MT][0] is True, state(b))
    await mq(AV, "online")
    check("mw: online bestaetigt", state(b).get("radon_meter_connected") is True)

    # E) Neustart: nur die retained Liste bekannt, nichts kommt -> nach Zeitgrenze getrennt
    b, dev, mq = mkbridge()
    nc = b.neuracell
    await mq(LT, json.dumps({"radon_meters": {MT: _t.time() - 3600}}), retain=True)
    check("mw: Liste gelesen", MT in nc._meter_known and state(b).get("radon_meters") == [MT], state(b))
    check("mw: vor Ablauf unbekannt (None)", state(b).get("radon_meter_connected") is None)
    nc._meter_ref[MT] = _t.monotonic() - 31 * 60
    await nc.check_radon_meter_link()
    check("mw: Neustart + 31 min ohne Nachricht -> getrennt", state(b).get("radon_meter_connected") is False, state(b))
    await mq(AV, "online")
    check("mw: Anmeldung -> verbunden", state(b).get("radon_meter_connected") is True)
    # eigene Liste kommt zurueck (wir sind selbst abonniert) -> keine Aenderung
    await mq(LT, nc.known_meters_payload(), retain=True)
    check("mw: eigene Liste unschaedlich", state(b).get("radon_meter_connected") is True and len(nc._meter_known) == 1)

    # F) Liste: alte Eintraege, fremde Topics, Unsinn
    b, dev, mq = mkbridge()
    nc = b.neuracell
    await mq(LT, json.dumps({"radon_meters": {MT: _t.time() - 8 * 86400, "radon/OLD/state": "x",
                                               "other/Y/state": _t.time(), "radon/Z/foo": _t.time()}}), retain=True)
    check("mw: >7 Tage alt / fremd / kaputt ignoriert", not nc._meter_known, nc._meter_known)
    await mq(LT, "not json", retain=True)
    await mq(LT, '{"radon_meters": [1,2]}', retain=True)
    check("mw: Unsinn in der Liste ignoriert", not nc._meter_known)
    nc._meter_known["radon/Q/state"] = _t.time() - 8 * 86400
    nc._meter_known["radon/R/state"] = _t.time()
    pl = json.loads(nc.known_meters_payload())
    check("mw: Liste laesst alte Geraete fallen", list(pl["radon_meters"]) == ["radon/R/state"], pl)

    # G) Zeitgrenze aus (0): nur der Last Will zaehlt
    b, dev, mq = mkbridge(radon_meter_timeout=0)
    nc = b.neuracell
    await mq(LT, json.dumps({"radon_meters": {MT: _t.time()}}), retain=True)
    nc._meter_ref[MT] = _t.monotonic() - 100 * 60
    await nc.check_radon_meter_link()
    check("mw: ohne Zeitgrenze bleibt None", state(b).get("radon_meter_connected") is None, state(b))
    await mq(MT, '{"mittelwert":40}')
    nc._meter_seen[MT] = nc._meter_ref[MT] = _t.monotonic() - 100 * 60
    await nc.check_radon_meter_link()
    check("mw: ohne Zeitgrenze bleibt verbunden", state(b).get("radon_meter_connected") is True)
    await mq(AV, "offline")
    check("mw: ohne Zeitgrenze LWT wirkt", state(b).get("radon_meter_connected") is False)

    # H) Bridge selbst ohne Broker: Zeitgrenze pausiert
    b, dev, mq = mkbridge()
    nc = b.neuracell
    await mq(MT, '{"mittelwert":40}')
    nc._meter_seen[MT] = nc._meter_ref[MT] = _t.monotonic() - 100 * 60
    b.client.is_connected = lambda: False
    await nc.check_radon_meter_link()
    check("mw: ohne Broker-Verbindung letzter Stand", state(b).get("radon_meter_connected") is True, state(b))
    b.client.is_connected = lambda: True
    nc.on_broker_connected()
    check("mw: Reconnect startet die Zeit neu (noch verbunden)", _t.monotonic() - nc._meter_ref[MT] < 5)
    await nc.check_radon_meter_link()
    check("mw: nach Reconnect volle Zeitgrenze, solange verbunden", state(b).get("radon_meter_connected") is True, state(b))
    nc._meter_ref[MT] = _t.monotonic() - 31 * 60
    await nc.check_radon_meter_link()
    check("mw: Zeitgrenze nach Reconnect abgelaufen -> getrennt", state(b).get("radon_meter_connected") is False, state(b))
    nc.on_broker_connected()
    check("mw: Reconnect bei getrennt startet nicht neu", _t.monotonic() - nc._meter_ref[MT] > 30 * 60)
    await nc.check_radon_meter_link()
    check("mw: bleibt getrennt bis eine Nachricht kommt", state(b).get("radon_meter_connected") is False)
    await mq(MT, '{"mittelwert":41}')
    check("mw: Nachricht -> wieder verbunden", state(b).get("radon_meter_connected") is True)

    # I) zwei Messgeraete: eines still -> getrennt
    b, dev, mq = mkbridge()
    nc = b.neuracell
    await mq(MT, '{"mittelwert":40}')
    await mq("radon/Radon_B/state", '{"mittelwert":41}')
    check("mw: zwei Messgeraete verbunden", state(b).get("radon_meter_connected") is True
          and state(b).get("radon_meters") == sorted([MT, "radon/Radon_B/state"]))
    nc._meter_seen["radon/Radon_B/state"] = nc._meter_ref["radon/Radon_B/state"] = _t.monotonic() - 31 * 60
    await nc.check_radon_meter_link()
    check("mw: eines still -> getrennt", state(b).get("radon_meter_connected") is False)
    check("mw: nur das stille Geraet verworfen", "radon/Radon_B/state" not in nc._radon_values and MT in nc._radon_values)
    nc._meter_seen[MT] = nc._meter_ref[MT] = _t.monotonic() - 31 * 60
    await nc.check_radon_meter_link()
    check("mw: zweites still -> auch verworfen (Einzelbeurteilung)",
          MT not in nc._radon_values and nc._meter_state_reported.get(MT) is False)
    await mq("radon/Radon_B/state", '{"mittelwert":42}')
    check("mw: eines zurueck, anderes still -> weiter getrennt", state(b).get("radon_meter_connected") is False
          and nc._meter_state_reported.get("radon/Radon_B/state") is True)
    await mq(MT, '{"mittelwert":43}')
    check("mw: beide zurueck -> verbunden", state(b).get("radon_meter_connected") is True)

    # I2) Vergessen nach 7 Tagen (beide Uhren), Uhrensprung vergisst nicht
    b, dev, mq = mkbridge()
    nc = b.neuracell
    await mq(MT, '{"mittelwert":40}')
    await mq("radon/Radon_B/state", '{"mittelwert":41}')
    nc._meter_known[MT] = _t.time() - 8 * 86400          # Epoch alt, monotonic frisch (Uhrensprung)
    await nc.check_radon_meter_link()
    check("mw: Uhrensprung allein vergisst nicht", MT in nc._meter_known and state(b).get("radon_meter_connected") is True)
    nc._meter_heard[MT] = _t.monotonic() - 8 * 86400
    await nc.check_radon_meter_link()
    check("mw: 7 Tage nichts -> vergessen", MT not in nc._meter_topics() and MT not in nc._radon_values
          and MT not in nc._meter_state_reported, nc._meter_topics())
    check("mw: Liste ohne das vergessene Geraet", list(meters_list(b)["radon_meters"]) == ["radon/Radon_B/state"], meters_list(b))
    check("mw: uebriges Geraet verbunden", state(b).get("radon_meter_connected") is True
          and state(b).get("radon_meters") == ["radon/Radon_B/state"])
    await mq(MT, '{"mittelwert":44}')
    check("mw: vergessenes Geraet meldet sich -> wieder dabei", MT in meters_list(b)["radon_meters"]
          and state(b).get("radon_meters") == sorted([MT, "radon/Radon_B/state"]))
    # Zeitgrenze aus + taegliche Reconnects: Vergessen haengt nicht an der Zeitgrenzen-Referenz
    b, dev, mq = mkbridge(radon_meter_timeout=0)
    nc = b.neuracell
    await mq(LT, json.dumps({"radon_meters": {MT: _t.time() - 6.9 * 86400}}), retain=True)
    check("mw: Liste: 6,9 Tage alt -> uebernommen, Hoerzeit aus dem Alter",
          MT in nc._meter_known and _t.monotonic() - nc._meter_heard[MT] > 6.8 * 86400)
    nc.on_broker_connected()
    nc._meter_known[MT] = _t.time() - 7.1 * 86400
    nc._meter_heard[MT] = _t.monotonic() - 7.1 * 86400
    await nc.check_radon_meter_link()
    check("mw: trotz Reconnect nach 7 Tagen vergessen", MT not in nc._meter_topics()
          and state(b).get("radon_meter_connected") is None)
    # Last Will 'offline' kommt vom Broker: kein Lebenszeichen fuer die Hoerzeit
    b, dev, mq = mkbridge()
    nc = b.neuracell
    await mq(MT, '{"mittelwert":40}')
    nc._meter_heard[MT] = _t.monotonic() - 1000
    await mq(AV, "offline")
    check("mw: LWT offline aendert die Hoerzeit nicht", _t.monotonic() - nc._meter_heard[MT] > 990)
    await mq(AV, "online")
    check("mw: online ist ein Lebenszeichen", _t.monotonic() - nc._meter_heard[MT] < 5)
    # Unsinn in der Liste: NaN / Infinity / Zukunft
    b, dev, mq = mkbridge()
    nc = b.neuracell
    await mq(LT, '{"radon_meters": {"radon/N/state": NaN, "radon/I/state": Infinity, "radon/F/state": %r}}'
             % (_t.time() + 5 * 86400), retain=True)
    check("mw: NaN/Infinity ignoriert, Zukunft auf jetzt begrenzt",
          set(nc._meter_known) == {"radon/F/state"} and _t.time() - nc._meter_known["radon/F/state"] < 5, nc._meter_known)
    # Liste gilt erst als geschrieben, wenn paho sie angenommen hat
    class NoConnClient(FakeClient):
        def __init__(self):
            super().__init__(); self.fail = True
        def publish(self, t, p, qos=0, retain=False):
            super().publish(t, p, qos, retain)
            class Info: pass
            i = Info(); i.rc = bridge.mqtt.MQTT_ERR_NO_CONN if self.fail else bridge.mqtt.MQTT_ERR_SUCCESS
            return i
    b, dev, mq = mkbridge()
    b.client = NoConnClient()
    nc = b.neuracell
    await mq(MT, '{"mittelwert":40}')
    check("mw: verworfene Veroeffentlichung zaehlt nicht als geschrieben", MT not in nc._meter_listed)
    b.client.fail = False
    await mq(MT, '{"mittelwert":40}')
    check("mw: naechste Nachricht schreibt die Liste", MT in nc._meter_listed)
    # Reconnect veroeffentlicht die Liste erneut - aber nie eine leere beim ersten Start
    class ConnClient(FakeClient):
        def __init__(self):
            super().__init__(); self.subs = []
        def subscribe(self, t, *a, **k):
            self.subs.append(t)
        def is_connected(self):
            return True
    b, dev, mq = mkbridge()
    b.client = ConnClient()
    b._on_mqtt_connect(b.client, None, {}, 0)
    check("mw: erster Start: keine (leere) Liste veroeffentlicht", meters_list(b) is None, meters_list(b))
    await mq(MT, '{"mittelwert":40}')
    n = len([1 for t, _p in b.client.pub if t.endswith("neuracell/meters")])
    b._on_mqtt_connect(b.client, None, {}, 0)
    check("mw: Reconnect veroeffentlicht die Liste erneut", len([1 for t, _p in b.client.pub if t.endswith("neuracell/meters")]) == n + 1
          and meters_list(b)["radon_meters"].keys() == {MT})

    # K) retained vom Broker (Neustart): 'online' und alter Wert sind kein Lebenszeichen
    b, dev, mq = mkbridge()
    nc = b.neuracell
    await mq(AV, "online", retain=True)
    check("mw: retained online -> bekannt, aber unbekannt (None)",
          MT in nc._meter_known and state(b).get("radon_meter_connected") is None, state(b))
    await mq(MT, '{"mittelwert":77}', retain=True)
    check("mw: retained Wert -> weiter unbekannt, Wert aber verfuegbar",
          state(b).get("radon_meter_connected") is None and nc.last_radon == 77, state(b))
    nc._meter_ref[MT] = _t.monotonic() - 31 * 60
    await nc.check_radon_meter_link()
    check("mw: retained online + 31 min still -> getrennt", state(b).get("radon_meter_connected") is False)
    check("mw: alter Wert nach Zeitgrenze verworfen", MT not in nc._radon_values)
    await mq(MT, '{"mittelwert":66}')
    check("mw: live Wert -> verbunden", state(b).get("radon_meter_connected") is True)
    b, dev, mq = mkbridge()
    await mq(AV, "offline", retain=True)
    check("mw: retained offline -> getrennt", state(b).get("radon_meter_connected") is False)
    await mq(MT, '{"mittelwert":166}', retain=True)
    check("mw: retained Wert aendert offline nicht", state(b).get("radon_meter_connected") is False)
    check("mw: gespeicherter Wert eines getrennten Geraets nicht verwendet",
          MT not in b.neuracell._radon_values and not b.neuracell.radon_active and b.neuracell.last_radon is None)
    await mq(MT, '{"mittelwert":166}')
    check("mw: live Wert -> verbunden und verwendet", state(b).get("radon_meter_connected") is True
          and b.neuracell.radon_active)
    # gespeicherte 0 (Startwert) nach Neustart wird ignoriert
    b, dev, mq = mkbridge()
    await mq(MT, '{"mittelwert":0}', retain=True)
    check("mw: gespeicherte 0 ignoriert, Geraet bekannt", b.neuracell.last_radon is None
          and MT in b.neuracell._meter_known and state(b).get("radon_meters") == [MT], state(b))
    # erste live Nachricht ist die Start-0: Status wird trotzdem veroeffentlicht
    b, dev, mq = mkbridge()
    n0 = len([1 for t, _p in b.client.pub if t.endswith("neuracell/state")])
    await mq(MT, '{"mittelwert":0}')
    check("mw: Start-0 als erste Nachricht -> Status 'verbunden' veroeffentlicht",
          len([1 for t, _p in b.client.pub if t.endswith("neuracell/state")]) > n0
          and state(b).get("radon_meter_connected") is True and b.neuracell.last_radon is None, state(b))
    # unbrauchbare Availability wird ignoriert
    await mq(AV, '{"foo": 1}')
    check("mw: unbrauchbare Availability ignoriert", state(b).get("radon_meter_connected") is True)
    await mq(AV, '{"state": "offline"}')
    check("mw: JSON-Availability verstanden", state(b).get("radon_meter_connected") is False)

    # L) Liste wird bei laufendem Geraet hoechstens stuendlich, aber sicher erneuert
    b, dev, mq = mkbridge()
    nc = b.neuracell
    await mq(MT, '{"mittelwert":40}')
    n1 = len([1 for t, _p in b.client.pub if t.endswith("neuracell/meters")])
    for _i in range(5):
        await mq(MT, '{"mittelwert":40}')
    check("mw: Liste nicht bei jedem Wert", len([1 for t, _p in b.client.pub if t.endswith("neuracell/meters")]) == n1)
    nc._meter_listed[MT] = _t.time() - 2 * 3600          # letzter Eintrag auf dem Broker 2 h alt
    await mq(MT, '{"mittelwert":40}')
    check("mw: Liste nach einer Stunde erneuert", len([1 for t, _p in b.client.pub if t.endswith("neuracell/meters")]) == n1 + 1
          and _t.time() - meters_list(b)["radon_meters"][MT] < 5, meters_list(b))
    # 8 Tage Betrieb mit 10-Minuten-Werten: Eintrag bleibt frisch (Regression: frueher nie erneuert)
    fake_now = [_t.time()]
    real_time = bridge.time.time
    bridge.time.time = lambda: fake_now[0]
    try:
        pubs_before = len([1 for t, _p in b.client.pub if t.endswith("neuracell/meters")])
        for _i in range(8 * 24 * 6):
            fake_now[0] += 600
            await mq(MT, '{"mittelwert":40}')
        age = fake_now[0] - meters_list(b)["radon_meters"][MT]
        n_pubs = len([1 for t, _p in b.client.pub if t.endswith("neuracell/meters")]) - pubs_before
    finally:
        bridge.time.time = real_time
    check("mw: nach 8 Tagen Betrieb Eintrag juenger als 1 h", age < 3600 + 1, age)
    check("mw: dabei etwa stuendlich erneuert (8 Tage ~ 192 mal)", 150 <= n_pubs <= 200, n_pubs)

    # J) Abonnements
    class SubClient(FakeClient):
        def __init__(self):
            super().__init__(); self.subs = []
        def subscribe(self, t, *a, **k):
            self.subs.append(t)
    b, dev, mq = mkbridge()
    b.client = SubClient()
    b._subscribe_all(b.client)
    check("mw: Liste abonniert", LT in b.client.subs, b.client.subs)
    check("mw: Reihenfolge Liste -> Availability -> Werte",
          b.client.subs.index(LT) < b.client.subs.index("radon/+/availability/state") < b.client.subs.index("radon/+/state"),
          b.client.subs)
    b, dev, mq = mkbridge(radon_meter_topic="")
    b.client = SubClient()
    b._subscribe_all(b.client)
    check("mw: ohne Messgeraet-Topic keine Liste", LT not in b.client.subs)


async def test_controller_direct():
    """1.4.27: Ambientika Taupunktsteuerung direkt (dew-point/<id>/state, ventilating)."""
    import time as _t
    c = bridge.BridgeConfig()
    c._apply_extras({}.get)
    c.apply_env_overrides()
    check("tpsd: Standard dew-point/+/state / ventilating",
          c.dewpoint_controller_topic == "dew-point/+/state" and c.dewpoint_controller_key == "ventilating")
    c._apply_extras({"dewpoint_controller_topic": "none", "dewpoint_controller_key": ""}.get)
    check("tpsd: none -> aus, leerer Schluessel -> Standard",
          c.dewpoint_controller_topic == "" and c.dewpoint_controller_key == "ventilating")
    c._apply_extras({"dewpoint_controller_topic": "tp/+/state", "dewpoint_controller_key": "lueften"}.get)
    check("tpsd: Werte uebernommen", c.dewpoint_controller_topic == "tp/+/state" and c.dewpoint_controller_key == "lueften")
    os.environ["DEWPOINT_CONTROLLER_TOPIC"] = "Off"
    c3 = bridge.BridgeConfig.from_env()
    del os.environ["DEWPOINT_CONTROLLER_TOPIC"]
    check("tpsd: Umgebungsvariable off", c3.dewpoint_controller_topic == "")
    check("tpsd: Sensor-Filter", bridge._sensors_filter("dew-point/+/state") == "dew-point/+/sensors"
          and bridge._sensors_filter("x/y") == "")
    check("tpsd: Availability-Filter", bridge._availability_filter("dew-point/+/state") == "dew-point/+/availability/state")
    cv = bridge._controller_ventilating
    check("tpsd: ventilating parsen", cv('{"ventilating": true, "reason": "ok"}', "ventilating") is True
          and cv('{"ventilating": false}', "ventilating") is False
          and cv('{"ventilating": 1}', "ventilating") is True and cv('{"ventilating": 0}', "ventilating") is False
          and cv('{"ventilating": "false"}', "ventilating") is False)
    check("tpsd: unbrauchbar -> None", cv('{"reason": "x"}', "ventilating") is None and cv("ON", "ventilating") is None
          and cv('{"ventilating": 2}', "ventilating") is None and cv('{"ventilating": null}', "ventilating") is None
          and cv("[1]", "ventilating") is None)

    class Msg:
        def __init__(self, t, p, retain=False):
            self.topic, self.payload, self.retain = t, p.encode(), retain

    def mkbridge(**kw):
        cfg = bridge.BridgeConfig()
        cfg.dewpoint_block_devices = "OFF-1"
        for k, v in kw.items():
            setattr(cfg, k, v)
        b = bridge.AmbientikaBridge(cfg)
        b.client = FakeClient()
        b.loop = asyncio.get_running_loop()
        office = FakeDevice(serial="OFF-1", name="Office", status=mkstatus(op=OM.Smart))
        smart = FakeDevice(serial="SM-1", name="Eltern", status=mkstatus(op=OM.Night))
        b.devices = {office.serial_number: office, smart.serial_number: smart}
        pending = []
        b._dispatch = lambda coro: pending.append(coro)

        async def mq(t, p, retain=False):
            b._on_mqtt_message(None, None, Msg(t, p, retain))
            while pending:
                await pending.pop(0)
        return b, office, smart, mq

    def state(b):
        return [json.loads(p) for t, p in b.client.pub if t.endswith("neuracell/state")][-1]

    ST, AV, SE = ("dew-point/TP_D48C4956EC10/state", "dew-point/TP_D48C4956EC10/availability/state",
                  "dew-point/TP_D48C4956EC10/sensors")

    # A) ohne jede Konfiguration: ventilating false -> Sperre, true -> frei
    b, office, smart, mq = mkbridge()
    await mq(ST, '{"ventilating": false, "reason": "Aussen zu feucht"}')
    check("tpsd: ventilating false -> OFFICE aus", office._status["operating_mode"] == OM.Off and b.neuracell.dewpoint_block)
    check("tpsd: SMART unberuehrt", smart._status["operating_mode"] == OM.Night)
    await mq(ST, '{"ventilating": true, "reason": "ok"}')
    check("tpsd: ventilating true -> frei, OFFICE wieder Smart",
          office._status["operating_mode"] == OM.Smart and not b.neuracell.dewpoint_block)
    await mq(ST, '{"reason": "ohne Flag"}')
    check("tpsd: Nachricht ohne Flag ignoriert", not b.neuracell.dewpoint_block)
    check("tpsd: live Nachricht der Steuerung = verbunden (auch ohne Availability)",
          state(b).get("dewpoint_controller_connected") is True, state(b))

    # B) Availability automatisch, Sensoren als Lebenszeichen
    await mq(AV, "online")
    check("tpsd: online -> verbunden", state(b).get("dewpoint_controller_connected") is True)
    before = b.neuracell._tps_last_seen
    await asyncio.sleep(0.01)
    await mq(SE, '{"indoor": {"temp": 20.1, "humidity": 55}, "outdoor": {"temp": 12.0, "humidity": 80}}')
    check("tpsd: Sensoren = Lebenszeichen", b.neuracell._tps_last_seen > before and not b.neuracell.dewpoint_block)
    await mq(AV, "offline")
    check("tpsd: Last Will -> getrennt (keep: Sperre bleibt frei)",
          state(b).get("dewpoint_controller_connected") is False and not b.neuracell.dewpoint_block)

    # C) release: Sperre aktiv, Steuerung haengt -> frei; zurueck -> letzte Entscheidung gilt wieder
    b, office, smart, mq = mkbridge(dewpoint_lost_action="release")
    await mq(AV, "online")
    await mq(ST, '{"ventilating": false}')
    check("tpsd: Sperre aktiv", office._status["operating_mode"] == OM.Off)
    await mq(AV, "offline")
    check("tpsd: haengt -> release", office._status["operating_mode"] == OM.Smart and not b.neuracell.dewpoint_block)
    await mq(AV, "online")
    check("tpsd: zurueck -> Sperre aus letzter Entscheidung wieder aktiv",
          office._status["operating_mode"] == OM.Off and b.neuracell.dewpoint_block)
    await mq(ST, '{"ventilating": true}')
    check("tpsd: frei", office._status["operating_mode"] == OM.Smart)

    # D) retained alter Zustand einer getrennten Steuerung wird bei release nicht angewendet
    b, office, smart, mq = mkbridge(dewpoint_lost_action="release")
    await mq(AV, "offline", retain=True)
    await mq(ST, '{"ventilating": false}', retain=True)
    check("tpsd: retained Sperre einer toten Steuerung ignoriert", office._status["operating_mode"] == OM.Smart
          and not b.neuracell.dewpoint_block)
    await mq(AV, "online")
    check("tpsd: online -> gespeicherte Entscheidung (Sperre) gilt", office._status["operating_mode"] == OM.Off)

    # E) Live-Nachricht auf dem eigenen Topic waehrend LWT offline: angewendet, wieder verbunden
    #    (nur die Steuerung selbst sendet dort - wie beim Messgeraet)
    b, office, smart, mq = mkbridge(dewpoint_lost_action="release")
    await mq(AV, "offline")
    await mq(ST, '{"ventilating": false}')
    check("tpsd: live Sperre trotz offline angewendet", office._status["operating_mode"] == OM.Off)
    check("tpsd: eigenes Topic live -> wieder verbunden", state(b).get("dewpoint_controller_connected") is True)
    await mq(AV, "offline")
    check("tpsd: erneuter Last Will -> getrennt, release", state(b).get("dewpoint_controller_connected") is False
          and office._status["operating_mode"] == OM.Smart)
    await mq(SE, '{"indoor": {"temp": 20}}')
    check("tpsd: Sensoren live -> wieder verbunden, letzte Entscheidung (Sperre) gilt wieder",
          state(b).get("dewpoint_controller_connected") is True and office._status["operating_mode"] == OM.Off)
    check("tpsd: Lebenszeichen hebt offline auf, meldet aber kein online", b.neuracell._tps_avail is None)
    # mit Zeitgrenze: nach dem Wiederbeleben ueber eigene Nachrichten greift die Zeitgrenze weiter
    b, office, smart, mq = mkbridge(dewpoint_lost_action="release", dewpoint_signal_timeout=10)
    await mq(AV, "online")
    await mq(AV, "offline")
    await mq(SE, '{"indoor": {"temp": 20}}')
    check("tpsd: Zeitgrenze: Sensoren nach offline -> verbunden", state(b).get("dewpoint_controller_connected") is True)
    b.neuracell._tps_last_seen = _t.monotonic() - 11 * 60
    b.neuracell._tps_watch_start = b.neuracell._tps_last_seen
    await b.neuracell.check_dewpoint_link()
    check("tpsd: Zeitgrenze greift nach dem Wiederbeleben weiter", state(b).get("dewpoint_controller_connected") is False)
    # generisches Sperr-Topic: dort bleibt der Last Will massgeblich (kann aus HA kommen)
    b, office, smart, mq = mkbridge(dewpoint_lost_action="release", dewpoint_availability_topic="tps/avail")
    await mq("tps/avail", "offline")
    await mq("ambientika/dewpoint/block", "ON")
    check("tpsd: generisches Topic live trotz offline angewendet", office._status["operating_mode"] == OM.Off)
    check("tpsd: generisches Topic: Status bleibt getrennt", state(b).get("dewpoint_controller_connected") is False)
    # gespeichertes 'online' nach Reconnect hebt einen live gesehenen Last Will nicht auf
    b, office, smart, mq = mkbridge(dewpoint_lost_action="release")
    await mq(AV, "online")
    await mq(ST, '{"ventilating": false}')
    await mq(AV, "offline")
    check("tpsd: offline -> release", office._status["operating_mode"] == OM.Smart)
    b.neuracell.on_broker_connected()
    await mq(AV, "online", retain=True)
    check("tpsd: gespeichertes online nach live offline -> bleibt getrennt",
          state(b).get("dewpoint_controller_connected") is False)
    await mq(ST, '{"ventilating": false}', retain=True)
    check("tpsd: gespeicherte Sperre der toten Steuerung weiter ignoriert", office._status["operating_mode"] == OM.Smart)
    await mq(AV, "online")
    check("tpsd: live online -> verbunden, Sperre gilt wieder", state(b).get("dewpoint_controller_connected") is True
          and office._status["operating_mode"] == OM.Off)

    # F) Zeitgrenze ohne Last Will: Sensoren halten die Verbindung
    b, office, smart, mq = mkbridge(dewpoint_signal_timeout=10)
    await mq(SE, '{"indoor": {"temp": 20}}')
    check("tpsd: Sensoren -> verbunden (Zeitgrenze)", state(b).get("dewpoint_controller_connected") is True)
    b.neuracell._tps_last_seen = _t.monotonic() - 11 * 60
    b.neuracell._tps_watch_start = b.neuracell._tps_last_seen
    await b.neuracell.check_dewpoint_link()
    check("tpsd: 11 min nichts -> getrennt", state(b).get("dewpoint_controller_connected") is False)
    await mq(ST, '{"ventilating": true}')
    check("tpsd: Nachricht -> wieder verbunden", state(b).get("dewpoint_controller_connected") is True)
    b, office, smart, mq = mkbridge(dewpoint_signal_timeout=10, dewpoint_lost_action="release")
    await mq(AV, "online", retain=True)
    await mq(ST, '{"ventilating": false}', retain=True)
    check("tpsd: Zeitgrenze: gespeicherte Sperre bei unbekanntem Status angewendet", office._status["operating_mode"] == OM.Off)
    b.neuracell._tps_watch_start = _t.monotonic() - 11 * 60
    await b.neuracell.check_dewpoint_link()
    check("tpsd: Zeitgrenze abgelaufen -> release", state(b).get("dewpoint_controller_connected") is False
          and office._status["operating_mode"] == OM.Smart)
    await mq(SE, '{"indoor": {"temp": 20}}')
    check("tpsd: nur Sensoren zurueck -> verbunden und Sperre wieder aktiv",
          state(b).get("dewpoint_controller_connected") is True and office._status["operating_mode"] == OM.Off)

    # G) generischer Sperr-Topic funktioniert weiter, beide Wege greifen ineinander
    b, office, smart, mq = mkbridge()
    await mq("ambientika/dewpoint/block", "ON")
    check("tpsd: ambientika/dewpoint/block ON", office._status["operating_mode"] == OM.Off)
    await mq(ST, '{"ventilating": true}')
    check("tpsd: Steuerung meldet frei -> frei", office._status["operating_mode"] == OM.Smart)
    await mq(ST, '{"ventilating": false}')
    await mq("ambientika/dewpoint/block", "OFF")
    check("tpsd: letzte Nachricht gewinnt", office._status["operating_mode"] == OM.Smart)

    # H) abgeschaltet: dew-point-Topics ohne Wirkung
    b, office, smart, mq = mkbridge(dewpoint_controller_topic="")
    await mq(ST, '{"ventilating": false}')
    await mq(AV, "offline")
    b.publish_neuracell_state()
    check("tpsd: aus -> keine Wirkung", office._status["operating_mode"] == OM.Smart
          and state(b).get("dewpoint_controller_connected") is None, state(b))

    # I) explizites Availability-Topic gleich dem automatischen -> kein Konflikt
    b, office, smart, mq = mkbridge(dewpoint_availability_topic=AV)
    await mq(AV, "online")
    await mq(ST, '{"ventilating": false}')
    check("tpsd: explizit + automatisch gleich -> verbunden und Sperre",
          state(b).get("dewpoint_controller_connected") is True and office._status["operating_mode"] == OM.Off)

    # K) retained vom Broker (Neustart): 'online' kein Lebenszeichen, Sensoren schon
    b, office, smart, mq = mkbridge()
    await mq(AV, "online", retain=True)
    check("tpsd: retained online -> unbekannt", state(b).get("dewpoint_controller_connected") is None, state(b))
    await mq(SE, '{"indoor": {"temp": 20}}', retain=True)
    check("tpsd: retained Sensoren kein Lebenszeichen", state(b).get("dewpoint_controller_connected") is None)
    await mq(SE, '{"indoor": {"temp": 20}}')
    check("tpsd: live Sensoren -> verbunden", state(b).get("dewpoint_controller_connected") is True)
    await mq(AV, "offline", retain=True)
    check("tpsd: retained offline -> getrennt", state(b).get("dewpoint_controller_connected") is False)
    await mq(ST, '{"ventilating": false}', retain=True)
    check("tpsd: retained Zustand kein Lebenszeichen (keep: Sperre uebernommen)",
          state(b).get("dewpoint_controller_connected") is False and b.neuracell.dewpoint_block)
    await mq(AV, "online")
    check("tpsd: live online -> verbunden", state(b).get("dewpoint_controller_connected") is True)
    # explizites Availability-Topic (1.4.26): retained online ebenfalls unbekannt
    b, office, smart, mq = mkbridge(dewpoint_availability_topic="taupunkt/TP_X/availability/state",
                                    dewpoint_controller_topic="")
    await mq("taupunkt/TP_X/availability/state", "online", retain=True)
    check("tpsd: explizit retained online -> unbekannt", state(b).get("dewpoint_controller_connected") is None)
    await mq("taupunkt/TP_X/availability/state", "online")
    check("tpsd: explizit live online -> verbunden", state(b).get("dewpoint_controller_connected") is True)

    # K2) Reconnect der Bridge: altes Lebenszeichen zaehlt nach retained 'online' nicht mehr
    b, office, smart, mq = mkbridge()
    await mq(SE, '{"indoor": {"temp": 20}}')
    check("tpsd: live -> verbunden", state(b).get("dewpoint_controller_connected") is True)
    await asyncio.sleep(0.01)
    b.neuracell.on_broker_connected()
    await mq(AV, "online", retain=True)
    check("tpsd: nach Reconnect nur retained online -> unbekannt", state(b).get("dewpoint_controller_connected") is None, state(b))
    await mq(SE, '{"indoor": {"temp": 21}}')
    check("tpsd: naechste live Nachricht -> verbunden", state(b).get("dewpoint_controller_connected") is True)
    b.neuracell.on_broker_connected()
    await mq(AV, "offline", retain=True)
    check("tpsd: nach Reconnect retained offline -> getrennt", state(b).get("dewpoint_controller_connected") is False)
    # ohne Reconnect bleibt ein frisches Lebenszeichen gueltig
    b, office, smart, mq = mkbridge()
    await mq(ST, '{"ventilating": true}')
    await mq(AV, "online", retain=True)
    check("tpsd: frisches Lebenszeichen bleibt", state(b).get("dewpoint_controller_connected") is True)

    # L) dewpoint_block_topic ueberdeckt das Steuerungs-Topic: Warnung, Sperrsignal-Weg bleibt (wie 1.4.26)
    import logging as _lg
    class _Catch(_lg.Handler):
        def __init__(self):
            super().__init__(); self.msgs = []
        def emit(self, r):
            self.msgs.append(r.getMessage())
    class SubClient(FakeClient):
        def __init__(self):
            super().__init__(); self.subs = []
        def subscribe(self, t, *a, **k):
            self.subs.append(t)
    b, office, smart, mq = mkbridge(dewpoint_block_topic="dew-point/+/state", dewpoint_block_key="blocked")
    b.client = SubClient()
    h = _Catch(); bridge.log.addHandler(h)
    try:
        b._subscribe_all(b.client)
    finally:
        bridge.log.removeHandler(h)
    check("tpsd: Ueberdeckung gewarnt", any("also covers the state topic" in m for m in h.msgs), h.msgs)
    await mq(ST, '{"blocked": true, "ventilating": true}')
    check("tpsd: Ueberdeckung -> Sperrsignal-Weg (dewpoint_block_key)", office._status["operating_mode"] == OM.Off)
    b, office, smart, mq = mkbridge()
    b.client = SubClient()
    h = _Catch(); bridge.log.addHandler(h)
    try:
        b._subscribe_all(b.client)
    finally:
        bridge.log.removeHandler(h)
    check("tpsd: Standard ohne Warnung", not any("also covers" in m for m in h.msgs), h.msgs)

    # M) Fehler in einem Handler landet im Log (statt still zu verschwinden)
    b, office, smart, mq = mkbridge()
    h = _Catch(); bridge.log.addHandler(h)
    try:
        async def boom():
            raise RuntimeError("kaputt")
        bridge.AmbientikaBridge._dispatch(b, boom())   # echter Dispatch (mkbridge faengt ihn sonst ab)
        await asyncio.sleep(0.05)
    finally:
        bridge.log.removeHandler(h)
    check("tpsd: Handler-Fehler geloggt", any("handler failed" in m and "kaputt" in m for m in h.msgs), h.msgs)

    # J) Abonnements und Zuordnung
    b, office, smart, mq = mkbridge()
    b.client = SubClient()
    b._subscribe_all(b.client)
    check("tpsd: dew-point-Topics abonniert",
          all(t in b.client.subs for t in ("dew-point/+/availability/state", "dew-point/+/state", "dew-point/+/sensors")), b.client.subs)
    check("tpsd: Availability vor State",
          b.client.subs.index("dew-point/+/availability/state") < b.client.subs.index("dew-point/+/state"))
    check("tpsd: Radon-Messgeraet nicht als Steuerung gelesen",
          not bridge._topic_match("dew-point/+/state", "radon/Radon_X/state")
          and not bridge._topic_match("dew-point/+/state", "dew-point/TP_X/availability/state"))


class _LogCatch:
    """Sammelt die Logzeilen der Bridge fuer eine Pruefung."""

    def __init__(self):
        import logging
        self.lines = []
        self._h = logging.Handler()
        self._h.emit = lambda r: self.lines.append((r.levelname, r.getMessage()))
        self._lg = logging.getLogger("ambientika_bridge")

    def __enter__(self):
        self._old = self._lg.level
        self._lg.setLevel(10)
        self._lg.addHandler(self._h)
        return self

    def __exit__(self, *a):
        self._lg.removeHandler(self._h)
        self._lg.setLevel(self._old)

    def has(self, level, text):
        return any(lv == level and text in m for lv, m in self.lines)


def test_filter_ack_robust():
    """Wartungsquittung (Kundenfall Okt. 2026): nach 43 statt 90 Tagen weg, ohne Log.

    Ursache: ein einziger Abruf mit unbekanntem oder gruenem Rohwert loeschte die
    Quittung endgueltig und still. Jetzt endet sie nur nach Ablauf der Frist oder
    nach FILTER_ACK_CLEAR_POLLS gruenen Abrufen in Folge - beides mit Logzeile.
    """
    import time as _t
    AB = bridge.AmbientikaBridge
    old_env = {k: os.environ.get(k) for k in ("SLAVE_FILTER_SOFT_RESET", "FILTER_ACK_PATH", "FILTER_ACK_TTL_DAYS")}
    os.environ["SLAVE_FILTER_SOFT_RESET"] = "1"
    os.environ["FILTER_ACK_PATH"] = os.path.join(_tempfile.mkdtemp(), "filter_ack.json")
    os.environ["FILTER_ACK_TTL_DAYS"] = "90"
    try:
        S = "E05A1B9BAF0C"
        AB._filter_ack_write(S, "Bad")
        check("ack: Bad -> effektiv Good", AB._filter_ack_effective(S, "Bad") == "Good")
        check("ack: Medium -> effektiv Good", AB._filter_ack_effective(S, "Medium") == "Good")

        # A) einzelner unbekannter Rohwert (null, leer, neuer Text) loescht nicht mehr
        with _LogCatch() as lc:
            for odd in (None, "", "Unknown"):
                check("ack: unbekannter Rohwert %r -> Quittung bleibt (Good)" % (odd,),
                      AB._filter_ack_effective(S, odd) == "Good")
            check("ack: unbekannter Rohwert steht im Log", lc.has("INFO", "unrecognised filter value"), lc.lines)
            n_before = len(lc.lines)
            AB._filter_ack_effective(S, "Unknown")
            check("ack: gleicher unbekannter Wert nicht jedes Mal geloggt", len(lc.lines) == n_before, lc.lines)
        check("ack: nach unbekannten Werten weiter gespeichert", S in AB._filter_ack_load())
        check("ack: danach Bad -> weiter Good", AB._filter_ack_effective(S, "Bad") == "Good")

        # B) kurz gruener Rohwert (unter der Schwelle) loescht nicht
        alt_min = bridge.FILTER_ACK_CLEAR_MIN_S
        bridge.FILTER_ACK_CLEAR_MIN_S = 0.0   # erst nur die Abruf-Zaehlung pruefen
        for _ in range(bridge.FILTER_ACK_CLEAR_POLLS - 1):
            AB._filter_ack_effective(S, "Good")
        check("ack: %d x Good in Folge -> Quittung bleibt" % (bridge.FILTER_ACK_CLEAR_POLLS - 1),
              S in AB._filter_ack_load())
        check("ack: Bad nach kurzem Good -> wieder quittiert", AB._filter_ack_effective(S, "Bad") == "Good")
        check("ack: Bad setzt die Good-Serie zurueck", S not in bridge._FILTER_ACK_GOOD_STREAK)
        for _ in range(bridge.FILTER_ACK_CLEAR_POLLS - 1):
            AB._filter_ack_effective(S, "Good")
        check("ack: Serie nach Unterbrechung neu gezaehlt -> bleibt", S in AB._filter_ack_load())

        # C) dauerhaft gruen (Filter direkt am Geraet zurueckgesetzt) -> geloescht, mit Log
        with _LogCatch() as lc:
            r = AB._filter_ack_effective(S, "Good")
            check("ack: %d. Good in Folge -> Rohwert Good" % bridge.FILTER_ACK_CLEAR_POLLS, r == "Good")
            check("ack: dauerhaft Good -> Quittung entfernt", S not in AB._filter_ack_load())
            check("ack: Entfernen wegen Good steht im Log",
                  lc.has("INFO", "removed") and lc.has("INFO", "polls in a row"), lc.lines)
        check("ack: ohne Quittung zeigt Bad wieder Bad", AB._filter_ack_effective(S, "Bad") == "Bad")
        bridge.FILTER_ACK_CLEAR_MIN_S = alt_min

        # C2) Mindestdauer: zehn Abrufe in Folge reichen allein nicht (Neustart
        # eines Slaves mit Standardstatus, poll_interval 10 s = 100 s)
        AB._filter_ack_write(S, "Bad")
        with _LogCatch() as lc:
            for _ in range(bridge.FILTER_ACK_CLEAR_POLLS * 3):
                AB._filter_ack_effective(S, "Good")
            check("ack: %d x Good ohne Mindestdauer -> Quittung bleibt" % (bridge.FILTER_ACK_CLEAR_POLLS * 3),
                  S in AB._filter_ack_load() and not lc.has("INFO", "removed"), lc.lines)
        check("ack: nach kurzer Good-Phase Bad -> weiter quittiert", AB._filter_ack_effective(S, "Bad") == "Good")
        # Serie neu, Startzeit zurueckdatieren -> Dauer erreicht, Zaehlung muss trotzdem voll sein
        AB._filter_ack_effective(S, "Good")
        cnt, since = bridge._FILTER_ACK_GOOD_STREAK[S]
        bridge._FILTER_ACK_GOOD_STREAK[S] = (cnt, since - bridge.FILTER_ACK_CLEAR_MIN_S - 1)
        check("ack: Dauer erreicht, aber erst 1 Abruf -> bleibt",
              AB._filter_ack_effective(S, "Good") == "Good" and S in AB._filter_ack_load())
        for _ in range(bridge.FILTER_ACK_CLEAR_POLLS):
            AB._filter_ack_effective(S, "Good")
        check("ack: Dauer und Abrufe erreicht -> entfernt", S not in AB._filter_ack_load())

        # D) Frist: 43 Tage alt -> gilt noch; 91 Tage alt -> entfernt, mit Log
        AB._filter_ack_write(S, "Bad")
        data = AB._filter_ack_load()
        data[S]["acked_at"] = _t.time() - 43 * 86400
        AB._filter_ack_save(data)
        check("ack: nach 43 Tagen (TTL 90) noch gueltig", AB._filter_ack_effective(S, "Bad") == "Good")
        data = AB._filter_ack_load()
        data[S]["acked_at"] = _t.time() - 91 * 86400
        AB._filter_ack_save(data)
        with _LogCatch() as lc:
            check("ack: nach 91 Tagen Rohwert Bad", AB._filter_ack_effective(S, "Bad") == "Bad")
            check("ack: Ablauf steht im Log", lc.has("INFO", "ran out"), lc.lines)
        check("ack: nach Ablauf entfernt", S not in AB._filter_ack_load())

        # E) kaputtes acked_at crasht nicht, gilt als abgelaufen
        AB._filter_ack_write(S, "Bad")
        data = AB._filter_ack_load()
        data[S]["acked_at"] = "kaputt"
        AB._filter_ack_save(data)
        check("ack: kaputtes acked_at -> kein Crash, Rohwert", AB._filter_ack_effective(S, "Bad") == "Bad")

        # F) ohne Option bleibt alles roh
        os.environ["SLAVE_FILTER_SOFT_RESET"] = "0"
        check("ack: Option aus -> Rohwert", AB._filter_ack_effective(S, "Bad") == "Bad")
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        bridge._FILTER_ACK_GOOD_STREAK.clear()
        bridge._FILTER_ACK_ODD_RAW.clear()


async def test_filter_ack_poll_path():
    """Kundenfall durch den echten Poll-Pfad: Bad, dann ein Abruf None, einer Good,
    dann wieder Bad -> der veroeffentlichte filters_status bleibt durchgehend Good,
    der Rohwert steht daneben, die Quittung bleibt gespeichert."""
    AB = bridge.AmbientikaBridge
    old_env = {k: os.environ.get(k) for k in ("SLAVE_FILTER_SOFT_RESET", "FILTER_ACK_PATH")}
    os.environ["SLAVE_FILTER_SOFT_RESET"] = "1"
    os.environ["FILTER_ACK_PATH"] = os.path.join(_tempfile.mkdtemp(), "filter_ack.json")
    try:
        cfg = bridge.BridgeConfig()
        cfg.neuracell_enabled = False
        cfg.dewpoint_enabled = False
        cfg.enable_discovery = False
        cfg.poll_interval = 1
        b = bridge.AmbientikaBridge(cfg)
        b.client = FakeClient()
        b.loop = asyncio.get_running_loop()
        dev = FakeDevice(serial="WOZI", name="Wohnzimmer", zone=0, status=mkstatus())
        dev._status["filters_status"] = "Bad"
        b.devices = {"WOZI": dev}
        AB._filter_ack_write("WOZI", "Bad")
        seen = []
        b._stop_event = asyncio.Event()
        task = asyncio.create_task(b._poll_loop())
        for raw in ("Bad", None, "Good", "Bad", "Bad"):
            dev._status["filters_status"] = raw
            await asyncio.sleep(1.05)
            states = [json.loads(pl) for t, pl in b.client.pub if t.endswith("/state")]
            seen.append((raw, states[-1]["filters_status"], states[-1]["filters_status_raw"]))
        b._stop_event.set()
        await task
        check("ack/poll: effektiv durchgehend Good", all(e == "Good" for _, e, _ in seen), seen)
        check("ack/poll: Rohwert daneben unveraendert", [r for r, _, _ in seen] == [r for _, _, r in seen], seen)
        check("ack/poll: Quittung weiterhin gespeichert", "WOZI" in AB._filter_ack_load())
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        bridge._FILTER_ACK_GOOD_STREAK.clear()
        bridge._FILTER_ACK_ODD_RAW.clear()


def test_filter_ack_status_sensor():
    """Quittung sichtbar machen (Kundenfall Okt. 2026, Bridge 1.4.35).

    Vorher: nach einem Neustart/Update stand "Filter Reset Status" bei jedem
    Geraet auf idle - auch bei einem Slave mit gueltiger Quittung -, und wann
    die Quittung ablaeuft, war nirgends zu sehen. Jetzt: Startzustand
    "acknowledged", solange die Quittung gilt; zwei Zeitstempel-Sensoren
    "quittiert seit" / "gueltig bis"; faellt die Quittung weg, geht der
    Status-Sensor auf idle zurueck, ohne Neustart.
    """
    import time as _t
    AB = bridge.AmbientikaBridge
    old_env = {k: os.environ.get(k) for k in ("SLAVE_FILTER_SOFT_RESET", "FILTER_ACK_PATH", "FILTER_ACK_TTL_DAYS")}
    os.environ["SLAVE_FILTER_SOFT_RESET"] = "1"
    os.environ["FILTER_ACK_PATH"] = os.path.join(_tempfile.mkdtemp(), "filter_ack.json")
    os.environ["FILTER_ACK_TTL_DAYS"] = "90"
    pfx = "ambientika"

    def resets(client, serial):
        return [p for t, p in client.pub if t == bridge.reset_state_topic(pfx, serial)]

    def mk():
        cfg = bridge.BridgeConfig()
        cfg.neuracell_enabled = False
        cfg.dewpoint_enabled = False
        cfg.enable_discovery = False
        b = bridge.AmbientikaBridge(cfg)
        b.client = FakeClient()
        master = FakeDevice(serial="FLUR", name="Flur", zone=0, status=mkstatus(op=OM.Smart))
        master.role = "Master"
        slave = FakeDevice(serial="WOZI", name="Wohnzimmer", zone=0, status=mkstatus(op=OM.Surveillance))
        slave._status["filters_status"] = "Bad"
        b.devices = {"FLUR": master, "WOZI": slave}
        return b, master, slave

    try:
        check("ackui: ISO 8601 mit Offset", bridge._iso_utc(0) == "1970-01-01T00:00:00+00:00", bridge._iso_utc(0))

        # Discovery: die beiden Zeitstempel nur mit eingeschalteter Option
        ents = bridge.build_discovery_configs(bridge.BridgeConfig(), "WOZI", "Wohnzimmer")
        ts = {t: p for t, p in ents if t.endswith(("_filter_ack_since/config", "_filter_ack_until/config"))}
        check("ackui: zwei Zeitstempel-Sensoren in der Discovery", len(ts) == 2, list(ts))
        check("ackui: device_class timestamp, diagnostic, eigene unique_id",
              all(p["device_class"] == "timestamp" and p["entity_category"] == "diagnostic"
                  and p["unique_id"] in ("ambientika_WOZI_filter_ack_since", "ambientika_WOZI_filter_ack_until")
                  and p["value_template"] in ("{{ value_json.filter_ack_since | default('None', true) }}",
                                              "{{ value_json.filter_ack_until | default('None', true) }}")
                  for p in ts.values()), ts)
        check("ackui: Option an -> nichts zu entfernen", bridge.discovery_removals(bridge.BridgeConfig(), "WOZI") == [])
        os.environ["SLAVE_FILTER_SOFT_RESET"] = "0"
        ents = bridge.build_discovery_configs(bridge.BridgeConfig(), "WOZI", "Wohnzimmer")
        check("ackui: Option aus -> keine Zeitstempel-Sensoren",
              not any("filter_ack_" in t for t, _ in ents))
        check("ackui: Option aus -> die beiden Config-Topics werden geleert",
              sorted(bridge.discovery_removals(bridge.BridgeConfig(), "WOZI")) == sorted(ts), bridge.discovery_removals(bridge.BridgeConfig(), "WOZI"))
        # ueber den echten Discovery-Pfad: leere, retained Nachricht je Topic
        cfg0 = bridge.BridgeConfig(); cfg0.neuracell_enabled = False; cfg0.dewpoint_enabled = False
        b0 = bridge.AmbientikaBridge(cfg0); b0.client = FakeClient()
        b0.devices = {"WOZI": FakeDevice(serial="WOZI", name="Wohnzimmer", zone=0)}
        b0._publish_discovery()
        cleared = [(t, p, r) for t, p, r in b0.client.pubr if "_filter_ack_" in t]
        check("ackui: Discovery-Pfad leert die Topics (leer, retained)",
              len(cleared) == 2 and all(p == "" and r is True for _, p, r in cleared), cleared)
        os.environ["SLAVE_FILTER_SOFT_RESET"] = "1"
        b0.client = FakeClient(); b0._publish_discovery()
        check("ackui: Option an -> Discovery-Pfad veroeffentlicht die Sensoren, leert nichts",
              sum(1 for t, p in b0.client.pub if "_filter_ack_" in t and p != "") == 2
              and not any(p == "" for t, p in b0.client.pub if "_filter_ack_" in t), b0.client.pub)

        # Ohne Quittung: Start idle, Felder null
        b, master, slave = mk()
        check("ackui: ohne Quittung kein Datensatz", AB._filter_ack_record("WOZI") is None)
        b._publish_initial_reset_states()
        check("ackui: Start ohne Quittung -> idle fuer alle",
              resets(b.client, "WOZI") == ["idle"] and resets(b.client, "FLUR") == ["idle"], b.client.pub)
        pl = b._state_payload("WOZI", slave, dict(slave._status))
        check("ackui: ohne Quittung since/until null",
              pl["filter_ack_since"] is None and pl["filter_ack_until"] is None and pl["filters_status"] == "Bad", pl)
        check("ackui: Payload bleibt JSON", json.dumps(pl) is not None)

        # Mit Quittung: Start acknowledged (nur der Slave), Log nennt sie, Felder gefuellt
        AB._filter_ack_write("WOZI", "Bad")
        rec = AB._filter_ack_record("WOZI")
        check("ackui: Datensatz mit acked_at/valid_until (90 Tage)",
              rec and abs(rec["valid_until"] - rec["acked_at"] - 90 * 86400) < 1e-6
              and abs(rec["acked_at"] - _t.time()) < 5 and rec["raw_when_acked"] == "Bad", rec)
        b, master, slave = mk()
        with _LogCatch() as lc:
            b._publish_initial_reset_states()
        check("ackui: Start mit Quittung -> acknowledged fuer den Slave, idle fuer den Master",
              resets(b.client, "WOZI") == ["acknowledged"] and resets(b.client, "FLUR") == ["idle"], b.client.pub)
        check("ackui: Startlog nennt Geraet, Datum und Ablauf",
              lc.has("INFO", "filter acknowledgement for WOZI (Wohnzimmer) still in force")
              and lc.has("INFO", "runs out 20"), lc.lines)
        pl = b._state_payload("WOZI", slave, dict(slave._status))
        exp_since = bridge._iso_utc(rec["acked_at"])
        exp_until = bridge._iso_utc(rec["acked_at"] + 90 * 86400)
        check("ackui: since/until im Payload, effektiv Good, roh Bad",
              pl["filter_ack_since"] == exp_since and pl["filter_ack_until"] == exp_until
              and pl["filters_status"] == "Good" and pl["filters_status_raw"] == "Bad", pl)
        check("ackui: Zeitstempel von HA lesbar (fromisoformat, mit Zeitzone)",
              bridge.datetime.fromisoformat(pl["filter_ack_since"]).tzinfo is not None)
        plm = b._state_payload("FLUR", master, dict(master._status))
        check("ackui: Master ohne Quittung -> null", plm["filter_ack_since"] is None and plm["filter_ack_until"] is None)

        # Laufender Betrieb: Quittung erscheint -> acknowledged einmal; nichts doppelt
        b, master, slave = mk()
        b._publish_reset_state("WOZI", "idle")
        b._publish_state("WOZI", b._state_payload("WOZI", slave, dict(slave._status)))
        b._publish_state("WOZI", b._state_payload("WOZI", slave, dict(slave._status)))
        check("ackui: Quittung im Betrieb -> Status einmal auf acknowledged",
              resets(b.client, "WOZI") == ["idle", "acknowledged"], resets(b.client, "WOZI"))
        # Waehrend eines laufenden Resets bleibt der Status unangetastet
        b._reset_inflight.add("WOZI")
        b._publish_reset_state("WOZI", "running")
        b._publish_state("WOZI", b._state_payload("WOZI", slave, dict(slave._status)))
        check("ackui: laufender Reset wird nicht ueberschrieben",
              resets(b.client, "WOZI")[-1] == "running", resets(b.client, "WOZI"))
        b._reset_inflight.discard("WOZI")
        b._publish_reset_state("WOZI", "acknowledged")
        # Ablauf der Frist: der Poll entfernt die Quittung (mit Logzeile) -> idle, Felder null
        data = AB._filter_ack_load()
        data["WOZI"]["acked_at"] = _t.time() - 91 * 86400
        AB._filter_ack_save(data)
        with _LogCatch() as lc:
            pl = b._state_payload("WOZI", slave, dict(slave._status))
            b._publish_state("WOZI", pl)
        check("ackui: nach Ablauf -> Rohwert, Felder null, Status idle, Logzeile",
              pl["filters_status"] == "Bad" and pl["filter_ack_since"] is None
              and resets(b.client, "WOZI")[-1] == "idle" and lc.has("INFO", "validity of 90 days ran out"),
              (pl, resets(b.client, "WOZI"), lc.lines))
        n = len(resets(b.client, "WOZI"))
        b._publish_state("WOZI", b._state_payload("WOZI", slave, dict(slave._status)))
        check("ackui: ohne Quittung bleibt idle, keine weitere Meldung", len(resets(b.client, "WOZI")) == n)
        # Ein abgeschlossener Master-Reset ("confirmed") bleibt stehen
        b._publish_reset_state("FLUR", "confirmed")
        b._publish_state("FLUR", b._state_payload("FLUR", master, dict(master._status)))
        check("ackui: confirmed ohne Quittung bleibt confirmed", resets(b.client, "FLUR")[-1] == "confirmed")

        # Option aus: gespeicherte Quittung zaehlt nicht
        AB._filter_ack_write("WOZI", "Bad")
        os.environ["SLAVE_FILTER_SOFT_RESET"] = "0"
        b, master, slave = mk()
        b._publish_initial_reset_states()
        pl = b._state_payload("WOZI", slave, dict(slave._status))
        check("ackui: Option aus -> idle und null trotz Datensatz",
              resets(b.client, "WOZI") == ["idle"] and pl["filter_ack_since"] is None and pl["filters_status"] == "Bad",
              (resets(b.client, "WOZI"), pl))
        # Kaputter Datensatz -> kein Absturz, kein acknowledged
        os.environ["SLAVE_FILTER_SOFT_RESET"] = "1"
        data = AB._filter_ack_load()
        data["WOZI"]["acked_at"] = "kaputt"
        AB._filter_ack_save(data)
        check("ackui: kaputtes acked_at -> idle", AB._filter_ack_record("WOZI") is None
              and mk()[0]._initial_reset_state("WOZI") == "idle")

        # Review-Befunde: beschaedigte Datei darf weder den Start noch den Abruf kosten
        def damaged(label, content):
            with open(os.environ["FILTER_ACK_PATH"], "w", encoding="utf-8") as f:
                f.write(content)
            bb, _mm, ss = mk()
            try:
                bb._publish_initial_reset_states()
                pl2 = bb._state_payload("WOZI", ss, dict(ss._status))
                bb._publish_state("WOZI", pl2)
                ok = (resets(bb.client, "WOZI") == ["idle"] and pl2["filters_status"] == "Bad"
                      and pl2["filter_ack_since"] is None and AB._filter_ack_record("WOZI") is None)
                check("ackui: %s -> kein Absturz, idle, Rohwert" % label, ok, (resets(bb.client, "WOZI"), pl2))
            except Exception as ex:      # noqa: BLE001
                check("ackui: %s -> kein Absturz" % label, False, repr(ex))
        damaged("Datensatz kein Objekt", json.dumps({"WOZI": "Bad"}))
        damaged("Datei kein Objekt", json.dumps([1, 2]))
        damaged("Datei kein JSON", "{kaputt")
        damaged("acked_at in Millisekunden", json.dumps({"WOZI": {"acked_at": _t.time() * 1000, "raw_when_acked": "Bad"}}))
        damaged("acked_at in der Zukunft", json.dumps({"WOZI": {"acked_at": _t.time() + 7 * 86400, "raw_when_acked": "Bad"}}))
        damaged("acked_at NaN", '{"WOZI": {"acked_at": NaN, "raw_when_acked": "Bad"}}')
        damaged("acked_at null", json.dumps({"WOZI": {"acked_at": None, "raw_when_acked": "Bad"}}))
        with _LogCatch() as lc:
            with open(os.environ["FILTER_ACK_PATH"], "w", encoding="utf-8") as f:
                f.write(json.dumps({"WOZI": {"acked_at": _t.time() * 1000}}))
            AB._filter_ack_effective("WOZI", "Bad")
        check("ackui: unbrauchbarer Datensatz wird mit Logzeile entfernt",
              lc.has("INFO", "removed: the stored record is unusable") and "WOZI" not in AB._filter_ack_load(), lc.lines)
        check("ackui: _iso_utc ist total",
              bridge._iso_utc(float("nan")) is None and bridge._iso_utc(1e12) is None
              and bridge._iso_utc("x") is None and bridge._iso_utc(0) == "1970-01-01T00:00:00+00:00")

        # Ein Urteil eines gedrueckten Resets (confirmed/unconfirmed) bleibt stehen
        AB._filter_ack_write("WOZI", "Bad")
        for verdict in ("unconfirmed", "confirmed"):
            bb, _mm, ss = mk()
            bb._publish_reset_state("WOZI", verdict)
            bb._publish_state("WOZI", bb._state_payload("WOZI", ss, dict(ss._status)))
            check("ackui: %s bleibt trotz gueltiger Quittung stehen" % verdict,
                  resets(bb.client, "WOZI") == [verdict], resets(bb.client, "WOZI"))
        bb, _mm, ss = mk()
        bb._publish_state("WOZI", bb._state_payload("WOZI", ss, dict(ss._status)))
        check("ackui: ohne veroeffentlichten Zustand -> acknowledged", resets(bb.client, "WOZI") == ["acknowledged"])

        # Zweiter Wegfall-Grund: das Geraet meldet selbst lange genug Good -> idle
        bb, _mm, ss = mk()
        bb._publish_reset_state("WOZI", "acknowledged")
        alt_min = bridge.FILTER_ACK_CLEAR_MIN_S
        bridge.FILTER_ACK_CLEAR_MIN_S = 0.0
        try:
            ss._status["filters_status"] = "Good"
            with _LogCatch() as lc:
                for _ in range(bridge.FILTER_ACK_CLEAR_POLLS):
                    bb._last_status.pop("WOZI", None)      # jeder Abruf zaehlt als neues Paket
                    bb._publish_state("WOZI", bb._state_payload("WOZI", ss, dict(ss._status)))
            check("ackui: Geraet selbst Good -> Quittung weg, Status idle, Logzeile",
                  resets(bb.client, "WOZI") == ["acknowledged", "idle"] and "WOZI" not in AB._filter_ack_load()
                  and lc.has("INFO", "no longer needed"), (resets(bb.client, "WOZI"), lc.lines))
        finally:
            bridge.FILTER_ACK_CLEAR_MIN_S = alt_min
            bridge._FILTER_ACK_GOOD_STREAK.clear()

        # Ohne Broker: paho liefert rc=4 statt einer Ausnahme -> nicht als gezeigt merken,
        # naechster Abruf versucht es erneut; nach dem Reconnect wird alles Bekannte neu gesendet
        class OfflineClient(FakeClient):
            def publish(self, tp, pl, qos=0, retain=False):
                super().publish(tp, pl, qos, retain)
                class R:
                    rc = 4
                return R()
        AB._filter_ack_write("WOZI", "Bad")
        bb, _mm, ss = mk()
        bb.client = OfflineClient()
        bb._publish_state("WOZI", bb._state_payload("WOZI", ss, dict(ss._status)))
        check("ackui: rc=4 -> nicht als gezeigt gemerkt", "WOZI" not in bb._reset_shown, bb._reset_shown)
        bb.client = FakeClient()
        bb._publish_state("WOZI", bb._state_payload("WOZI", ss, dict(ss._status)))
        check("ackui: nach Rueckkehr des Brokers nachgeholt",
              resets(bb.client, "WOZI") == ["acknowledged"] and bb._reset_shown.get("WOZI") == "acknowledged")
        bb._publish_reset_state("FLUR", "confirmed")
        bb.client = FakeClient()
        bb._on_mqtt_connect(bb.client, None, None, 0)
        check("ackui: Reconnect sendet die bekannten Zustaende erneut",
              resets(bb.client, "WOZI") == ["acknowledged"] and resets(bb.client, "FLUR") == ["confirmed"],
              [(tp, pl) for tp, pl in bb.client.pub if "reset_state" in tp])
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        bridge._FILTER_ACK_GOOD_STREAK.clear()
        bridge._FILTER_ACK_ODD_RAW.clear()


class _IgnoringDevice(FakeDevice):
    """Cloud nimmt change-mode an (HTTP 200), das Geraet uebernimmt den Modus nicht."""

    async def change_mode(self, mode):
        self.mode_calls.append(mode)
        return Success(None)


async def test_mode_verify():
    """change_mode OK heisst nur angenommen: Bridge prueft den Modus nach (Kundenfall Okt. 2026)."""
    cfg = bridge.BridgeConfig()
    cfg.neuracell_enabled = False
    cfg.dewpoint_enabled = False
    cfg.enable_discovery = False
    b = bridge.AmbientikaBridge(cfg)
    b.client = FakeClient()
    b.loop = asyncio.get_running_loop()
    master = FakeDevice(serial="FLUR", name="Flur", zone=0, status=mkstatus(op=OM.Smart))
    master.role = "Master"
    ok = FakeDevice(serial="KIZI", name="Kinderzimmer", zone=0, status=mkstatus(op=OM.Surveillance))
    stuck = _IgnoringDevice(serial="GBAD", name="Gaeste-Bad", zone=0, status=mkstatus(op=OM.Surveillance))
    solo = _IgnoringDevice(serial="SOLO", name="Einzel", zone=5, status=mkstatus(op=OM.Auto))
    solo.role = "Master"
    b.devices = {d.serial_number: d for d in (master, ok, stuck, solo)}

    alt = bridge.MODE_VERIFY_WINDOW_S
    bridge.MODE_VERIFY_WINDOW_S = 0.25
    try:
        with _LogCatch() as lc:
            for s in ("KIZI", "GBAD", "SOLO"):
                await b._handle_command(s, "operating_mode", "Smart")
            check("mode: OK-Zeile sagt 'accepted by the cloud'",
                  lc.has("INFO", "change_mode OK for GBAD (accepted by the cloud)"), lc.lines)
            check("mode: drei Erwartungen offen", set(b._mode_expect) == {"KIZI", "GBAD", "SOLO"}, b._mode_expect)

            # erster Poll: Kinderzimmer bestaetigt, die anderen noch im Fenster -> keine Warnung
            for s, d in b.devices.items():
                b._check_mode_applied(s, d, d._status["operating_mode"])
            check("mode: uebernommener Modus wird bestaetigt", lc.has("INFO", "operating mode Smart confirmed on KIZI"), lc.lines)
            check("mode: im Fenster noch keine Warnung", not any(lv == "WARNING" for lv, _ in lc.lines), lc.lines)
            check("mode: Master ohne Befehl wird nicht geprueft", "FLUR" not in b._mode_expect)

            await asyncio.sleep(0.3)
            # echter Poll-Pfad: ein Durchlauf von _poll_loop nach Fensterende
            b._stop_event = asyncio.Event()
            task = asyncio.create_task(b._poll_loop())
            await asyncio.sleep(0.3)
            b._stop_event.set()
            await task
            warn_slave = [m for lv, m in lc.lines if lv == "WARNING" and "GBAD" in m]
            check("mode: Slave ohne Uebernahme -> Warnung", len(warn_slave) == 1, lc.lines)
            check("mode: Warnung nennt Ist-Modus und Master",
                  warn_slave and "still reports Surveillance" in warn_slave[0] and "FLUR" in warn_slave[0]
                  and "SLAVE" in warn_slave[0], warn_slave)
            warn_solo = [m for lv, m in lc.lines if lv == "WARNING" and "SOLO" in m]
            check("mode: Einzelgeraet ohne Uebernahme -> Warnung ohne Slave-Hinweis",
                  len(warn_solo) == 1 and "SLAVE" not in warn_solo[0] and "has not been applied" in warn_solo[0], warn_solo)
            check("mode: Warnung nur einmal, danach nichts offen", not b._mode_expect, b._mode_expect)
            check("mode: kein Warn-Eintrag fuer das Kinderzimmer",
                  not any(lv == "WARNING" and "KIZI" in m for lv, m in lc.lines))

        # Lueefterstufe allein: kein Moduswechsel verlangt -> keine Pruefung
        b._mode_expect.clear()
        await b._handle_command("SOLO", "fan_speed", "High")
        check("mode: Befehl ohne Modus legt keine Pruefung an", "SOLO" not in b._mode_expect, b._mode_expect)

        # NeuraCell-X uebernimmt das Geraet -> nicht als Fehler werten
        await b._handle_command("GBAD", "operating_mode", "Smart")
        orig = b.neuracell._device_under_control
        b.neuracell._device_under_control = lambda s, d=None: s == "GBAD"
        try:
            await asyncio.sleep(0.3)
            with _LogCatch() as lc:
                b._check_mode_applied("GBAD", stuck, OM.Intake)
                check("mode: unter NeuraCell-X-Schutz keine Warnung",
                      not any(lv == "WARNING" for lv, _ in lc.lines) and "GBAD" not in b._mode_expect, lc.lines)
        finally:
            b.neuracell._device_under_control = orig
    finally:
        bridge.MODE_VERIFY_WINDOW_S = alt


async def _one_poll(b, cycles=1):
    """Run _poll_loop for the given number of cycles (poll_interval tiny)."""
    b._stop_event = asyncio.Event()
    task = asyncio.create_task(b._poll_loop())
    await asyncio.sleep(0.06 * cycles + 0.02)
    b._stop_event.set()
    await task


def _last_state(b, serial):
    states = [json.loads(pl) for t, pl in b.client.pub if t.endswith("/%s/state" % serial)]
    return states[-1] if states else None


async def test_shown_mode():
    """Kundenfall Okt. 2026: Slaves melden 'Surveillance', laufen aber mit dem Master.
    Die Bridge zeigt fuer einen Slave den Modus des Masters, den eigenen Wert in *_raw."""
    cfg = bridge.BridgeConfig()
    cfg.neuracell_enabled = False
    cfg.dewpoint_enabled = False
    cfg.enable_discovery = False
    cfg.poll_interval = 0.05
    b = bridge.AmbientikaBridge(cfg)
    b.client = FakeClient()
    b.loop = asyncio.get_running_loop()
    # Slave vor dem Master im Dict: im ersten Durchlauf ist der Master noch unbekannt
    gbad = FakeDevice(serial="GBAD", name="Gaeste-Bad", zone=0, status=mkstatus(op=OM.Surveillance))
    master = FakeDevice(serial="FLUR", name="Flur", zone=0, status=mkstatus(op=OM.Smart))
    master.role = "Master"
    wozi = FakeDevice(serial="WOZI", name="Wohnzimmer", zone=0, status=mkstatus(op=OM.Smart))
    solo = FakeDevice(serial="SOLO", name="Einzel", zone=5, status=mkstatus(op=OM.Night))
    solo.role = "Master"
    b.devices = {d.serial_number: d for d in (gbad, master, wozi, solo)}

    with _LogCatch() as lc:
        await _one_poll(b, 1)
        firsts = [json.loads(pl) for t, pl in b.client.pub if t.endswith("/GBAD/state")]
        st = firsts[0] if firsts else None
        check("shown: Master zuerst -> schon der erste Durchlauf zeigt den Mastermodus",
              st and st["operating_mode"] == "Smart" and st["operating_mode_raw"] == "Surveillance", st)
        # ohne jeden Masterwert (Master noch nie gelesen) -> eigener Wert
        b._last_mode.pop("FLUR", None)
        st0 = b._shown_mode("GBAD", gbad, OM.Surveillance)
        check("shown: ohne Masterwert -> eigener Wert", st0 == OM.Surveillance, st0)
        await _one_poll(b, 3)
        st = _last_state(b, "GBAD")
        check("shown: Slave zeigt den Modus des Masters", st["operating_mode"] == "Smart", st)
        check("shown: eigener Wert steht in operating_mode_raw", st["operating_mode_raw"] == "Surveillance", st)
        check("shown: Zahlen passend", st["operating_mode_num"] == OM.Smart.value
              and st["operating_mode_raw_num"] == OM.Surveillance.value, st)
        notes = [m for lv, m in lc.lines if lv == "INFO" and "operating mode of GBAD" in m]
        check("shown: Hinweis genau einmal im Log", len(notes) == 1, notes)
        check("shown: Hinweis nennt beide Werte und den Master",
              notes and "reports Surveillance, shown as Smart" in notes[0] and "FLUR" in notes[0], notes)
        st = _last_state(b, "FLUR")
        check("shown: Master unveraendert", st["operating_mode"] == "Smart" and st["operating_mode_raw"] == "Smart", st)
        st = _last_state(b, "WOZI")
        check("shown: Slave gleich Master -> kein Hinweis",
              st["operating_mode"] == "Smart" and not any("operating mode of WOZI" in m for _, m in lc.lines), st)
        st = _last_state(b, "SOLO")
        check("shown: Einzelgeraet zeigt eigenen Wert", st["operating_mode"] == "Night"
              and st["operating_mode_raw"] == "Night", st)

        # Master wechselt -> Slave folgt in der Anzeige, neuer Hinweis
        master._status["operating_mode"] = OM.Auto
        await _one_poll(b, 3)
        st = _last_state(b, "GBAD")
        check("shown: Masterwechsel wird uebernommen", st["operating_mode"] == "Auto", st)
        notes = [m for lv, m in lc.lines if lv == "INFO" and "operating mode of GBAD" in m]
        check("shown: neuer Hinweis bei geaendertem Paar", len(notes) == 2, notes)

    # Master zu lange nicht gelesen -> eigener Wert
    b._last_mode["FLUR"] = (OM.Auto, time.monotonic() - 1000)
    st = b._state_payload("GBAD", gbad, dict(gbad._status))
    check("shown: veralteter Masterwert -> eigener Wert", st["operating_mode"] == "Surveillance", st)
    # Grenze waechst mit dem Abrufintervall: 280 s Intervall, Masterwert 400 s alt -> noch gueltig
    b.cfg.poll_interval = 280
    b._last_mode["FLUR"] = (OM.Auto, time.monotonic() - 400)
    st = b._state_payload("GBAD", gbad, dict(gbad._status))
    check("shown: Altersgrenze haengt am Abrufintervall", st["operating_mode"] == "Auto", st)
    b.cfg.poll_interval = 0.05

    # NeuraCell-X steuert das Geraet -> eigener Wert (Schutzmodus sichtbar)
    b._last_mode["FLUR"] = (OM.Smart, time.monotonic())
    orig = b.neuracell._device_under_control
    b.neuracell._device_under_control = lambda s, d=None: s == "GBAD"
    try:
        gbad._status["operating_mode"] = OM.Intake
        st = b._state_payload("GBAD", gbad, dict(gbad._status))
        check("shown: unter NeuraCell-X-Schutz eigener Wert", st["operating_mode"] == "Intake", st)
    finally:
        b.neuracell._device_under_control = orig
    # Fehler in der Schutzabfrage kostet nie den Status
    b.neuracell._device_under_control = lambda s, d=None: 1 / 0
    try:
        st = b._state_payload("GBAD", gbad, dict(gbad._status))
        check("shown: Fehler in Schutzabfrage -> eigener Wert, kein Absturz", st["operating_mode"] == "Intake", st)
    finally:
        b.neuracell._device_under_control = orig


async def test_plausibility():
    """Einzelne unmoegliche Tiefstwerte (6 % Feuchte) werden nicht veroeffentlicht.
    Anstiege, Duschspitzen, der Wechseltakt und echte trockene Luft bleiben unberuehrt."""
    cfg = bridge.BridgeConfig()
    cfg.neuracell_enabled = False
    cfg.dewpoint_enabled = False
    cfg.enable_discovery = False

    def run(b, seq, key="humidity"):
        return [b._plausible("WOZI", key, v) for v in seq]

    # A) Kundenfall: einzelner Ausreisser 6 % zwischen 55 und 70 %
    b = bridge.AmbientikaBridge(cfg)
    with _LogCatch() as lc:
        out = run(b, [55, 6, 70, 72, 68])
    check("plaus: Ausreisser 6 % wird zurueckgehalten", out == [55, 55, 70, 72, 68], out)
    held = [m for lv, m in lc.lines if "held back" in m]
    again = [m for lv, m in lc.lines if "plausible again" in m]
    check("plaus: genau ein Hinweis + eine Entwarnung", len(held) == 1 and len(again) == 1, lc.lines)
    check("plaus: Hinweis nennt Wert und Grund", held and "humidity 6 from WOZI held back (below 20 % without dry air in the last 3 readings" in held[0], held)

    # B) Anstiege und Spitzen werden nie zurueckgehalten (auch bei langem Abrufintervall)
    b = bridge.AmbientikaBridge(cfg)
    seq = [50, 70, 90, 100, 30, 100, 62, 100, 64, 99, 45]
    check("plaus: Anstiege, Spitzen, Wechseltakt unveraendert", run(b, seq) == seq, run(bridge.AmbientikaBridge(cfg), seq))

    # C) echte trockene Luft im Wechseltakt: erster trockener Wert einmal verzoegert, dann durch
    b = bridge.AmbientikaBridge(cfg)
    out = run(b, [40, 12, 41, 11, 42, 13, 40])
    check("plaus: trockene Phase bestaetigt sich", out == [40, 40, 41, 11, 42, 13, 40], out)
    # Bestaetigung auch durch einen Wert knapp ueber 20 %
    b = bridge.AmbientikaBridge(cfg)
    check("plaus: 22 % bestaetigt 15 %", run(b, [40, 22, 41, 15]) == [40, 22, 41, 15])
    # veroeffentlichte trockene Luft vor einigen Minuten zaehlt (Zeitfenster), auch nach
    # drei normalen Werten; ein nur zurueckgehaltener Wert haelt das Fenster nicht offen
    b = bridge.AmbientikaBridge(cfg)
    check("plaus: veroeffentlichte trockene Luft haelt das Zeitfenster offen",
          run(b, [40, 12, 13, 41, 45, 50, 11]) == [40, 40, 13, 41, 45, 50, 11])
    b = bridge.AmbientikaBridge(cfg)
    check("plaus: zwei einzelne Ausreisser im Abstand bestaetigen sich nicht",
          run(b, [40, 12, 41, 45, 50, 11]) == [40, 40, 41, 45, 50, 50])
    # Zeitfenster abgelaufen und keine trockene Messung unter den letzten drei -> zurueckhalten
    alt = bridge.HUMIDITY_LOW_WINDOW_S
    bridge.HUMIDITY_LOW_WINDOW_S = 0.0
    try:
        b = bridge.AmbientikaBridge(cfg)
        b.cfg.poll_interval = 0
        check("plaus: Zeitfenster abgelaufen -> gehalten",
              run(b, [40, 12, 13, 41, 45, 50, 11]) == [40, 40, 13, 41, 45, 50, 50])
    finally:
        bridge.HUMIDITY_LOW_WINDOW_S = alt
    # schneller Abruf: Aussenphase ueber mehrere Werte -> nur der erste verzoegert
    b = bridge.AmbientikaBridge(cfg)
    check("plaus: mehrere trockene Werte am Stueck", run(b, [45, 14, 13, 15, 44, 46, 15]) == [45, 45, 13, 15, 44, 46, 15])

    # D) Start mit einem Ausreisser: wird nicht veroeffentlicht
    b = bridge.AmbientikaBridge(cfg)
    check("plaus: Ausreisser als erster Wert -> nichts veroeffentlicht", run(b, [6, 55, 56]) == [None, 55, 56])

    # E) harte Grenzen
    b = bridge.AmbientikaBridge(cfg)
    out = run(b, [50, 0, 101, 52])
    check("plaus: 0 % und 101 % verworfen", out == [50, 50, 50, 52], out)
    out = run(b, [20, -45, -40, 85, 86, 21], key="temperature")
    check("plaus: Temperaturgrenzen -40..85", out == [20, 20, -40, 85, 85, 21], out)
    out = run(b, [True, 53])
    check("plaus: bool wird nie als Messwert veroeffentlicht", out == [52, 53], out)
    out = run(b, [float("nan"), "x", 54])
    check("plaus: NaN und Text verworfen", out == [53, 53, 54], out)

    # F) toter Sensor: ein Hinweis, nach der Haltezeit 'unbekannt', kein Logspam
    alt = bridge.PLAUSIBLE_HOLD_S
    bridge.PLAUSIBLE_HOLD_S = 0.0
    try:
        b = bridge.AmbientikaBridge(cfg)
        with _LogCatch() as lc:
            out = run(b, [55] + [0] * 50)
        check("plaus: toter Sensor -> unbekannt statt eingefrorenem Wert",
              out[0] == 55 and all(v is None for v in out[1:]), out[:5])
        check("plaus: hoechstens zwei Logzeilen fuer 50 Ausfaelle", len(lc.lines) <= 2, lc.lines)
    finally:
        bridge.PLAUSIBLE_HOLD_S = alt

    # G) None bleibt None ohne Log; ueber den Payload-Pfad
    b = bridge.AmbientikaBridge(cfg)
    with _LogCatch() as lc:
        r = b._plausible("X", "humidity", None)
    check("plaus: None bleibt None ohne Log", r is None and not lc.lines, lc.lines)
    dev = FakeDevice(serial="WOZI", name="Wohnzimmer", zone=0, status=mkstatus(op=OM.Smart))
    dev.role = "Master"
    vals = []
    for h in (55, 6, 70):
        dev._status["humidity"] = h
        vals.append(b._state_payload("WOZI", dev, dict(dev._status))["humidity"])
    check("plaus: Payload nutzt den Filter", vals == [55, 55, 70], vals)


async def test_zone_master_houses_and_order():
    """Zone 0 gibt es in jedem Haus: ein Slave folgt nur dem Master seines Hauses.
    Master werden zuerst abgefragt, damit Slaves den Modus desselben Durchlaufs zeigen."""
    cfg = bridge.BridgeConfig()
    cfg.neuracell_enabled = False
    cfg.dewpoint_enabled = False
    cfg.enable_discovery = False
    cfg.poll_interval = 0.05
    b = bridge.AmbientikaBridge(cfg)
    b.client = FakeClient()
    b.loop = asyncio.get_running_loop()
    ma = FakeDevice(serial="MA", name="Master A", zone=0, status=mkstatus(op=OM.Off))
    ma.role = "Master"
    sb = FakeDevice(serial="SB", name="Slave B", zone=0, status=mkstatus(op=OM.Surveillance))
    mb = FakeDevice(serial="MB", name="Master B", zone=0, status=mkstatus(op=OM.Smart))
    mb.role = "Master"
    b.devices = {"MA": ma, "SB": sb, "MB": mb}
    b._device_house = {"MA": 1, "SB": 2, "MB": 2}
    check("houses: Master im selben Haus gefunden", b._zone_master(sb) is mb, b._zone_master(sb))
    await _one_poll(b, 1)
    firsts = [json.loads(pl) for t, pl in b.client.pub if t.endswith("/SB/state")]
    check("houses: Slave zeigt Master seines Hauses, schon im ersten Durchlauf",
          firsts and firsts[0]["operating_mode"] == "Smart", firsts[:1])
    order = [t.split("/")[1] for t, pl in b.client.pub if t.endswith("/state")][:3]
    check("order: Master vor Slave abgefragt", order.index("MB") < order.index("SB"), order)
    b._device_house = {"MA": 1, "SB": 3, "MB": 2}
    check("houses: kein Master im Haus -> None", b._zone_master(sb) is None)


async def test_ack_publish_now():
    """Quittung: der wirksame Wert wird sofort veroeffentlicht, nicht erst beim naechsten Abruf."""
    old_env = {k: os.environ.get(k) for k in ("SLAVE_FILTER_SOFT_RESET", "FILTER_ACK_PATH")}
    os.environ["SLAVE_FILTER_SOFT_RESET"] = "1"
    os.environ["FILTER_ACK_PATH"] = os.path.join(_tempfile.mkdtemp(), "filter_ack.json")
    alt = bridge.FILTER_RESET_VERIFY_DELAY
    bridge.FILTER_RESET_VERIFY_DELAY = 0.01
    try:
        cfg = bridge.BridgeConfig()
        cfg.neuracell_enabled = False
        cfg.dewpoint_enabled = False
        cfg.enable_discovery = False
        b = bridge.AmbientikaBridge(cfg)
        b.client = FakeClient()
        b.loop = asyncio.get_running_loop()
        master = FakeDevice(serial="FLUR", name="Flur", zone=0, status=mkstatus(op=OM.Smart))
        master.role = "Master"
        master._status["filters_status"] = "Bad"
        wozi = FakeDevice(serial="WOZI", name="Wohnzimmer", zone=0, status=mkstatus(op=OM.Smart))
        wozi._status["filters_status"] = "Bad"
        b.devices = {"FLUR": master, "WOZI": wozi}

        async def fake_req(device, method, path, body):
            return (200, None, "")
        b._reset_request = fake_req
        with _LogCatch() as lc:
            res = await b._reset_filter(wozi)
        st = _last_state(b, "WOZI")
        check("ack-now: Ergebnis acknowledged", res == "acknowledged", res)
        check("ack-now: Zustand sofort veroeffentlicht",
              st and st["filters_status"] == "Good" and st["filters_status_raw"] == "Bad"
              and st["filter_status_num"] == 0, st)
        check("ack-now: Logzeile sagt published", lc.has("INFO", "stays unchanged (published)"), lc.lines)
        # Quittung laesst sich nicht speichern -> nicht 'published' behaupten
        os.environ["FILTER_ACK_PATH"] = "/proc/nicht/schreibbar/filter_ack.json"
        with _LogCatch() as lc:
            res = await b._reset_filter(wozi)
        st = _last_state(b, "WOZI")
        check("ack-now: Speichern fehlgeschlagen -> Ergebnis unconfirmed", res == "unconfirmed", res)
        check("ack-now: Speichern fehlgeschlagen -> Warnung statt 'recorded'",
              lc.has("WARNING", "could not be stored") and not lc.has("INFO", "recorded bridge-side"), lc.lines)
        os.environ["FILTER_ACK_PATH"] = os.path.join(_tempfile.mkdtemp(), "filter_ack.json")
        # alter, abgelaufener Eintrag + Speichern scheitert still -> nicht als gespeichert werten
        AB = bridge.AmbientikaBridge
        AB._filter_ack_save({"WOZI": {"acked_at": 1.0, "raw_when_acked": "Bad"}})
        orig_save = AB._filter_ack_save
        AB._filter_ack_save = classmethod(lambda cls, data: None)
        try:
            check("ack-now: stilles Speicherversagen neben altem Eintrag erkannt",
                  AB._filter_ack_write("WOZI", "Bad") is False)
        finally:
            AB._filter_ack_save = orig_save
        # ohne MQTT-Verbindung: ehrlicher Hinweis statt Behauptung
        b.client = None
        with _LogCatch() as lc:
            res = await b._reset_filter(wozi)
        check("ack-now: ohne Verbindung -> naechster Abruf",
              res == "acknowledged" and lc.has("INFO", "published with the next poll"), lc.lines)
        # Reset greift wirklich (Zaehler am Geraet geloescht) -> gespeicherte Quittung wird vergessen
        b.client = FakeClient()
        check("ack-now: Quittung vor dem Test gespeichert", "WOZI" in AB._filter_ack_load())

        async def clearing_req(device, method, path, body):
            wozi._status["filters_status"] = "Good"
            return (200, None, "")
        b._reset_request = clearing_req
        with _LogCatch() as lc:
            res = await b._reset_filter(wozi)
        check("ack-now: bestaetigter Reset -> confirmed und Quittung entfernt",
              res == "confirmed" and "WOZI" not in AB._filter_ack_load()
              and lc.has("INFO", "removed: the counter really cleared"), (res, lc.lines))
        # ueber _start_reset (echter Ablauf): running -> ... -> Endzustand, Sync greift nicht dazwischen
        wozi._status["filters_status"] = "Bad"
        b._reset_request = fake_req
        b.client = FakeClient()
        b._start_reset(wozi)
        await asyncio.sleep(0.2)
        seq = [pl for tp, pl in b.client.pub if tp == bridge.reset_state_topic("ambientika", "WOZI")]
        check("ack-now: _start_reset -> running, dann acknowledged, nichts dazwischen",
              seq == ["running", "acknowledged"], seq)
    finally:
        bridge.FILTER_RESET_VERIFY_DELAY = alt
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        bridge._FILTER_ACK_GOOD_STREAK.clear()
        bridge._FILTER_ACK_ODD_RAW.clear()


def test_discovery_raw_mode():
    cfg = bridge.BridgeConfig()
    ents = bridge.build_discovery_configs(cfg, "AMB-2", "Kitchen")
    uids = [p["unique_id"] for _, p in ents]
    raw = next((p for t, p in ents if t.endswith("AMB-2_operating_mode_raw/config")), None)
    rawn = next((p for t, p in ents if t.endswith("AMB-2_operating_mode_raw_num/config")), None)
    check("disc: Mode raw vorhanden", raw and raw["value_template"] == "{{ value_json.operating_mode_raw }}", raw)
    check("disc: Mode raw (num) mit measurement", rawn and rawn.get("state_class") == "measurement", rawn)
    check("disc: unique_ids weiterhin eindeutig", len(uids) == len(set(uids)))


async def test_live_roles():
    """Die Rolle aus dem Status des Geraets zaehlt, nicht die vom Start:
    zurueckgesetzte Geraete behalten in der Cloud ihren Zonen-Index, neu gekoppelte
    ihre alte Rolle - beides darf keinen falschen Modus anzeigen."""
    cfg = bridge.BridgeConfig()
    cfg.neuracell_enabled = False
    cfg.dewpoint_enabled = False
    cfg.enable_discovery = False
    cfg.poll_interval = 0.05
    b = bridge.AmbientikaBridge(cfg)
    b.client = FakeClient()
    b.loop = asyncio.get_running_loop()
    flur = FakeDevice(serial="FLUR", name="Flur", zone=0, status=mkstatus(op=OM.Smart))
    flur.role = "Master"
    gbad = FakeDevice(serial="GBAD", name="Gaeste-Bad", zone=0, status=mkstatus(op=OM.Surveillance))
    gbad.role = "SlaveOppositeMaster"                  # echte Rollenbezeichnung der Cloud
    kizi = FakeDevice(serial="KIZI", name="Kinderzimmer", zone=0, status=mkstatus(op=OM.Night))
    kizi.role = "SlaveEqualMaster"
    reset = FakeDevice(serial="RSET", name="Zurueckgesetzt", zone=0, status=mkstatus(op=OM.Auto))
    reset.role = None                                  # Cloud: Rolle geloescht, Zone 0 bleibt
    reset._status["device_role"] = "NotConfigured"     # das Geraet selbst meldet das
    b.devices = {d.serial_number: d for d in (gbad, reset, flur, kizi)}
    await _one_poll(b, 2)
    st = {sn: _last_state(b, sn) for sn in b.devices}
    check("live: SlaveOppositeMaster zeigt Master", st["GBAD"]["operating_mode"] == "Smart", st["GBAD"])
    check("live: SlaveEqualMaster zeigt Master", st["KIZI"]["operating_mode"] == "Smart", st["KIZI"])
    check("live: zurueckgesetztes Geraet in Zone 0 zeigt eigenen Wert",
          st["RSET"]["operating_mode"] == "Auto" and st["RSET"]["operating_mode_raw"] == "Auto", st["RSET"])
    check("live: zurueckgesetztes Geraet hat keinen Zonen-Master", b._zone_master(reset) is None)
    check("live: Master zeigt eigenen Wert", st["FLUR"]["operating_mode"] == "Smart")

    # Neu gekoppelt in der App, Bridge nicht neu gestartet: KIZI meldet jetzt Master,
    # FLUR meldet jetzt SlaveOppositeMaster - die Startrollen sind veraltet.
    kizi._status["device_role"] = "Master"
    flur._status["device_role"] = "SlaveOppositeMaster"
    kizi._status["operating_mode"] = OM.ManualHeatRecovery
    flur._status["operating_mode"] = OM.Off
    await _one_poll(b, 3)
    st = {sn: _last_state(b, sn) for sn in b.devices}
    check("live: neuer Master zeigt eigenen Wert", st["KIZI"]["operating_mode"] == "ManualHeatRecovery", st["KIZI"])
    check("live: alter Master ist jetzt Slave und zeigt den neuen Master",
          st["FLUR"]["operating_mode"] == "ManualHeatRecovery" and st["FLUR"]["operating_mode_raw"] == "Off", st["FLUR"])
    check("live: anderer Slave folgt dem neuen Master", st["GBAD"]["operating_mode"] == "ManualHeatRecovery", st["GBAD"])
    check("live: Zonen-Master nach Umkopplung ist KIZI", b._zone_master(flur) is kizi)
    # Reihenfolge: der neue Master wird im naechsten Durchlauf zuerst abgefragt
    b.client.pub.clear()
    await _one_poll(b, 1)
    order = [t.split("/")[1] for t, pl in b.client.pub if t.endswith("/state")]
    check("live: neuer Master zuerst abgefragt", order and order[0] == "KIZI", order)
    # Filter-Reset-Ziel folgt ebenfalls der Live-Rolle
    cands = [sn for sn, _ in b._reset_candidates(flur)]
    check("live: Reset-Kandidaten nach Live-Rolle", cands == ["FLUR", "KIZI"], cands)
    # Status ohne Rolle (None) aendert die gemerkte Rolle nicht
    kizi._status["device_role"] = None
    await _one_poll(b, 1)
    check("live: Status ohne Rolle laesst die gemerkte Rolle stehen", b._role_of(kizi) == "master")
    # Rollennamen enthalten "Master": ein SlaveEqualMaster ist nie ein Master
    check("live: SlaveEqualMaster ist kein Master", b._role_of(gbad).startswith("slave")
          and all(b._zone_master(d) is not gbad for d in b.devices.values()))


async def test_duplicate_packets():
    """Die Cloud liefert das letzte Paket des Geraets; bei kurzem Abrufintervall
    wird dasselbe Paket mehrfach gelesen. Ein zurueckgehaltener Wert darf sich so
    nicht selbst bestaetigen, und ein Wiederholungspaket zaehlt nirgends doppelt."""
    cfg = bridge.BridgeConfig()
    cfg.neuracell_enabled = False
    cfg.dewpoint_enabled = False
    cfg.enable_discovery = False
    b = bridge.AmbientikaBridge(cfg)
    dev = FakeDevice(serial="WOZI", name="Wohnzimmer", zone=0, status=mkstatus(op=OM.Smart))
    dev.role = "Master"

    def poll(h, t=22):
        dev._status["humidity"] = h
        dev._status["temperature"] = t
        return b._state_payload("WOZI", dev, dict(dev._status))["humidity"]

    # Kundenfall bei 10 s Abruf: das 6-%-Paket wird dreimal gelesen
    with _LogCatch() as lc:
        out = [poll(55), poll(6), poll(6), poll(6), poll(70), poll(70), poll(68)]
    check("dup: Ausreisser bleibt bei Wiederholung zurueckgehalten", out == [55, 55, 55, 55, 70, 70, 68], out)
    held = [m for lv, m in lc.lines if lv == "INFO" and "held back" in m]
    check("dup: Hinweis trotz drei Lesungen nur einmal", len(held) == 1, lc.lines)
    # echte trockene Luft: das erste neue Paket bestaetigt, Wiederholungen nicht
    b = bridge.AmbientikaBridge(cfg)
    out = [poll(40), poll(12), poll(12), poll(12), poll(13), poll(13), poll(41), poll(11)]
    check("dup: trockene Luft wird vom naechsten neuen Paket bestaetigt",
          out == [40, 40, 40, 40, 13, 13, 41, 11], out)
    # gleiches Paket, aber anderer Wert in einem anderen Feld -> neues Paket
    b = bridge.AmbientikaBridge(cfg)
    out = [poll(40), poll(12, 22), poll(12, 21)]
    check("dup: Paket mit anderer Temperatur gilt als neu", out == [40, 40, 12], out)
    # Ein seit Minuten unveraendertes Paket ist kein Wiederholungspaket mehr: ein
    # stillstehendes Geraet meldet dieselben Werte wirklich, und sie zaehlen
    b = bridge.AmbientikaBridge(cfg)
    out = [poll(26), poll(18), poll(18)]
    check("dup: gleiches Paket kurz danach -> gehalten", out == [26, 26, 26], out)
    d0, t0 = b._last_status["WOZI"]
    b._last_status["WOZI"] = (d0, t0 - bridge.DUPLICATE_PACKET_MAX_S - 1)
    out = [poll(18), poll(18)]
    check("dup: gleiches Paket nach mehr als 2 min gilt als neu und bestaetigt", out == [18, 18], out)
    # Haltezeit laeuft auch bei Wiederholungspaketen ab
    alt = bridge.PLAUSIBLE_HOLD_S
    bridge.PLAUSIBLE_HOLD_S = 0.0
    try:
        b = bridge.AmbientikaBridge(cfg)
        with _LogCatch() as lc:
            out = [poll(55), poll(0), poll(0), poll(0)]
        check("dup: Haltezeit abgelaufen -> unbekannt auch bei gleichem Paket",
              out == [55, None, None, None], out)
        check("dup: 'unbekannt' einmal im Log", sum("published as unknown" in m for _, m in lc.lines) == 1, lc.lines)
    finally:
        bridge.PLAUSIBLE_HOLD_S = alt


async def test_log_rate_limit():
    """Ein flatternder Sensor (0 % / 55 % / 0 % ...) schreibt nicht bei jedem Wechsel ins Log."""
    cfg = bridge.BridgeConfig()
    cfg.neuracell_enabled = False
    cfg.dewpoint_enabled = False
    cfg.enable_discovery = False
    b = bridge.AmbientikaBridge(cfg)
    with _LogCatch() as lc:
        out = [b._plausible("X", "humidity", v) for v in [55, 0] * 100]
    info = [m for lv, m in lc.lines if lv == "INFO"]
    check("rate: 200 Flatterwerte -> genau zwei INFO-Zeilen (Beginn und Entwarnung)",
          len(info) == 2 and "held back" in info[0] and "plausible again" in info[1], info)
    check("rate: Werte trotzdem richtig", out == [55] * 200, out[:4])
    # Ein einzelnes Ereignis: beide Zeilen INFO
    b2 = bridge.AmbientikaBridge(cfg)
    with _LogCatch() as lc:
        [b2._plausible("Y", "humidity", v) for v in [55, 0, 55]]
    check("rate: einzelnes Ereignis -> Beginn und Entwarnung auf INFO",
          [lv for lv, _ in lc.lines] == ["INFO", "INFO"], lc.lines)
    # Einmal je Stunde darf wieder gemeldet werden
    st = b._last_plausible["X"]["humidity"]
    st["log_t"] = time.monotonic() - 3601
    with _LogCatch() as lc:
        b._plausible("X", "humidity", 55)      # Entwarnung des leisen Ereignisses -> DEBUG
        b._plausible("X", "humidity", 0)       # neues Ereignis nach einer Stunde -> INFO
        b._plausible("X", "humidity", 55)      # seine Entwarnung -> INFO
    check("rate: nach einer Stunde wieder INFO (Beginn und Entwarnung)",
          [lv for lv, _ in lc.lines] == ["DEBUG", "INFO", "INFO"], lc.lines)


async def test_mode_confirmed_slave_note():
    """Die Bestaetigungszeile fuer einen Slave sagt, dass der gezeigte Modus der des Masters bleibt."""
    cfg = bridge.BridgeConfig()
    cfg.neuracell_enabled = False
    cfg.dewpoint_enabled = False
    cfg.enable_discovery = False
    b = bridge.AmbientikaBridge(cfg)
    b.client = FakeClient()
    b.loop = asyncio.get_running_loop()
    master = FakeDevice(serial="FLUR", name="Flur", zone=0, status=mkstatus(op=OM.Smart))
    master.role = "Master"
    slave = FakeDevice(serial="GBAD", name="Gaeste-Bad", zone=0, status=mkstatus(op=OM.Surveillance))
    b.devices = {"FLUR": master, "GBAD": slave}
    with _LogCatch() as lc:
        await b._handle_command("GBAD", "operating_mode", "Night")
        b._check_mode_applied("GBAD", slave, OM.Night)
        await b._handle_command("FLUR", "operating_mode", "Night")
        b._check_mode_applied("FLUR", master, OM.Night)
    conf = [m for lv, m in lc.lines if "confirmed on" in m]
    check("confirmed: Slave-Zeile nennt den Master", len(conf) == 2 and "SLAVE" in conf[0] and "FLUR" in conf[0], conf)
    check("confirmed: Master-Zeile unveraendert", conf[1] == "operating mode Night confirmed on FLUR", conf)


class _Clock:
    """Ersetzt time.monotonic der Bridge fuer Zeitreihen ohne echtes Warten."""

    def __init__(self, t=10000.0):
        self.t = t
        self._real = None

    def __enter__(self):
        self._real = bridge.time.monotonic
        bridge.time.monotonic = lambda: self.t
        return self

    def __exit__(self, *a):
        bridge.time.monotonic = self._real


def _avg_bridge(poll=30):
    cfg = bridge.BridgeConfig()
    cfg.neuracell_enabled = False
    cfg.dewpoint_enabled = False
    cfg.enable_discovery = False
    cfg.poll_interval = poll
    b = bridge.AmbientikaBridge(cfg)
    dev = FakeDevice(serial="BAD", name="Gaeste-Bad", zone=0, status=mkstatus(op=OM.Smart))
    dev.role = "Master"
    return b, dev


def _avg_poll(b, dev, clock, h, t=21, dt=None):
    if dt is not None:
        clock.t += dt
    dev._status["humidity"] = h
    dev._status["temperature"] = t
    return b._state_payload("BAD", dev, dict(dev._status))


async def test_average():
    """Gemittelte Sensoren (humidity_avg / temperature_avg): stabiler Wert ueber den
    Wechseltakt, Rohwert unveraendert daneben (Vorschlag Kunde, Bridge 1.4.33)."""
    # A) Kundenfall: Wechsel 90 % / 56 % bei jedem Abruf (30 s) -> Mittel 73 %
    b, dev = _avg_bridge(30)
    with _Clock() as c:
        out = [_avg_poll(b, dev, c, 90 if i % 2 == 0 else 56, dt=30) for i in range(60)]
    raw = [p["humidity"] for p in out]
    avg = [p["humidity_avg"] for p in out]
    check("avg: Rohwert bleibt unveraendert (90/56 im Wechsel)",
          raw == [90 if i % 2 == 0 else 56 for i in range(60)], raw[:6])
    check("avg: vor 2 min Abdeckung noch unbekannt", avg[:3] == [None, None, None], avg[:5])
    check("avg: ab 4 Abrufen veroeffentlicht", all(v is not None for v in avg[3:]), avg[3:8])
    check("avg: Mittel 73 %, ueber das volle Fenster stabil (+-1)",
          all(72.0 <= v <= 74.0 for v in avg[20:]), avg[20:])
    check("avg: eine Nachkommastelle", all(v == round(v, 1) for v in avg[3:]))
    check("avg: Temperatur gemittelt (konstant 21)", all(p["temperature_avg"] == 21.0 for p in out[3:]),
          [p["temperature_avg"] for p in out[:6]])

    # B) kurzes Abrufintervall: dasselbe Paket wird dreimal gelesen (Geraet sendet alle
    #    30 s, Abruf alle 10 s) - jede Lesung zaehlt nur fuer ihre 10 s, kein Ungleichgewicht
    b, dev = _avg_bridge(10)
    with _Clock() as c:
        out = [_avg_poll(b, dev, c, 90 if (i // 3) % 2 == 0 else 56, dt=10) for i in range(180)]
    avg = [p["humidity_avg"] for p in out]
    check("avg: Wiederholungspakete gewichten richtig (Mittel 73 +-1)",
          all(72.0 <= v <= 74.0 for v in avg[60:]), (min(avg[60:]), max(avg[60:])))

    # C) Zusatzveroeffentlichung zwischen zwei Abrufen (z. B. Quittung) verschiebt nichts
    b, dev = _avg_bridge(30)
    with _Clock() as c:
        for i in range(40):
            _avg_poll(b, dev, c, 90 if i % 2 == 0 else 56, dt=30)
            if i % 5 == 0:
                _avg_poll(b, dev, c, 90 if i % 2 == 0 else 56, dt=0.5)
        v = _avg_poll(b, dev, c, 90, dt=29.5)["humidity_avg"]
    check("avg: Zusatzveroeffentlichung ohne Einfluss", 72.0 <= v <= 74.0, v)

    # D) zurueckgehaltener Ausreisser (6 %) zaehlt nicht ins Mittel
    b, dev = _avg_bridge(30)
    with _Clock() as c:
        out = [_avg_poll(b, dev, c, h, dt=30) for h in [55] * 10 + [6] + [55] * 5]
    check("avg: Ausreisser 6 % nicht im Mittel", all(p["humidity_avg"] == 55.0 for p in out[3:]),
          [p["humidity_avg"] for p in out])
    check("avg: Rohwert haelt wie bisher den letzten plausiblen Wert",
          [p["humidity"] for p in out] == [55] * 16)

    # E) Duschspitze: Rohwert springt sofort, Mittel steigt verzoegert und faellt wieder
    b, dev = _avg_bridge(30)
    with _Clock() as c:
        seq = [60] * 30 + [95] * 6 + [60] * 70
        out = [_avg_poll(b, dev, c, h, dt=30) for h in seq]
    avg = [p["humidity_avg"] for p in out]
    check("avg: Duschspitze im Rohwert sofort", out[30]["humidity"] == 95)
    check("avg: Mittel steigt mit der Spitze, aber gedaempft",
          60.0 < avg[35] < 95.0 and avg[35] == max(avg[3:]), (avg[29:40], max(avg[3:])))
    check("avg: nach dem Fenster wieder 60", avg[-1] == 60.0, avg[-5:])

    # F) toter Sensor: nach dem Fenster ohne plausiblen Wert -> unbekannt
    alt = bridge.PLAUSIBLE_HOLD_S
    bridge.PLAUSIBLE_HOLD_S = 0.0
    try:
        b, dev = _avg_bridge(30)
        with _Clock() as c:
            out = [_avg_poll(b, dev, c, h, dt=30) for h in [55] * 10 + [0] * 50]
        avg = [p["humidity_avg"] for p in out]
        check("avg: toter Sensor -> Rohwert sofort unbekannt", out[10]["humidity"] is None)
        check("avg: Mittel nur aus echten Werten, dann unbekannt",
              avg[10] == 55.0 and avg[-1] is None, avg[8:])
    finally:
        bridge.PLAUSIBLE_HOLD_S = alt
    # toter Sensor mit normaler Haltezeit: Mittel wird nach 10 min unbekannt wie der Rohwert
    b, dev = _avg_bridge(30)
    with _Clock() as c:
        out = [_avg_poll(b, dev, c, h, dt=30) for h in [55] * 30 + [0] * 25]
    raw = [p["humidity"] for p in out]
    avg = [p["humidity_avg"] for p in out]
    # (der Rohwert zaehlt die Haltezeit ab dem letzten neuen Paket, das Mittel ab der
    #  letzten gezaehlten Lesung - beide sind spaetestens 10,5 min nach dem letzten
    #  plausiblen Wert unbekannt)
    check("avg: toter Sensor -> Mittel nach 10 min unbekannt wie der Rohwert",
          avg[45] == 55.0 and all(v is None for v in avg[50:]) and all(v is None for v in raw[50:]),
          (raw[44:52], avg[44:52]))
    alt = bridge.PLAUSIBLE_HOLD_S
    try:
        pass
    finally:
        bridge.PLAUSIBLE_HOLD_S = alt

    # G) langes Abrufintervall (300 s): Fenster mind. 10 Abrufe, nach 2 Abrufen sichtbar
    b, dev = _avg_bridge(300)
    with _Clock() as c:
        out = [_avg_poll(b, dev, c, 90 if i % 2 == 0 else 56, dt=300) for i in range(40)]
    # Fenster = max(1800 s, 20 x 300 s) = 100 min
    avg = [p["humidity_avg"] for p in out]
    check("avg: 300 s Abruf -> ab dem 2. Abruf ein Wert", avg[0] is None and avg[1] is not None, avg[:3])
    check("avg: 300 s Abruf -> Fenster 20 Abrufe (100 min), danach stabil 73",
          all(v == 73.0 for v in avg[20:]) and all(72.0 <= v <= 75.0 for v in avg[10:]), avg[10:])

    # H) Ausfall: nach langer Luecke zaehlt der erste neue Wert nicht fuer die ganze Luecke
    b, dev = _avg_bridge(30)
    with _Clock() as c:
        for _ in range(80):
            _avg_poll(b, dev, c, 40, dt=30)
        a1 = _avg_poll(b, dev, c, 80, dt=420)["humidity_avg"]    # 7 min ohne Abruf
    # Fenster 1800 s: 40 % fuer die 1380 s vor der Luecke, 80 % nur fuer 60 s (2 Abrufe)
    check("avg: Luecke wird nicht dem neuen Wert zugeschlagen", a1 == round((40 * 1380 + 80 * 60) / 1440, 1), a1)

    # I) Werte aus Text, bool und NaN zaehlen nie (direkt ueber _average)
    b, _ = _avg_bridge(30)
    r = [b._average("X", "humidity", v, now=1000.0 + 30 * i)
         for i, v in enumerate([50, True, float("nan"), "x", 50, 50, 50, 50])]
    check("avg: bool/NaN/Text sind Luecken", r[-1] == 50.0 and r[3] is None, r)

    # J) Slave: eigener Sensor, eigenes Mittel (kein Master-Wert)
    b, dev = _avg_bridge(30)
    with _Clock() as c:
        for _ in range(6):
            _avg_poll(b, dev, c, 70, dt=30)
            b._state_payload("OTHER", dev, dict(dev._status, humidity=40))
    check("avg: getrennt je Geraet",
          b._average("BAD", "humidity", 70, now=c.t + 30) == 70.0
          and b._average("OTHER", "humidity", 40, now=c.t + 30) == 40.0)

    # K) Payload als JSON: unbekannt -> null (HA: unknown)
    b, dev = _avg_bridge(30)
    with _Clock() as c:
        p = _avg_poll(b, dev, c, 55, dt=30)
    js = json.loads(json.dumps(p))
    check("avg: Felder im Payload, anfangs null", "humidity_avg" in js and js["humidity_avg"] is None
          and "temperature_avg" in js and js["temperature_avg"] is None, js)

    # L) Speicher bleibt begrenzt
    b, dev = _avg_bridge(10)
    with _Clock() as c:
        for i in range(5000):
            _avg_poll(b, dev, c, 50 + i % 7, dt=10)
    n = len(b._avg_buf["BAD"]["humidity"])
    check("avg: Puffer auf das Fenster begrenzt", n <= 181 + 1, n)


def test_average_discovery_and_env():
    cfg = bridge.BridgeConfig()
    ents = dict(bridge.build_discovery_configs(cfg, "AMB-2", "Kitchen"))
    h = ents.get("homeassistant/sensor/AMB-2_humidity_avg/config")
    t = ents.get("homeassistant/sensor/AMB-2_temperature_avg/config")
    check("disc: Humidity (averaged)", h and h["name"] == "Humidity (averaged)"
          and h["value_template"] == "{{ value_json.humidity_avg }}"
          and h["device_class"] == "humidity" and h["unit_of_measurement"] == "%"
          and h["state_class"] == "measurement" and h["unique_id"] == "ambientika_AMB-2_humidity_avg", h)
    check("disc: Temperature (averaged)", t and t["name"] == "Temperature (averaged)"
          and t["device_class"] == "temperature" and t["unit_of_measurement"] == "°C"
          and t["state_class"] == "measurement", t)
    raw = ents.get("homeassistant/sensor/AMB-2_humidity/config")
    check("disc: roher Feuchtesensor unveraendert", raw and raw["unique_id"] == "ambientika_AMB-2_humidity"
          and raw["value_template"] == "{{ value_json.humidity }}")
    check("avg: Standardfenster 30 min", bridge.AVERAGE_WINDOW_S == 1800.0, bridge.AVERAGE_WINDOW_S)
    import subprocess
    code = ("import importlib.util,sys;s=importlib.util.spec_from_file_location('b',sys.argv[1]);"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);print(m.AVERAGE_WINDOW_S)")
    res = {}
    for val in ("5", "abc", "0", "2000", "", "nan"):
        env = dict(os.environ, AVERAGE_WINDOW_MIN=val)
        r = subprocess.run([sys.executable, "-c", code, os.path.join(HERE, "bridge.py")],
                           env=env, capture_output=True, text=True)
        res[val] = r.stdout.strip() or r.stderr[-200:]
    check("avg: AVERAGE_WINDOW_MIN wird gelesen und begrenzt",
          res == {"5": "300.0", "abc": "1800.0", "0": "60.0", "2000": "86400.0", "": "1800.0", "nan": "1800.0"}, res)


class _LogCapture:
    """Sammelt die Logzeilen der Bridge (Level >= INFO) fuer Pruefungen."""

    def __init__(self):
        import logging
        self.lines = []
        self._h = logging.Handler()
        self._h.emit = lambda r: self.lines.append((r.levelname, r.getMessage()))
        self._h.setLevel(logging.INFO)
        self._lvl = None

    def __enter__(self):
        import logging
        self._lvl = bridge.log.level
        bridge.log.addHandler(self._h)
        if bridge.log.getEffectiveLevel() > logging.INFO:
            bridge.log.setLevel(logging.INFO)
        return self

    def __exit__(self, *a):
        bridge.log.removeHandler(self._h)
        bridge.log.setLevel(self._lvl)

    def has(self, level, *parts):
        return any(lv == level and all(p in msg for p in parts) for lv, msg in self.lines)


def _house_bridge(names, poll=30, awd=""):
    """Bridge mit Geraeten wie beim Kunden (Name -> Serial = Name in Grossbuchstaben)."""
    cfg = bridge.BridgeConfig()
    cfg.neuracell_enabled = False
    cfg.dewpoint_enabled = False
    cfg.enable_discovery = False
    cfg.poll_interval = poll
    cfg.average_window_devices = awd
    b = bridge.AmbientikaBridge(cfg)
    devs = {}
    for i, n in enumerate(names):
        d = FakeDevice(serial="SN-%d" % i, name=n, zone=0, status=mkstatus(op=OM.Smart))
        d.role = "Master"
        devs[d.serial_number] = d
    b.devices = devs
    return b, devs


def test_average_per_device():
    """Fenster je Geraet (average_window_devices), Vorschlag Kunde, Bridge 1.4.34."""
    import unicodedata as _ud
    C = bridge.BridgeConfig

    def parse(s):
        c = C()
        c.average_window_devices = s
        return c.average_window_entries()

    # A) Parsen
    e, p = parse("Gäste-Bad=15, Schlafzimmer Eltern=60; AMB-9:45")
    # Schluessel: Bindestrich/Unterstrich wie Leerzeichen (seit 1.4.35 - Kundengeraet
    # hiess "Gäste Bad", das Doku-Beispiel "Gäste-Bad")
    check("win: drei Eintraege, keine Probleme",
          [(k, m) for k, _, m in e] == [("gäste bad", 15.0), ("schlafzimmer eltern", 60.0), ("amb 9", 45.0)]
          and p == [], (e, p))
    check("win: Schluessel - Bindestrich, Unterstrich, Leerzeichen gleich",
          len({bridge._name_key(v) for v in ("Gäste-Bad", "Gäste Bad", "gäste_bad", " GÄSTE - BAD ")}) == 1
          and bridge._name_key("Gäste-Bad") != bridge._name_key("Gästebad"),
          [bridge._name_key(v) for v in ("Gäste-Bad", "Gäste Bad", "gäste_bad", "Gästebad")])
    check("win: Bezeichnung wie eingegeben", [lbl for _, lbl, _ in e] == ["Gäste-Bad", "Schlafzimmer Eltern", "AMB-9"], e)
    for s in ("", "  ", " , ; ", None):
        e, p = parse(s)
        check("win: leer %r -> nichts" % (s,), e == [] and p == [], (e, p))
    e, p = parse("Bad, =10, Bad=, Küche=abc, Flur=nan, Keller=inf, WC=-inf")
    check("win: unbrauchbare Eintraege ignoriert, je einmal gemeldet", e == [] and len(p) == 7, (e, p))
    e, p = parse("Bad=0, Flur=500, WC=7.5, Keller= 20 ")
    check("win: ausserhalb 1-240 begrenzt, Kommazahl und Leerzeichen ok",
          [m for _, _, m in e] == [1.0, 240.0, 7.5, 20.0] and len(p) == 2
          and "using 1" in p[0] and "using 240" in p[1], (e, p))
    e, p = parse("Bad:EG=20, Bad OG : 30")
    check("win: letztes Trennzeichen zaehlt, Namen mit ':'",
          [(lbl, m) for _, lbl, m in e] == [("Bad:EG", 20.0), ("Bad OG", 30.0)], e)
    e, p = parse("Bad=15 min\nWC=20min\r\nFlur = 45 MIN")
    check("win: Zeilenumbruch trennt, 'min' hinter der Zahl erlaubt",
          [(lbl, m) for _, lbl, m in e] == [("Bad", 15.0), ("WC", 20.0), ("Flur", 45.0)] and p == [], (e, p))
    e, p = parse("A=B:15")
    check("win: mit '=' trennt immer '=' (Name mit '=' -> Seriennummer)", e == [] and len(p) == 1, (e, p))
    for v in (15, True, ["Bad=15"], {"Bad": 15}):
        try:
            e, p = parse(v)
            ok = isinstance(e, list) and isinstance(p, list)
        except Exception as ex:      # noqa: BLE001
            ok, e = False, ex
        check("win: Nicht-Text aus options.json %r -> kein Absturz" % (v,), ok, e)

    # B) Zuordnung zu den Geraeten: Name (NFC/NFD, Gross/Klein, Leerzeichen) oder Serial
    nfd = _ud.normalize("NFD", "Gäste-Bad")
    b, devs = _house_bridge(["Flur", nfd, "Kinderzimmer", "Wohnzimmer", "Schlafzimmer Eltern"],
                            awd="gäste-bad=15, SCHLAFZIMMER  ELTERN=60, sn-2=45, Keller=20")
    with _LogCapture() as lc:
        b._resolve_average_windows()
    check("win: Bad (NFD im Geraetenamen) 15 min", b._avg_window.get("SN-1") == 900.0, b._avg_window)
    check("win: Schlafzimmer (Gross, doppeltes Leerzeichen) 60 min", b._avg_window.get("SN-4") == 3600.0)
    check("win: Kinderzimmer per Seriennummer 45 min", b._avg_window.get("SN-2") == 2700.0)
    check("win: nicht genannte Geraete ohne eigenes Fenster",
          "SN-0" not in b._avg_window and "SN-3" not in b._avg_window, b._avg_window)
    check("win: unbekannter Name gewarnt, mit Geraeteliste",
          lc.has("WARNING", "no unit named 'Keller'", "Flur (SN-0)"), lc.lines)
    check("win: Startlog nennt die Fenster je Geraet",
          lc.has("INFO", "Averaging per unit:", "15 min", "Schlafzimmer Eltern 60 min", "all others 30 min"),
          lc.lines)
    check("win: wirksames Fenster je Geraet",
          b._average_window_s("SN-1", 30) == 900.0 and b._average_window_s("SN-0", 30) == 1800.0)

    # C) Untergrenze 20 Abrufe gilt auch je Geraet, und der Log sagt es
    b, devs = _house_bridge(["Bad"], poll=30, awd="Bad=5")
    with _LogCapture() as lc:
        b._resolve_average_windows()
    check("win: 5 min bei 30 s Abruf -> wirksam 10 min",
          b._average_window_s("SN-0", 30) == 600.0, b._average_window_s("SN-0", 30))
    check("win: Log nennt die Untergrenze", lc.has("INFO", "Bad 10 min (set 5, at least 20 polls)"), lc.lines)

    # D) Doppelt genannt: der spaetere Eintrag gilt, mit Warnung; gleicher Wert ohne Warnung
    b, devs = _house_bridge(["Bad"], awd="Bad=15, SN-0=20")
    with _LogCapture() as lc:
        b._resolve_average_windows()
    check("win: doppelt -> spaeterer Eintrag, Warnung",
          b._avg_window["SN-0"] == 1200.0 and lc.has("WARNING", "listed more than once"), lc.lines)
    b, devs = _house_bridge(["Bad"], awd="Bad=15, SN-0=15")
    with _LogCapture() as lc:
        b._resolve_average_windows()
    check("win: doppelt mit gleichem Wert -> keine Warnung",
          b._avg_window["SN-0"] == 900.0 and not lc.has("WARNING", "listed more than once"), lc.lines)

    # E) Zwei Geraete mit gleichem Namen (zwei Haeuser): beide bekommen das Fenster
    b, devs = _house_bridge(["Bad", "Bad"], awd="Bad=15")
    b._resolve_average_windows()
    check("win: gleicher Name -> beide", b._avg_window == {"SN-0": 900.0, "SN-1": 900.0}, b._avg_window)

    # F) Leer -> keine Zuordnung, kein Log
    b, devs = _house_bridge(["Bad"], awd="")
    with _LogCapture() as lc:
        b._resolve_average_windows()
    check("win: leer -> nichts, kein Log", b._avg_window == {} and not lc.lines, lc.lines)
    # Nichts passt -> nur die Warnung, keine leere Infozeile
    b, devs = _house_bridge(["Bad"], awd="Keller=15")
    with _LogCapture() as lc:
        b._resolve_average_windows()
    check("win: nichts passt -> keine leere Infozeile",
          b._avg_window == {} and not lc.has("INFO", "Averaging per unit") and lc.has("WARNING", "Keller"),
          lc.lines)
    # Alle genannt -> ohne "all others"
    b, devs = _house_bridge(["Bad", "WC"], awd="Bad=15, WC=20")
    with _LogCapture() as lc:
        b._resolve_average_windows()
    check("win: alle genannt -> kein 'all others'",
          lc.has("INFO", "Averaging per unit: Bad 15 min, WC 20 min.")
          and not lc.has("INFO", "all others"), lc.lines)
    # Seriennummer geht vor einem gleichlautenden Geraetenamen
    b, devs = _house_bridge(["SN-1", "Flur"], awd="sn-1=15")
    b._resolve_average_windows()
    check("win: Seriennummer vor gleichlautendem Namen", b._avg_window == {"SN-1": 900.0}, b._avg_window)
    # Kundenfall Okt. 2026: Geraet heisst in der App "Gäste Bad", die Option sagt
    # "Gäste-Bad" (das Doku-Beispiel) -> seit 1.4.35 trotzdem zugeordnet; und
    # umgekehrt. Ein zusammengeschriebener Name bleibt ein anderer Name.
    b, devs = _house_bridge(["Flur", "Gäste Bad", "Gästebad"], awd="Gäste-Bad=15")
    with _LogCapture() as lc:
        b._resolve_average_windows()
    check("win: 'Gäste-Bad' trifft 'Gäste Bad', nicht 'Gästebad'",
          b._avg_window == {"SN-1": 900.0} and not lc.has("WARNING", "no unit named"), (b._avg_window, lc.lines))
    b, devs = _house_bridge(["Gäste-Bad"], awd="gäste bad=15, SN_0=20")
    with _LogCapture() as lc:
        b._resolve_average_windows()
    check("win: 'Gäste Bad' trifft 'Gäste-Bad'; Seriennummer mit '_' statt '-' ebenso (spaeterer Eintrag)",
          b._avg_window == {"SN-0": 1200.0} and lc.has("WARNING", "listed more than once"), (b._avg_window, lc.lines))

    # F2) Nicht genannte Geraete rechnen exakt wie ohne Option (Zufallsfolge)
    import random as _rnd
    rng = _rnd.Random(11)
    seq = [(rng.choice([0, 5, 10, 30, 30, 30, 31, 60, 400]),
            rng.choice([None, 40, 55, 56, 90, 12.5, float("nan"), True, "x"])) for _ in range(3000)]
    b0, _ = _house_bridge(["Bad", "Flur"], awd="")
    b1, _ = _house_bridge(["Bad", "Flur"], awd="Bad=7")
    b1._resolve_average_windows()
    t, same = 50000.0, True
    for dt, v in seq:
        t += dt
        a0 = b0._average("SN-1", "humidity", v, now=t)
        b1._average("SN-0", "humidity", v, now=t)
        a1 = b1._average("SN-1", "humidity", v, now=t)
        same = same and a0 == a1
    check("win: nicht genanntes Geraet unveraendert (3000 Zufallswerte)",
          same and b0._avg_buf["SN-1"] == b1._avg_buf["SN-1"])

    # G) Wirkung: Sprung 50 -> 90 %; 15/30/60 min erreichen den neuen Wert nach ihrem Fenster
    b, devs = _house_bridge(["Bad", "Flur", "Schlafzimmer"], awd="Bad=15, Schlafzimmer=60")
    b._resolve_average_windows()
    res = {s: [] for s in devs}
    with _Clock() as c:
        for i in range(120 + 80):              # 60 min 50 %, dann 40 min 90 %
            c.t += 30
            h = 50 if i < 120 else 90
            for s, d in devs.items():
                d._status["humidity"] = h
                res[s].append(b._state_payload(s, d, dict(d._status))["humidity_avg"])
    at = lambda s, m: res[s][120 + int(m * 2) - 1]  # m Minuten nach dem Sprung
    check("win: vor dem Sprung alle 50", all(res[s][119] == 50.0 for s in devs), [res[s][119] for s in devs])
    check("win: 15 min nach dem Sprung: Bad 90, Flur ~70, Schlafzimmer ~60",
          at("SN-0", 15) == 90.0 and 69.0 <= at("SN-1", 15) <= 71.0 and 59.0 <= at("SN-2", 15) <= 61.0,
          (at("SN-0", 15), at("SN-1", 15), at("SN-2", 15)))
    check("win: 30 min nach dem Sprung: Flur 90, Schlafzimmer ~70",
          at("SN-1", 30) == 90.0 and 69.0 <= at("SN-2", 30) <= 71.0, (at("SN-1", 30), at("SN-2", 30)))
    check("win: 7,5 min nach dem Sprung: Bad ~70", 69.0 <= at("SN-0", 7.5) <= 71.0, at("SN-0", 7.5))
    check("win: Puffer je Geraet auf sein Fenster begrenzt",
          len(b._avg_buf["SN-0"]["humidity"]) <= 31 + 1 and len(b._avg_buf["SN-2"]["humidity"]) <= 121 + 1,
          (len(b._avg_buf["SN-0"]["humidity"]), len(b._avg_buf["SN-2"]["humidity"])))

    # H) Kundenrhythmus 90/56 im Wechsel: Bad (15 min) und Schlafzimmer (60 min) mitteln beide auf 73
    b, devs = _house_bridge(["Bad", "Schlafzimmer"], awd="Bad=15, Schlafzimmer=60")
    b._resolve_average_windows()
    out = {s: [] for s in devs}
    with _Clock() as c:
        for i in range(200):
            c.t += 30
            for s, d in devs.items():
                d._status["humidity"] = 90 if i % 2 == 0 else 56
                out[s].append(b._state_payload(s, d, dict(d._status))["humidity_avg"])
    check("win: Wechseltakt -> Bad 73 +-1 ab 15 min", all(72.0 <= v <= 74.0 for v in out["SN-0"][30:]),
          (min(out["SN-0"][30:]), max(out["SN-0"][30:])))
    check("win: Wechseltakt -> Schlafzimmer 73 +-1 ab 60 min", all(72.0 <= v <= 74.0 for v in out["SN-1"][120:]),
          (min(out["SN-1"][120:]), max(out["SN-1"][120:])))

    # I) Konfiguration: options.json, YAML (bridge:), Umgebungsvariable, null
    import tempfile
    tmp = tempfile.mkdtemp()
    old_env = os.environ.pop("AVERAGE_WINDOW_DEVICES", None)
    try:
        pj = os.path.join(tmp, "options.json")
        with open(pj, "w") as f:
            json.dump({"ambientika_username": "u", "average_window_devices": "Gäste-Bad=15"}, f)
        check("cfg: options.json", C.from_ha_options(pj).average_window_devices == "Gäste-Bad=15")
        with open(pj, "w") as f:
            json.dump({"ambientika_username": "u", "average_window_devices": None}, f)
        check("cfg: options.json null -> leer", C.from_ha_options(pj).average_window_devices == "")
        with open(pj, "w") as f:
            json.dump({"ambientika_username": "u"}, f)
        check("cfg: options.json ohne Schluessel -> leer", C.from_ha_options(pj).average_window_devices == "")
        yp = os.path.join(tmp, "config.yaml")
        with open(yp, "w") as f:
            f.write("bridge:\n  poll_interval: 30\n  average_window_devices: \"Bad=15, Flur=45\"\n")
        check("cfg: YAML bridge-Abschnitt", C.from_yaml(yp).average_window_devices == "Bad=15, Flur=45")
        with open(yp, "w") as f:
            f.write("bridge:\n  poll_interval: 30\n")
        check("cfg: YAML ohne Schluessel -> leer", C.from_yaml(yp).average_window_devices == "")
        os.environ["AVERAGE_WINDOW_DEVICES"] = "WC=20"
        check("cfg: Umgebungsvariable", C.from_env().average_window_devices == "WC=20")
        os.environ["AVERAGE_WINDOW_DEVICES"] = "null"
        check("cfg: Umgebungsvariable 'null' (bashio) -> leer", C.from_env().average_window_devices == "")
    finally:
        os.environ.pop("AVERAGE_WINDOW_DEVICES", None)
        if old_env is not None:
            os.environ["AVERAGE_WINDOW_DEVICES"] = old_env


class _FakeRoom:
    def __init__(self, devices):
        self.devices = devices


class _FakeHouse:
    def __init__(self, hid, rooms):
        self.id = hid
        self.rooms = rooms


class _FakeApi:
    def __init__(self, houses):
        self._houses = houses

    async def houses(self):
        return Success(self._houses)


async def test_average_per_device_discovery():
    """average_window_devices wird bei der Geraetesuche aufgeloest - ein Fehler darin
    haelt die Bridge nie an."""
    cfg = bridge.BridgeConfig()
    cfg.neuracell_enabled = False
    cfg.dewpoint_enabled = True
    cfg.dewpoint_source = "device"
    cfg.dewpoint_device_serial = "TPS-1"
    cfg.average_window_devices = "Bad=15, Taupunkt=20"
    b = bridge.AmbientikaBridge(cfg)
    bad = FakeDevice(serial="B-1", name="Bad", zone=0)
    tps = FakeDevice(serial="TPS-1", name="Taupunkt", zone=0)
    b.api = _FakeApi([_FakeHouse(1, [_FakeRoom([bad, tps])])])
    with _LogCapture() as lc:
        await b._discover_devices()
    check("disc-win: Bad 15 min nach der Geraetesuche", b._avg_window == {"B-1": 900.0}, b._avg_window)
    check("disc-win: TPS (kein Luefter) gilt als unbekannt", lc.has("WARNING", "no unit named 'Taupunkt'"), lc.lines)

    cfg2 = bridge.BridgeConfig()
    cfg2.neuracell_enabled = False
    cfg2.dewpoint_enabled = False
    cfg2.average_window_devices = "Bad=15"
    b2 = bridge.AmbientikaBridge(cfg2)
    b2.api = _FakeApi([_FakeHouse(1, [_FakeRoom([FakeDevice(serial="B-1", name="Bad", zone=0)])])])

    def boom():
        raise RuntimeError("kaputt")
    cfg2.average_window_entries = boom
    with _LogCapture() as lc:
        await b2._discover_devices()
    check("disc-win: Fehler -> Geraetesuche laeuft durch, globales Fenster",
          "B-1" in b2.devices and b2._avg_window == {} and lc.has("ERROR", "could not be applied"),
          (list(b2.devices), b2._avg_window, lc.lines))


async def test_ack_startup_run_path():
    """Kundenaufbau durch den echten run()-Pfad (Login, Geraetesuche, Broker, Startzustand,
    Abrufschleife): Quittung 12 Tage alt fuer das Wohnzimmer in filter_ack.json."""
    import time as _t
    AB = bridge.AmbientikaBridge
    old_env = {k: os.environ.get(k) for k in ("SLAVE_FILTER_SOFT_RESET", "FILTER_ACK_PATH", "FILTER_ACK_TTL_DAYS")}
    os.environ["SLAVE_FILTER_SOFT_RESET"] = "1"
    os.environ["FILTER_ACK_PATH"] = os.path.join(_tempfile.mkdtemp(), "filter_ack.json")
    os.environ["FILTER_ACK_TTL_DAYS"] = "90"
    pfx = "ambientika"

    def mk():
        cfg = bridge.BridgeConfig()
        cfg.neuracell_enabled = False
        cfg.dewpoint_enabled = False
        cfg.poll_interval = 1
        cfg.average_window_devices = "Gäste-Bad=15"     # Doku-Beispiel gegen App-Namen "Gäste Bad"
        b = bridge.AmbientikaBridge(cfg)
        flur = FakeDevice(serial="FLUR", name="Flur", zone=0, status=mkstatus(op=OM.Smart))
        flur.role = "Master"
        gbad = FakeDevice(serial="GBAD", name="Gäste Bad", zone=0, status=mkstatus(op=OM.Surveillance))
        wozi = FakeDevice(serial="WOZI", name="Wohnzimmer", zone=0, status=mkstatus(op=OM.Surveillance))
        wozi._status["filters_status"] = "Bad"
        devs = {d.serial_number: d for d in (flur, gbad, wozi)}

        async def _login():
            b.api = object()

        async def _discover():
            b.devices = devs
            b._device_house = {s: 1 for s in devs}
            b._resolve_average_windows()

        def _connect():
            b.client = FakeClient()
            b._publish_discovery()
        b._login = _login
        b._discover_devices = _discover
        b._mqtt_connect = _connect
        return b

    async def run_for(b, seconds):
        task = asyncio.create_task(b.run())
        await asyncio.sleep(seconds)
        b._stop_event.set()
        await task

    def resets(b, serial):
        return [p for tp, p in b.client.pub if tp == bridge.reset_state_topic(pfx, serial)]

    def states(b, serial):
        return [json.loads(p) for tp, p in b.client.pub if tp == bridge.state_topic(pfx, serial)]

    try:
        acked = _t.time() - 12 * 86400
        AB._filter_ack_save({"WOZI": {"acked_at": acked, "raw_when_acked": "Bad"}})
        b = mk()
        with _LogCatch() as lc:
            await run_for(b, 2.6)
        check("run: Wohnzimmer acknowledged, genau einmal; Rest idle",
              resets(b, "WOZI") == ["acknowledged"] and resets(b, "FLUR") == ["idle"] and resets(b, "GBAD") == ["idle"],
              {s: resets(b, s) for s in b.devices})
        first_reset = next(i for i, (tp, _) in enumerate(b.client.pub) if tp.endswith("/reset_state"))
        first_state = next(i for i, (tp, _) in enumerate(b.client.pub) if tp.endswith("/state") and "neuracell" not in tp)
        check("run: Startzustand retained und vor dem ersten Statuspaket",
              first_reset < first_state and all(r for tp, _, r in b.client.pubr if tp.endswith("/reset_state")),
              (first_reset, first_state))
        st = states(b, "WOZI")
        check("run: Pakete mit Good/Bad und since/until",
              len(st) >= 2 and all(s["filters_status"] == "Good" and s["filters_status_raw"] == "Bad"
                                   and s["filter_ack_since"] == bridge._iso_utc(acked)
                                   and s["filter_ack_until"] == bridge._iso_utc(acked + 90 * 86400) for s in st), st[-1:])
        check("run: Startlog nennt die Quittung", lc.has("INFO", "still in force"), lc.lines[:30])
        check("run: 'Gäste-Bad=15' trifft 'Gäste Bad'", lc.has("INFO", "Averaging per unit: Gäste Bad 15 min"), lc.lines[:30])
        # Neustart (Update): gleiche Datei, neue Bridge -> wieder acknowledged
        b2 = mk()
        await run_for(b2, 1.6)
        check("run: nach Neustart wieder acknowledged", resets(b2, "WOZI") == ["acknowledged"], resets(b2, "WOZI"))
        # Frist laeuft im Betrieb ab -> idle, Paket ohne Datum, Datei bereinigt
        os.environ["FILTER_ACK_TTL_DAYS"] = repr((_t.time() - acked + 1.5) / 86400.0)
        b3 = mk()
        with _LogCatch() as lc:
            await run_for(b3, 3.6)
        st = states(b3, "WOZI")
        check("run: Ablauf im Betrieb -> acknowledged, dann idle; zuletzt Bad ohne Datum; Logzeile",
              resets(b3, "WOZI") == ["acknowledged", "idle"] and st[0]["filters_status"] == "Good"
              and st[-1]["filters_status"] == "Bad" and st[-1]["filter_ack_until"] is None
              and lc.has("INFO", "ran out") and "WOZI" not in AB._filter_ack_load(),
              (resets(b3, "WOZI"), st[0]["filters_status"], st[-1]["filters_status"]))
        # beschaedigte Datei: run() startet trotzdem
        with open(os.environ["FILTER_ACK_PATH"], "w", encoding="utf-8") as f:
            f.write(json.dumps({"WOZI": "Bad", "FLUR": {"acked_at": _t.time() * 1000}}))
        b4 = mk()
        try:
            await run_for(b4, 1.6)
            check("run: beschaedigte filter_ack.json -> Start laeuft durch, idle",
                  resets(b4, "WOZI") == ["idle"] and resets(b4, "FLUR") == ["idle"] and len(states(b4, "WOZI")) >= 1,
                  {s: resets(b4, s) for s in b4.devices})
        except Exception as ex:      # noqa: BLE001
            check("run: beschaedigte filter_ack.json -> Start laeuft durch", False, repr(ex))
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        bridge._FILTER_ACK_GOOD_STREAK.clear()
        bridge._FILTER_ACK_ODD_RAW.clear()


async def test_neuracell_timed_expulsion():
    """1.4.37: Die zeitgesteuerte Abluft (TimedExpulsion) ist eine 20-Minuten-Aktion,
    kein Modus, zu dem ein Geraet zurueckkehrt - sie wird nie zur Baseline."""
    def mk(op, last):
        cfg = bridge.BridgeConfig()
        cfg.radon_threshold = 300
        cfg.radon_hysteresis = 50
        b = bridge.AmbientikaBridge(cfg)
        b.client = FakeClient()
        b.loop = asyncio.get_running_loop()
        dev = FakeDevice(serial="FLUR", name="Flur", zone=1,
                         status=mkstatus(op=op, last=last, fan=FS.High, hum=HL.Normal))
        dev.role = "Master"
        b.devices = {dev.serial_number: dev}
        return b, dev, b.neuracell

    # A) Taupunktsperre waehrend der Abluft: Baseline = Modus, zu dem das Geraet zurueckkehrt
    b, dev, nc = mk(OM.TimedExpulsion, OM.Auto)
    with _LogCapture() as lc:
        await nc.on_dewpoint_block("ON")
    check("timed: Sperre -> Geraet Off", dev._status["operating_mode"] == OM.Off, dev._status["operating_mode"])
    check("timed: Baseline ist Auto (last_operating_mode), nicht TimedExpulsion",
          nc._saved_modes.get("FLUR", {}).get("operating_mode") == OM.Auto, nc._saved_modes.get("FLUR"))
    check("timed: Luefterstufe/Feuchteschwelle wie gemeldet",
          nc._saved_modes["FLUR"]["fan_speed"] == FS.High and nc._saved_modes["FLUR"]["humidity_level"] == HL.Normal)
    check("timed: Log nennt den uebernommenen Modus", lc.has("INFO", "timed extraction", "FLUR", "Auto"), lc.lines[-3:])
    await nc.on_dewpoint_block("OFF")
    check("timed: Freigabe -> Auto wiederhergestellt, nicht TimedExpulsion",
          dev.mode_calls[-1]["operating_mode"] == OM.Auto, dev.mode_calls[-1:])
    check("timed: Baseline geloescht", "FLUR" not in nc._saved_modes)

    # B) Radonschutz: gleicher Weg
    b, dev, nc = mk(OM.TimedExpulsion, OM.Smart)
    await nc.on_radon_value("500")
    check("timed/radon: Geraet Intake", dev._status["operating_mode"] == bridge.RADON_PROTECTION_MODE)
    check("timed/radon: Baseline Smart", nc._saved_modes.get("FLUR", {}).get("operating_mode") == OM.Smart)
    await nc.on_radon_value("100")
    check("timed/radon: Smart wiederhergestellt", dev.mode_calls[-1]["operating_mode"] == OM.Smart, dev.mode_calls[-1:])

    # C) kein brauchbarer vorheriger Modus -> altes Verhalten, mit Warnung
    b, dev, nc = mk(OM.TimedExpulsion, OM.TimedExpulsion)
    with _LogCapture() as lc:
        await nc.on_dewpoint_block("ON")
    check("timed/unbrauchbar: Baseline bleibt TimedExpulsion",
          nc._saved_modes.get("FLUR", {}).get("operating_mode") == OM.TimedExpulsion)
    check("timed/unbrauchbar: Warnung im Log", lc.has("WARNING", "no usable", "FLUR"), lc.lines[-3:])
    with _LogCapture() as lc:
        await nc.on_dewpoint_block("OFF")
    check("timed/unbrauchbar: Restore warnt (TimedExpulsion als Baseline)",
          lc.has("WARNING", "saved baseline", "FLUR", "TimedExpulsion"), lc.lines[-3:])

    # D) normaler Modus unveraendert: Baseline = gemeldeter Modus (kein Rueckgriff auf last)
    b, dev, nc = mk(OM.ManualHeatRecovery, OM.Auto)
    await nc.on_dewpoint_block("ON")
    check("timed/normal: Baseline bleibt der gemeldete Modus",
          nc._saved_modes.get("FLUR", {}).get("operating_mode") == OM.ManualHeatRecovery)
    await nc.on_dewpoint_block("OFF")

    # E) manuelles TimedExpulsion waehrend des Schutzes: verworfen, Baseline unveraendert, nichts gesendet
    b, dev, nc = mk(OM.Auto, OM.Auto)
    await nc.on_dewpoint_block("ON")
    sent = len(dev.mode_calls)
    with _LogCapture() as lc:
        await b._handle_command("FLUR", "operating_mode", "TimedExpulsion")
        await asyncio.sleep(0.05)
    check("timed/manuell: Baseline bleibt Auto", nc._saved_modes.get("FLUR", {}).get("operating_mode") == OM.Auto,
          nc._saved_modes.get("FLUR"))
    check("timed/manuell: nichts ans Geraet gesendet", len(dev.mode_calls) == sent, dev.mode_calls[sent:])
    check("timed/manuell: Warnung 'not carried over'", lc.has("WARNING", "TimedExpulsion", "not carried over", "FLUR"), lc.lines[-3:])
    # ... ein normaler Modus wird wie bisher vorgemerkt
    await b._handle_command("FLUR", "operating_mode", "Night")
    await asyncio.sleep(0.05)
    check("timed/manuell: Night wird als neue Baseline vorgemerkt",
          nc._saved_modes.get("FLUR", {}).get("operating_mode") == OM.Night, nc._saved_modes.get("FLUR"))
    await nc.on_dewpoint_block("OFF")
    check("timed/manuell: Freigabe -> Night", dev.mode_calls[-1]["operating_mode"] == OM.Night, dev.mode_calls[-1:])

    # F) Freigabe, aber Restore steht noch aus (Geraet war nicht erreichbar): TimedExpulsion
    #    darf nicht aus dem Schutzmodus heraus starten - erst Restore, dann Abluft.
    from returns.result import Failure as _Failure
    import types as _types
    b, dev, nc = mk(OM.Auto, OM.Auto)
    await nc.on_dewpoint_block("ON")
    check("timed/pending: Sperre -> Off, Baseline Auto",
          dev._status["operating_mode"] == OM.Off and nc._saved_modes["FLUR"]["operating_mode"] == OM.Auto)
    real_change_mode = dev.change_mode
    async def _unreachable(mode):
        return _Failure({"status_code": 500})
    dev.change_mode = _unreachable
    await nc.on_dewpoint_block("OFF")
    check("timed/pending: Restore schlaegt fehl -> Baseline bleibt, Geraet weiter Off",
          "FLUR" in nc._saved_modes and dev._status["operating_mode"] == OM.Off, nc._saved_modes.get("FLUR"))
    sent = len(dev.mode_calls)
    with _LogCapture() as lc:
        await b._handle_command("FLUR", "operating_mode", "TimedExpulsion")
    check("timed/pending: Geraet weiter unerreichbar -> Abluft nicht gestartet, Warnung",
          lc.has("WARNING", "TimedExpulsion", "FLUR", "restore pending", "not started"), lc.lines[-3:])
    check("timed/pending: Baseline bleibt Auto", nc._saved_modes.get("FLUR", {}).get("operating_mode") == OM.Auto)
    dev.change_mode = real_change_mode
    with _LogCapture() as lc:
        await b._handle_command("FLUR", "operating_mode", "TimedExpulsion")
    sent_modes = [c["operating_mode"] for c in dev.mode_calls[sent:]]
    check("timed/pending: erreichbar -> erst Auto (Restore), dann TimedExpulsion",
          sent_modes == [OM.Auto, OM.TimedExpulsion], sent_modes)
    check("timed/pending: Abluft startet aus Auto heraus (Luefter/Feuchte aus der Baseline)",
          dev.mode_calls[-1]["fan_speed"] == FS.High and dev.mode_calls[-1]["humidity_level"] == HL.Normal, dev.mode_calls[-1])
    check("timed/pending: Baseline danach geloescht", "FLUR" not in nc._saved_modes, nc._saved_modes)
    check("timed/pending: Log 'restored first'", lc.has("INFO", "FLUR", "restored first"), lc.lines[-4:])

    # G) kombinierter Befehl waehrend des Schutzes: Modus verworfen, Luefterstufe vorgemerkt, nichts gesendet
    b, dev, nc = mk(OM.Auto, OM.Auto)
    await nc.on_dewpoint_block("ON")
    sent = len(dev.mode_calls)
    with _LogCapture() as lc:
        await b._handle_command_set("FLUR", {"mode": "TimedExpulsion", "fanSpeed": "Low"})
    check("timed/kombiniert: Luefterstufe Low vorgemerkt, Modus bleibt Auto",
          nc._saved_modes["FLUR"]["fan_speed"] == FS.Low and nc._saved_modes["FLUR"]["operating_mode"] == OM.Auto,
          nc._saved_modes["FLUR"])
    check("timed/kombiniert: nichts gesendet", len(dev.mode_calls) == sent, dev.mode_calls[sent:])
    check("timed/kombiniert: Warnungen fuer beides (verworfen / vorgemerkt)",
          lc.has("WARNING", "not carried over", "FLUR") and lc.has("WARNING", "deferring manual fan_speed change on FLUR"),
          lc.lines[-4:])
    await nc.on_dewpoint_block("OFF")
    check("timed/kombiniert: Freigabe -> Auto mit Low", dev.mode_calls[-1]["operating_mode"] == OM.Auto
          and dev.mode_calls[-1]["fan_speed"] == FS.Low, dev.mode_calls[-1:])

    # H) TimedExpulsion + Lichtsensor waehrend des Schutzes: Licht geht durch, Modus bleibt Off
    b, dev, nc = mk(OM.Auto, OM.Auto)
    await nc.on_dewpoint_block("ON")
    sent = len(dev.mode_calls)
    await b._handle_command_set("FLUR", {"mode": "TimedExpulsion", "lightSensorLevel": "Low"})
    check("timed/licht: ein Befehl, Lichtsensor Low, Modus Off (Schutz bleibt)",
          len(dev.mode_calls) == sent + 1 and dev.mode_calls[-1]["light_sensor_level"] == LS.Low
          and dev.mode_calls[-1]["operating_mode"] == OM.Off, dev.mode_calls[sent:])
    check("timed/licht: Baseline unveraendert Auto", nc._saved_modes["FLUR"]["operating_mode"] == OM.Auto)
    await nc.on_dewpoint_block("OFF")

    # I) Zustandsdatei einer aelteren Version mit TimedExpulsion-Baseline: geladen, mit Warnung wiederhergestellt
    b, dev, nc = mk(OM.Off, OM.Auto)
    with open(bridge.NEURACELL_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"saved_modes": {"FLUR": {"operating_mode": "TimedExpulsion", "fan_speed": "High",
                                              "humidity_level": "Normal"}},
                   "applied": {"FLUR": {"operating_mode": "Off", "fan_speed": "Medium"}},
                   "ts": time.time()}, f)
    old_grace = bridge.STARTUP_GRACE_S
    bridge.STARTUP_GRACE_S = 0.0
    try:
        with _LogCapture() as lc:
            nc.load_persisted()
            check("timed/legacy: Baseline aus Datei geladen",
                  nc._saved_modes.get("FLUR", {}).get("operating_mode") == OM.TimedExpulsion, nc._saved_modes.get("FLUR"))
            await nc.reconcile()
            await nc.reconcile()
        check("timed/legacy: wiederhergestellt (altes Verhalten)", dev._status["operating_mode"] == OM.TimedExpulsion,
              dev._status["operating_mode"])
        check("timed/legacy: genau eine Warnung dazu",
              sum(1 for lv, m in lc.lines if lv == "WARNING" and "saved baseline" in m and "timed extraction" in m
                  and "FLUR" in m) == 1,
              [m for lv, m in lc.lines if "saved baseline" in m])
    finally:
        bridge.STARTUP_GRACE_S = old_grace
        try:
            os.remove(bridge.NEURACELL_STATE_FILE)
        except OSError:
            pass


async def _async_suite():
    await test_neuracell()
    await test_neuracell_scoped()
    await test_neuracell_robust()
    await test_neuracell_timed_expulsion()
    await test_radon_meter()
    await test_dewpoint_watch()
    await test_meter_watch()
    await test_controller_direct()
    await test_payload()
    await test_command()
    await test_command_coalescing()
    await test_readonly_values()
    await test_mode_verify()
    await test_filter_ack_poll_path()
    await test_shown_mode()
    await test_plausibility()
    await test_zone_master_houses_and_order()
    await test_ack_publish_now()
    await test_ack_startup_run_path()
    await test_live_roles()
    await test_duplicate_packets()
    await test_log_rate_limit()
    await test_mode_confirmed_slave_note()
    await test_average()
    await test_average_per_device_discovery()


def main():
    FAILS.clear()
    test_discovery()
    test_dewpoint()
    test_config()
    test_filter_ack_robust()
    test_filter_ack_status_sensor()
    test_discovery_raw_mode()
    test_average_discovery_and_env()
    test_average_per_device()
    asyncio.run(_async_suite())
    print("\nRESULT:", "ALL PASS" if not FAILS else f"{len(FAILS)} FAIL -> {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
