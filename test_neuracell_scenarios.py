#!/usr/bin/env python3
"""Szenario-Simulation der NeuraCell-X-Logik im Zeitraffer.

Laedt die echte bridge.py und laesst ihre echte Poll-Schleife (_poll_loop)
gegen gemockte Geraete laufen. Eine Fake-Uhr schiebt die Zeit pro Durchlauf um
ein Abfrageintervall (30 s) weiter, MQTT-Nachrichten werden zu festen
Simulationszeiten eingespielt. So werden Zeitlimits (30 min), Neustarts und das
Zusammenspiel von Taupunktsperre, Radonschutz und Notlueftung ueber Stunden
geprueft, ohne dass echte Zeit vergeht.

Anlage wie in einer realen Installation: Schlafetage mit 5 SMART in drei
Zonen (zwei Master-Slave-Paare, ein einzelner Master), Keller mit zwei OFFICE
(Master + Slave) an Taupunktsteuerung und Radon-Messgeraet. Taupunktsperre nur
fuer die beiden OFFICE.

Standalone lauffaehig (CI):  python test_neuracell_scenarios.py   (Exit != 0 bei Fehler)
"""
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import time as real_time

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("COMMAND_COALESCE_MS", "0")
_spec = importlib.util.spec_from_file_location("bridge", os.path.join(HERE, "bridge.py"))
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)
STATE_DIR = tempfile.mkdtemp()
bridge.NEURACELL_STATE_FILE = os.path.join(STATE_DIR, "nc_state.json")
bridge.PENDING_RESET_FILE = os.path.join(STATE_DIR, "resets.json")
from returns.result import Success, Failure  # noqa: E402

OM, FS, HL, LS = bridge.OperatingMode, bridge.FanSpeed, bridge.HumidityLevel, bridge.LightSensorLevel
FAILS = []


def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else "  <<< " + str(extra)))
    if not cond:
        FAILS.append(name)


# --------------------------------------------------------------------------- Fake-Uhr
class FakeTime:
    """Ersetzt das time-Modul innerhalb der Bridge: monotonic() und time()
    laufen mit einem verschiebbaren Versatz, alles andere kommt vom Original."""

    def __init__(self, real):
        self._real = real
        self.offset = 0.0

    def monotonic(self):
        return self._real.monotonic() + self.offset

    def time(self):
        return self._real.time() + self.offset

    def __getattr__(self, name):
        return getattr(self._real, name)


CLOCK = FakeTime(real_time)
bridge.time = CLOCK
POLL_S = 30.0


# --------------------------------------------------------------------------- Fakes
class FakeClient:
    def __init__(self):
        self.pub = []

    def publish(self, t, p, qos=0, retain=False):
        self.pub.append((t, p))

    def subscribe(self, *a, **k):
        pass

    def is_connected(self):
        return True


def mkstatus(op, fan=FS.Medium, hum=HL.Normal, role="Master", dtype="SMART", serial="X"):
    return {
        "operating_mode": op, "fan_speed": fan, "humidity_level": hum,
        "light_sensor_level": LS.Off, "temperature": 21, "humidity": 55,
        "air_quality": "Good", "humidity_alarm": False, "filters_status": "Green",
        "night_alarm": False, "device_role": role, "last_operating_mode": op,
        "packet_type": "P", "device_type": dtype, "device_serial_number": serial,
    }


class Unit:
    """Ein Geraet in der Cloud: beantwortet status() und change_mode().

    Ein Slave behaelt sein eigenes Modusfeld (die Cloud nimmt den Befehl an,
    das Feld folgt dem Master) - wie beim echten Geraet. `offline_polls` > 0
    laesst status() so viele Abrufe lang fehlschlagen (404 Status packet not
    found, wie im Kundenprotokoll)."""

    def __init__(self, serial, name, zone, role, op, fan=FS.Medium, dtype="SMART"):
        self.serial_number, self.name, self.zone_index = serial, name, zone
        self._role = role
        self._status = mkstatus(op, fan, role=role, dtype=dtype, serial=serial)
        self.mode_calls = []
        self.offline_polls = 0
        self.reset_calls = 0

    @property
    def role(self):
        return self._role

    @role.setter
    def role(self, v):
        self._role = v
        self._status["device_role"] = v

    @property
    def is_slave(self):
        return self._role.lower().startswith("slave")

    async def status(self):
        if self.offline_polls > 0:
            self.offline_polls -= 1
            return Failure({"status_code": 404, "data": "Status packet not found!"})
        return Success(dict(self._status))

    async def change_mode(self, mode):
        self.mode_calls.append((round(CLOCK.offset / 60, 1), mode["operating_mode"].name, mode["fan_speed"].name))
        if not self.is_slave:
            self._status["operating_mode"] = mode["operating_mode"]
            self._status["fan_speed"] = mode["fan_speed"]
            self._status["humidity_level"] = mode["humidity_level"]
        return Success(None)

    async def reset_filter(self):
        self.reset_calls += 1
        return Success(None)

    def mode(self):
        return self._status["operating_mode"].name, self._status["fan_speed"].name


