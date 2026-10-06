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


async def _async_suite():
    await test_neuracell()
    await test_neuracell_scoped()
    await test_neuracell_robust()
    await test_radon_meter()
    await test_dewpoint_watch()
    await test_meter_watch()
    await test_controller_direct()
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