def house(cellar_op=OM.Smart, cellar_fan=FS.Medium):
    """7 Geraete wie beim Kunden. Rueckgabe: dict serial -> Unit."""
    units = [
        Unit("441BF633FDC8", "BueroM", 1, "Master", OM.Smart),
        Unit("A0F262D56A3C", "BueroL", 1, "SlaveOppositeMaster", OM.Smart),
        Unit("441BF633FDD4", "Eltern", 2, "Master", OM.Smart),
        Unit("441BF633AE58", "KindSchlafen", 3, "Master", OM.Smart),
        Unit("441BF633B678", "KindSpielen", 3, "SlaveOppositeMaster", OM.Smart),
        Unit("FCB467C6EABC", "KellerLinks", 4, "Master", cellar_op, cellar_fan, dtype="OFFICE"),
        Unit("FCB467C4B1E4", "KellerRechts", 4, "SlaveOppositeMaster", cellar_op, cellar_fan, dtype="OFFICE"),
    ]
    return {u.serial_number: u for u in units}


class Msg:
    def __init__(self, t, p, retain=False):
        self.topic, self.payload, self.retain = t, p.encode(), retain


TPS_ST, TPS_AV, TPS_SE = ("dew-point/TP_D48C4956EC10/state", "dew-point/TP_D48C4956EC10/availability/state",
                          "dew-point/TP_D48C4956EC10/sensors")
MET_ST, MET_AV = "radon/Radon_D48C4958ECCC/state", "radon/Radon_D48C4958ECCC/availability/state"
METERS = "ambientika/neuracell/meters"


class Sim:
    """Eine laufende Bridge mit Fake-Uhr, Poll-Schleife und Ereignisplan."""

    def __init__(self, units, label, **cfg_kw):
        cfg = bridge.BridgeConfig()
        cfg.dewpoint_block_devices = "fcb467c4b1e4, fcb467c6eabc"
        cfg.radon_threshold = 100
        cfg.radon_hysteresis = 50
        cfg.radon_protection_fan = "Medium"
        cfg.poll_interval = 0.01            # echte Wartezeit; die Fake-Uhr springt pro Durchlauf 30 s
        cfg.enable_discovery = False
        for k, v in cfg_kw.items():
            setattr(cfg, k, v)
        self.cfg = cfg
        self.b = bridge.AmbientikaBridge(cfg)
        self.b.client = FakeClient()
        self.b.devices = units
        self.units = units
        self.label = label
        self.events = []        # (minute, topic, payload, retain)
        self.marks = {}         # minute -> callable(sim) fuer Pruefungen/Eingriffe
        self.log = []
        self._pending = []
        self.b._dispatch = lambda coro: self._pending.append(coro)
        self.start_min = 0.0

    # --- Ereignisse ---
    def at(self, minute, topic, payload, retain=False):
        """Eine Nachricht zur Simulationsminute. `retain` nur fuer Nachrichten
        aus dem Speicher des Brokers (nach einem Neustart/Reconnect): eine
        live zugestellte Nachricht traegt das Flag nie (MQTT 3.1.1, 3.3.1.3)."""
        self.events.append((minute, topic, payload, retain))

    def mark(self, minute, fn):
        self.marks.setdefault(minute, []).append(fn)

    async def _deliver(self, topic, payload, retain):
        self.b._on_mqtt_message(None, None, Msg(topic, payload, retain))
        while self._pending:
            await self._pending.pop(0)

    def minute(self):
        return (CLOCK.offset - self.start_offset) / 60.0

    def nc_state(self):
        st = [json.loads(p) for t, p in self.b.client.pub if t.endswith("neuracell/state")]
        return st[-1] if st else {}

    async def run(self, minutes, retained=(), resume=False):
        """Bridge starten (wie nach einem Neustart: zuerst die gespeicherten
        Nachrichten des Brokers), dann `minutes` Simulationsminuten laufen."""
        import logging
        h = logging.Handler()
        h.emit = lambda r: self.log.append((round(self.minute(), 1), r.levelname, r.getMessage()))
        bridge.log.addHandler(h)
        lvl = bridge.log.level
        bridge.log.setLevel(logging.INFO)
        self.start_offset = CLOCK.offset
        try:
            self.b.loop = asyncio.get_running_loop()
            if resume:
                self.b.neuracell.load_persisted()
            self.b.neuracell.on_broker_connected()
            for topic, payload in retained:
                await self._deliver(topic, payload, True)
            nc = self.b.neuracell
            orig_enforce = nc.enforce
            sim = self
            events = sorted(self.events)

            async def enforce_with_clock():
                CLOCK.offset += POLL_S
                now = sim.minute()
                while events and events[0][0] <= now:
                    _m, topic, payload, retain = events.pop(0)
                    await sim._deliver(topic, payload, retain)
                for m in sorted(k for k in sim.marks if k <= now):
                    for fn in sim.marks.pop(m):
                        fn(sim)
                await orig_enforce()
                if now >= minutes:
                    sim.b._stop_event.set()
            nc.enforce = enforce_with_clock
            self.b._stop_event = asyncio.Event()
            await self.b._poll_loop()
            # offene Marken (nach dem letzten Durchlauf)
            for m in sorted(self.marks):
                for fn in self.marks.pop(m):
                    fn(self)
        finally:
            bridge.log.removeHandler(h)
            bridge.log.setLevel(lvl)
            self.b.neuracell.persist()

    def has_log(self, level, *parts, after=None):
        return any(lv == level and all(p in m for p in parts) and (after is None or t >= after)
                   for t, lv, m in self.log)

    def calls(self, serial, since=None):
        return [c for c in self.units[serial].mode_calls if since is None or c[0] >= since]

    def dump(self, filt=("NeuraCell", "set to", "unreachable", "restored", "Taupunkt", "radon")):
        for t, lv, m in self.log:
            if any(f in m for f in filt):
                print("    %6.1f %-7s %s" % (t, lv, m[:150]))


KL, KR = "FCB467C6EABC", "FCB467C4B1E4"
SMARTS = ("441BF633FDC8", "A0F262D56A3C", "441BF633FDD4", "441BF633AE58", "441BF633B678")


# --------------------------------------------------------------------------- Szenarien
async def scenario_cellar_outage():
    """02.11.-Ablauf: Neustart, Messgeraet kommt, Sperre, WLAN-Aussetzer im
    Keller (TPS und Messgeraet weg), Notlueftung, Rueckkehr der Geraete."""
    print("\n[A] Kellerausfall waehrend der Sperre -> Notlueftung -> Rueckkehr")
    units = house()
    sim = Sim(units, "A")
    # Stand nach dem Neustart: gespeicherte Nachrichten des Brokers
    retained = [
        (METERS, json.dumps({"radon_meters": {MET_ST: real_time.time() - 3600}})),
        (MET_AV, "offline"),                                   # Last Will des Messgeraets
        (TPS_AV, "online"),                                    # altes "online" der TPS
        (TPS_ST, json.dumps({"ventilating": True, "reason": "ok"})),
    ]
    sim.at(28, MET_AV, "online")
    sim.at(28, MET_ST, json.dumps({"mittelwert": 0}))        # gerade gestartet
    for m in (38, 48, 58, 68, 78, 88, 98):                   # Messgeraet meldet alle 10 min
        sim.at(m, MET_ST, json.dumps({"mittelwert": 60 + m % 7}))
    for m in range(38, 102, 5):                               # TPS sendet Sensorwerte, bis sie ausfaellt
        sim.at(m, TPS_SE, json.dumps({"indoor": {"temp": 19.5, "humidity": 64}}))
    sim.at(70, TPS_ST, json.dumps({"ventilating": False, "reason": "aussen zu feucht"}))
    sim.mark(99, lambda s: setattr(s.units[KR], "offline_polls", 2))
    sim.at(102, TPS_AV, "offline")                # Last Will TPS
    sim.at(104, MET_AV, "offline")                # Last Will Messgeraet

    def check_blocked(s):
        check("A: Sperre -> beide OFFICE aus, Slave genau ein Befehl",
              s.units[KL].mode() == ("Off", "Medium") and len(s.calls(KR)) == 1 and s.calls(KR)[0][1] == "Off",
              (s.units[KL].mode(), s.calls(KR)))
        check("A: SMART-Geraete unberuehrt", all(not s.calls(x) for x in SMARTS))
    sim.mark(80, check_blocked)
    sim.mark(100, lambda s: check("A: 29 min nach Sperre weiterhin ein Slave-Befehl", len(s.calls(KR)) == 1, s.calls(KR)))

    def check_blind(s):
        st = s.nc_state()
        check("A: TPS und Messgeraet getrennt, Sperre bleibt (keep)",
              st["dewpoint_controller_connected"] is False and st["radon_meter_connected"] is False
              and st["dewpoint_block"] is True and st["dewpoint_fallback"] is False, st)
        check("A: Notlueftung angekuendigt", s.has_log("WARNING", "fallback ventilation", "in 30 min"))
        check("A: Geraete noch aus", s.units[KL].mode()[0] == "Off")
    sim.mark(120, check_blind)

    def check_fallback(s):
        st = s.nc_state()
        check("A: Notlueftung nach 30 min aktiv (ca. 20:09)",
              st["dewpoint_fallback"] is True and s.units[KL].mode() == ("ManualHeatRecovery", "Low"), (st, s.units[KL].mode()))
        kr = s.calls(KR)
        check("A: Slave bekommt Notlueftung genau einmal", len(kr) == 2 and kr[-1][1:] == ("ManualHeatRecovery", "Low"), kr)
        check("A: Notlueftung zwischen Minute 134 und 136", kr and 133.5 <= kr[-1][0] <= 136, kr)
        check("A: SMART-Geraete weiter unberuehrt", all(not s.calls(x) for x in SMARTS))
    sim.mark(140, check_fallback)
    sim.mark(180, lambda s: check("A: 40 min Notlueftung ohne Wiederholungen",
                                  len(s.calls(KR)) == 2 and len(s.calls(KL)) == 2, (s.calls(KL), s.calls(KR))))
    # TPS kommt zurueck (MQTT-Daten neu eingetragen), sperrt weiter
    sim.at(185, TPS_AV, "online")
    sim.at(185, TPS_ST, json.dumps({"ventilating": False, "reason": "aussen zu feucht"}))

    def check_tps_back(s):
        st = s.nc_state()
        check("A: TPS zurueck -> Notlueftung aus, Sperre (Off) gilt wieder",
              st["dewpoint_fallback"] is False and st["dewpoint_controller_connected"] is True
              and s.units[KL].mode()[0] == "Off" and s.calls(KR)[-1][1] == "Off", (st, s.calls(KR)))
        check("A: Ende im Log", s.has_log("WARNING", "fallback ventilation ended", "Taupunktsteuerung connected again"))
    sim.mark(190, check_tps_back)
    sim.at(215, TPS_ST, json.dumps({"ventilating": True, "reason": "ok"}))

    def check_released(s):
        check("A: Freigabe -> OFFICE zurueck auf Smart/Medium, Slave einmal",
              s.units[KL].mode() == ("Smart", "Medium") and s.calls(KR)[-1][1:] == ("Smart", "Medium")
              and not s.b.neuracell._saved_modes, (s.units[KL].mode(), s.calls(KR)))
        check("A: Befehle insgesamt: Master 4, Slave 4, SMART 0",
              len(s.calls(KL)) == 4 and len(s.calls(KR)) == 4 and all(not s.calls(x) for x in SMARTS),
              (s.calls(KL), s.calls(KR)))
        check("A: 404-Ausfall von KellerRechts protokolliert und wieder da",
              s.has_log("INFO", "Poll 1/3 failed for FCB467C4B1E4") and s.has_log("INFO", "Device FCB467C4B1E4 is answering again"))
        check("A: keine Fehler/Tracebacks", not any(lv == "ERROR" for _t, lv, _m in s.log), [m for t, lv, m in s.log if lv == "ERROR"])
    sim.mark(225, check_released)
    await sim.run(230, retained=retained)
    sim.dump()
    return sim


async def scenario_restart_during_fallback():
    """Neustart des Add-ons (HA-Update) mitten in der Notlueftung: sie wird
    mit den gespeicherten Baselines fortgesetzt, kein Befehl ans Geraet, bis
    sich Messgeraet oder TPS melden."""
    print("\n[B] Neustart waehrend der Notlueftung")
    # Zustand erzeugen: Sperre + beide weg + Notlueftung, dann persistieren
    units = house()
    pre = Sim(units, "B0")
    pre.at(1, MET_AV, "online")
    pre.at(1, MET_ST, json.dumps({"mittelwert": 60}))
    pre.at(1, TPS_AV, "online")
    pre.at(2, TPS_ST, json.dumps({"ventilating": False, "reason": "x"}))
    pre.at(5, TPS_AV, "offline")
    pre.at(6, MET_AV, "offline")
    await pre.run(40)
    check("B: Vorbereitung: Notlueftung aktiv", pre.nc_state()["dewpoint_fallback"] is True
          and units[KL].mode() == ("ManualHeatRecovery", "Low"))
    saved = json.load(open(bridge.NEURACELL_STATE_FILE))
    check("B: Zustandsdatei traegt Notlueftung und Baselines",
          saved.get("dewpoint_fallback") is True and set(saved["saved_modes"]) == {KL, KR}, saved)

    # Neustart: neue Bridge, Geraete stehen physisch noch in der Notlueftung
    units2 = house(cellar_op=OM.ManualHeatRecovery, cellar_fan=FS.Low)
    units2[KR]._status["operating_mode"] = OM.Smart      # Slave: eigenes Feld blieb Smart
    sim = Sim(units2, "B")
    retained = [
        (METERS, json.dumps({"radon_meters": {MET_ST: real_time.time() - 600}})),
        (MET_AV, "offline"),
        (TPS_AV, "offline"),                                  # Last Will der TPS ist gespeichert
        (TPS_ST, json.dumps({"ventilating": False, "reason": "x"})),
    ]

    def check_resumed(s):
        st = s.nc_state()
        check("B: nach Neustart Notlueftung fortgesetzt, Sperre gespeichert",
              st["dewpoint_fallback"] is True and st["dewpoint_block"] is True, st)
        check("B: kein einziger Befehl nach dem Neustart", not s.calls(KL) and not s.calls(KR)
              and all(not s.calls(x) for x in SMARTS), (s.calls(KL), s.calls(KR)))
        check("B: Log nennt die Fortsetzung", s.has_log("WARNING", "resumed", "fallback ventilation was in force"))
    sim.mark(10, check_resumed)
    sim.mark(50, lambda s: check("B: nach 50 min weiterhin keine Befehle", not s.calls(KL) and not s.calls(KR)))
    sim.at(90, MET_AV, "online")
    for m in (90, 100, 110, 120):
        sim.at(m, MET_ST, json.dumps({"mittelwert": 45}))

    def check_meter_back(s):
        st = s.nc_state()
        check("B: Messgeraet zurueck -> Notlueftung aus, OFFICE aus (TPS weiter weg, keep)",
              st["dewpoint_fallback"] is False and st["dewpoint_controller_connected"] is False
              and s.units[KL].mode()[0] == "Off" and s.calls(KR) and s.calls(KR)[-1][1] == "Off", (st, s.calls(KR)))
        check("B: dafuer genau ein Befehl je OFFICE", len(s.calls(KL)) == 1 and len(s.calls(KR)) == 1)
    sim.mark(95, check_meter_back)
    sim.at(120, TPS_AV, "online")
    sim.at(120, TPS_ST, json.dumps({"ventilating": True, "reason": "ok"}))
    sim.mark(125, lambda s: check("B: TPS gibt frei -> Smart/Medium zurueck (Baseline aus der Datei)",
                                  s.units[KL].mode() == ("Smart", "Medium") and s.calls(KR)[-1][1:] == ("Smart", "Medium")
                                  and not s.b.neuracell._saved_modes, (s.units[KL].mode(), s.calls(KR))))
    await sim.run(130, retained=retained, resume=True)
    sim.dump()
    check("B: keine Fehler/Tracebacks", not any(lv == "ERROR" for _t, lv, _m in sim.log))
    return sim


async def scenario_broker_restart_no_block():
    """29.10.-Ablauf: Broker/HA-Neustart ohne Sperre. TPS und Messgeraet
    kommen nicht zurueck. Nach 30 min gelten beide als getrennt (Sensoren
    aus -> Handy-Hinweis aus dem Paket), es wird nichts geschaltet."""
    print("\n[C] Broker-Neustart ohne Sperre: nur Ueberwachung, keine Befehle")
    units = house()
    sim = Sim(units, "C")
    retained = [
        (METERS, json.dumps({"radon_meters": {MET_ST: real_time.time() - 1200}})),
        (MET_AV, "online"),                                   # altes online (Broker neu, kein Last Will)
        (TPS_AV, "online"),
        (TPS_ST, json.dumps({"ventilating": True, "reason": "ok"})),
    ]

    def check_unknown(s):
        st = s.nc_state()
        check("C: nach 10 min beide noch unbekannt", st["dewpoint_controller_connected"] is None
              and st["radon_meter_connected"] is None, st)
    sim.mark(10, check_unknown)

    def check_lost(s):
        st = s.nc_state()
        check("C: nach 31 min beide getrennt (Sensoren aus)", st["dewpoint_controller_connected"] is False
              and st["radon_meter_connected"] is False, st)
        check("C: Log: TPS sent nothing after the restart", s.has_log("WARNING", "Taupunktsteuerung sent nothing for 30 min"))
        check("C: Log: Messgeraet still", s.has_log("WARNING", "radon meter", "sent nothing for 30 min"))
        check("C: keine Sperre, keine Notlueftung", st["dewpoint_block"] is False and st["dewpoint_fallback"] is False)
    sim.mark(32, check_lost)
    sim.mark(120, lambda s: check("C: zwei Stunden ohne einen Befehl",
                                  all(not u.mode_calls for u in s.units.values()), [(u.name, u.mode_calls) for u in s.units.values() if u.mode_calls]))
    await sim.run(121, retained=retained)
    sim.dump()
    return sim


async def scenario_radon_during_fallback():
    """Messgeraet kehrt mit hohem Wert zurueck: Radonschutz fuer alle 7
    Geraete (je genau ein Befehl), danach zurueck: OFFICE Sperre, Rest Baseline."""
    print("\n[D] Radonalarm waehrend der Notlueftung")
    units = house()
    sim = Sim(units, "D")
    sim.at(1, MET_AV, "online")
    sim.at(1, MET_ST, json.dumps({"mittelwert": 60}))
    sim.at(1, TPS_AV, "online")
    sim.at(2, TPS_ST, json.dumps({"ventilating": False, "reason": "x"}))
    sim.at(5, TPS_AV, "offline")
    sim.at(6, MET_AV, "offline")
    sim.mark(40, lambda s: check("D: Notlueftung aktiv", s.nc_state()["dewpoint_fallback"] is True))
    sim.at(45, MET_AV, "online")
    sim.at(45, MET_ST, json.dumps({"mittelwert": 320}))

    def check_radon(s):
        st = s.nc_state()
        check("D: Radon -> alle 7 Geraete Zuluft Medium, Notlueftung aus",
              st["radon_protection"] is True and st["dewpoint_fallback"] is False
              and all(u.mode() == ("Intake", "Medium") for u in s.units.values() if not u.is_slave)
              and all(u.mode_calls[-1][1:] == ("Intake", "Medium") for u in s.units.values()), st)
        n = {u.name: len(u.mode_calls) for u in s.units.values()}
        check("D: je SMART genau ein Befehl", all(n[x] == 1 for x in ("BueroM", "BueroL", "Eltern", "KindSchlafen", "KindSpielen")), n)
    sim.mark(47, check_radon)
    sim.mark(60, lambda s: check("D: Radon aktiv: keine Wiederholungen (SMART 1, OFFICE 3 Befehle: Off, MHR, Intake)",
                                 all(len(s.calls(x)) == 1 for x in SMARTS) and len(s.calls(KL)) == 3 and len(s.calls(KR)) == 3,
                                 {u.name: u.mode_calls for u in s.units.values()}))
    sim.at(70, MET_ST, json.dumps({"mittelwert": 40}))

    def check_after(s):
        st = s.nc_state()
        check("D: Radon vorbei -> OFFICE aus (Sperre), SMART zurueck auf Smart/Medium",
              st["radon_protection"] is False and s.units[KL].mode()[0] == "Off"
              and all(s.units[x].mode() == ("Smart", "Medium") for x in SMARTS if not s.units[x].is_slave)
              and all(s.calls(x)[-1][1:] == ("Smart", "Medium") for x in SMARTS), st)
        check("D: Messgeraet da, TPS weg -> keine Notlueftung mehr scharf", s.b.neuracell._blind_since is None)
    sim.mark(75, check_after)
    await sim.run(80)
    sim.dump()
    check("D: keine Fehler/Tracebacks", not any(lv == "ERROR" for _t, lv, _m in sim.log))
    return sim


def nils_house():
    """5 SMART in einer Zone wie beim Kunden (Nov. 2026): Flur Master, vier
    SlaveEqualMaster. Alle Smart."""
    units = [
        Unit("E05A1B9C0DAC", "Flur", 0, "Master", OM.Smart),
        Unit("E05A1B9BAF0C", "Wohnzimmer", 0, "SlaveEqualMaster", OM.Smart),
        Unit("CCDBA789F890", "Kinderzimmer", 0, "SlaveEqualMaster", OM.Smart),
        Unit("E05A1B9C1111", "Schlafzimmer", 0, "SlaveEqualMaster", OM.Smart),
        Unit("E05A1B9C2222", "Gaeste Bad", 0, "SlaveEqualMaster", OM.Smart),
    ]
    return {u.serial_number: u for u in units}


WOZI, FLUR = "E05A1B9BAF0C", "E05A1B9C0DAC"


async def scenario_unit_unplugged():
    """Kundenprotokoll 31.10./01.11. (Bridge 1.4.41): das Wohnzimmer wird zum
    Streichen 9 h vom Strom genommen - die Cloud antwortet 1063 Abrufe lang
    404 "Status packet not found". Diagnose: Letztes Statuspaket bleibt stehen,
    Fehlabrufe zaehlen, Ausfall von/bis im Attribut; Topologie einmal im Log;
    nichts davon kostet einen Abruf der anderen vier."""
    print("\n[E] Einheit 9 h stromlos (1063 Abrufe 404) -> Diagnose, Topologie, Rueckkehr")
    units = nils_house()
    sim = Sim(units, "E", neuracell_enabled=False, dewpoint_enabled=False)
    for sn in units:
        sim.b._device_house[sn] = 1

    async def no_reauth(reason):          # 9 h Simulation: kein echter Token-Refresh gegen die Cloud
        return True
    sim.b._reauth = no_reauth
    t_start = {}

    def unplug(s):
        t_start["wall"] = bridge.time.time()
        s.units[WOZI].offline_polls = 1063
    sim.mark(10, unplug)                      # ab Minute 10 ohne Strom

    def diag(serial):
        msgs = [json.loads(p) for t, p in sim.b.client.pub if t == bridge.diag_topic("ambientika", serial)]
        return msgs[-1] if msgs else {}

    def during(s):
        d = diag(WOZI)
        check("E: waehrend des Ausfalls - Zaehler laufen, Ausfall offen, letztes Paket steht (~Minute 10)",
              d.get("poll_failures_consecutive", 0) >= 100 and d["last_outage_end"] is None
              and d["last_outage_polls"] == d["poll_failures_consecutive"]
              and d["last_status"] is not None and d["last_status"] <= bridge._iso_utc(t_start["wall"] + 1), d)
        av = [p for t, p in s.b.client.pub if t == "ambientika/%s/availability" % WOZI]
        check("E: Wohnzimmer nach 3 Fehlabrufen offline, Flur online", av and av[-1] == "offline"
              and [p for t, p in s.b.client.pub if t == "ambientika/%s/availability" % FLUR][-1] == "online")
        df = diag(FLUR)
        check("E: Flur ohne Fehlabrufe, letztes Paket aktuell",
              df["poll_failures_total"] == 0 and df["last_status"] >= bridge._iso_utc(t_start["wall"] + 50 * 60), df)
    sim.mark(70, during)

    def after(s):
        d = diag(WOZI)
        check("E: nach der Rueckkehr - 1063 Fehlabrufe, Ausfall geschlossen, consecutive 0",
              d["poll_failures_total"] == 1063 and d["poll_failures_consecutive"] == 0
              and d["last_outage_polls"] == 1063 and d["last_outage_end"] is not None
              and d["last_outage_start"] is not None and d["last_status"] >= d["last_outage_end"], d)
        start, end = d["last_outage_start"], d["last_outage_end"]
        from datetime import datetime as _dt
        dur_min = (_dt.fromisoformat(end) - _dt.fromisoformat(start)).total_seconds() / 60.0
        check("E: Ausfalldauer ~ 1063 Abrufe a 30 s = 531.5 min (von/bis im Attribut)",
              530.0 <= dur_min <= 533.0, dur_min)
        check("E: Log - 'answering again after 1063 failed poll(s) since <Beginn>'",
              s.has_log("INFO", "Device %s is answering again after 1063 failed poll(s) since %s" % (WOZI, start)), [m for t, lv, m in s.log if "answering" in m])
        warn = [m for t, lv, m in s.log if lv == "WARNING"]
        detail = [m for m in warn if m.startswith("status() failed for %s" % WOZI)]
        hourly = [m for m in detail if "failed polls in a row since" in m]
        check("E: Warnungen - 3x Detail bis zur Schwelle, 1x offline, dann stuendliche Zusammenfassung (9), sonst nichts",
              len(detail) - len(hourly) == 3 and hourly and 8 <= len(hourly) <= 10
              and warn.count("Poll failed 3 times in a row for %s (status() failed) - marking offline." % WOZI) == 1
              and len(warn) == len(detail) + 1, (len(warn), len(detail), len(hourly), warn[:6]))
        check("E: Zusammenfassung nennt Zaehler und Beginn",
              hourly and "in a row since %s" % start in hourly[-1] and "Failed Polls sensor" in hourly[-1], hourly[-1:])
        check("E: keine 1063 Einzelwarnungen mehr", len(warn) < 20, len(warn))
        av = [p for t, p in s.b.client.pub if t == "ambientika/%s/availability" % WOZI]
        check("E: Wohnzimmer wieder online", av[-1] == "online")
        topo = [m for t, lv, m in s.log if m.startswith("Zone topology")]
        check("E: Topologie genau einmal, eine Zone: Flur Master + vier Slaves, keine 'changed'-Zeile",
              len(topo) == 1 and topo[0].startswith("Zone topology - zone 0: Master Flur (E05A1B9C0DAC); Slaves: ")
              and topo[0].count("SlaveEqualMaster") == 4 and "changed" not in topo[0], topo)
        ri = [json.loads(p) for t, p in s.b.client.pub if t == bridge.reset_info_topic("ambientika", WOZI)]
        check("E: reset_info Wohnzimmer einmal: Slave, Master Flur, Zaehler bleibt",
              len(ri) == 1 and ri[0]["device_role"] == "SlaveEqualMaster" and ri[0]["zone_master"] == "Flur (E05A1B9C0DAC)"
              and ri[0]["cloud_reset_clears_counter"] is False, ri)
        n_states = {sn: sum(1 for t, p in s.b.client.pub if t == "ambientika/%s/state" % sn) for sn in s.units}
        check("E: die anderen vier haben jeden Abruf einen Zustand bekommen (>= 1080), Wohnzimmer ~1063 weniger",
              all(n_states[sn] >= 1080 for sn in s.units if sn != WOZI)
              and n_states[FLUR] - n_states[WOZI] in range(1060, 1067), n_states)
    sim.mark(10 + 532 + 5, after)
    await sim.run(10 + 532 + 8)
    check("E: keine Fehler/Tracebacks", not any(lv == "ERROR" for _t, lv, _m in sim.log))
    return sim


async def main_async():
    await scenario_cellar_outage()
    await scenario_restart_during_fallback()
    await scenario_broker_restart_no_block()
    await scenario_radon_during_fallback()
    await scenario_unit_unplugged()


def main():
    FAILS.clear()
    asyncio.run(main_async())
    print("\nRESULT:", "ALL PASS" if not FAILS else f"{len(FAILS)} FAIL -> {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
