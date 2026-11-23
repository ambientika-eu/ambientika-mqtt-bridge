"""Test driver inside a real Home Assistant for the integration 'Ambientika for Home Assistant'.

The integration is the released code; only its DEFAULT_HOST points at the local
stand-in for the Ambientika cloud. Results go to SIM_HA_RESULT.

Two things are checked from version 1.3 on and only noted for older versions:
binary sensors of a unit without status are unavailable, and an account that is
already set up is refused a second time.

From version 1.4 on the status of a unit is read once for all its entities.
Checked from then on, noted for older versions: how often the cloud is asked
(at the set-up, per minute, for an update of all entities of a unit), one line
in the log for a unit that stops answering, and a request the cloud never
answers.

From version 1.6 on the integration works like the Ambientika MQTT Bridge:
select entities (also for the dusk sensor), measured values with plausibility
check and averages, a Slave shown with its Master's mode, the filter reset sent
to the zone Master and checked, commands collected into one call, a unit
unavailable only after three failed readings, diagnostics and options.
"""
import asyncio
import json
import logging
import os
import time

import aiohttp
import voluptuous as vol

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.loader import async_get_integration

DOMAIN = "simtest_hacs"
CONFIG_SCHEMA = vol.Schema({DOMAIN: object}, extra=vol.ALLOW_EXTRA)
_LOGGER = logging.getLogger(__name__)

CLOUD = os.environ["SIM_CLOUD_URL"]
RESULT = os.environ["SIM_HA_RESULT"]
USER, PASSWORD = "kunde@example.com", "geheim#1 äöü"
MASTER, SLAVE2 = "A0B1C2D3E402", "A0B1C2D3E403"
SERIALS = ["A0B1C2D3E401", "A0B1C2D3E402", "A0B1C2D3E403", "A0B1C2D3E404", "A0B1C2D3E405"]
NAMES = ["Flur", "Wohnzimmer", "Kinderzimmer", "Gäste-Bad", "Schlafzimmer Eltern"]
RESULTS = []


def check(name, cond, detail=None):
    RESULTS.append({"name": name, "ok": bool(cond), "detail": None if cond else str(detail)[:900]})
    _LOGGER.info("%s %s%s", "PASS" if cond else "FAIL", name, "" if cond else "  <<< %s" % str(detail)[:900])
    return bool(cond)


def note(name, detail):
    RESULTS.append({"name": "OBSERVATION " + name, "ok": True, "detail": str(detail)[:900]})
    _LOGGER.info("NOTE %s: %s", name, str(detail)[:900])


class Warnings(logging.Handler):
    """The warnings and errors the integration writes to the log."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.lines = []

    def emit(self, record):
        self.lines.append("%s %s" % (record.levelname, record.getMessage()))


async def until(fn, timeout=20.0, step=0.2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            v = fn()
            if asyncio.iscoroutine(v):
                v = await v
        except Exception:  # noqa: BLE001
            v = None
        if v:
            return v
        await asyncio.sleep(step)
    return None


class Cloud:
    def __init__(self):
        self.session = aiohttp.ClientSession()

    async def req(self, method, path, body=None):
        async with self.session.request(method, CLOUD + path, json=body) as r:
            return await r.json()

    async def log(self, kind=None):
        return [x for x in await self.req("GET", "/_ctl/log") if kind is None or x["kind"] == kind]

    async def clear(self):
        await self.req("POST", "/_ctl/clear", {})

    async def asked(self):
        """How often the status of each unit was asked for since the last clear()."""
        return (await self.req("GET", "/_ctl/counts")).get("status", {})

    async def unit(self, serial, set=None, fault="-"):
        body = {"serial": serial}
        if set is not None:
            body["set"] = set
        if fault != "-":
            body["fault"] = fault
        await self.req("POST", "/_ctl/unit", body)

    async def outage(self, mode):
        await self.req("POST", "/_ctl/outage", {"mode": mode})


async def run(hass: HomeAssistant):
    # Home Assistant 2026.11 tries out a changed HTTP configuration (the port of
    # this test) and restarts after five minutes unless someone confirms it.
    try:
        from homeassistant.components.http.config import DATA_STORE  # noqa: PLC0415
        hass.data[DATA_STORE].async_cancel_revert()
    except Exception:  # noqa: BLE001  (older versions have no such trial)
        pass
    cloud = Cloud()
    ent, dev = er.async_get(hass), dr.async_get(hass)
    flow = hass.config_entries.flow

    def st(entity_id):
        s = hass.states.get(entity_id)
        return s.state if s else None

    def attr(entity_id, name):
        s = hass.states.get(entity_id)
        return s.attributes.get(name) if s else None

    async def refresh(ids):
        await hass.services.async_call("homeassistant", "update_entity", {"entity_id": ids}, blocking=True)

    integration = await async_get_integration(hass, "ambientika")
    version = tuple(int(x) for x in str(integration.version).split(".")[:2])
    since_1_3 = version >= (1, 3)
    since_1_4 = version >= (1, 4)
    since_1_6 = version >= (1, 6)
    fails = 3 if since_1_6 else 1   # failed readings until a unit is unavailable
    _LOGGER.info("integration version %s", integration.version)
    warnings = Warnings()
    logging.getLogger("custom_components.ambientika").addHandler(warnings)

    if os.environ.get("SIM_SCENARIO") == "same-name":
        # two units carry the same name in the app
        try:
            await cloud.req("POST", "/_ctl/unit", {"serial": "A0B1C2D3E401", "name": "Flur"})
            res = await flow.async_init("ambientika", context={"source": "user"})
            res = await flow.async_configure(res["flow_id"], {"username": USER, "password": PASSWORD})
            await hass.async_block_till_done()
            entry = hass.config_entries.async_entries("ambientika")[0]
            ents = er.async_entries_for_config_entry(ent, entry.entry_id)
            devs = dr.async_entries_for_config_entry(dev, entry.entry_id)
            counts = {}
            for e in ents:
                counts[e.domain] = counts.get(e.domain, 0) + 1
            check("two units with the same name: five devices", len(devs) == 5, sorted(d.name for d in devs))
            check("two units with the same name: every unit has all its entities (%d in total)" % len(ents),
                  len(ents) % 5 == 0 and len(ents) >= 30 and len(set(counts.values()) - {5, 10, 20}) <= 1, counts)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("test driver failed")
            check("test driver ran through", False, repr(exc))
        finally:
            await cloud.session.close()

            def _write2():
                with open(RESULT, "w", encoding="utf-8") as fh:
                    json.dump(RESULTS, fh, indent=1, ensure_ascii=False)
            await hass.async_add_executor_job(_write2)
            hass.async_create_task(hass.async_stop())
        return

    try:
        # ---- set-up through the config flow
        res = await flow.async_init("ambientika", context={"source": "user"})
        check("config flow opens with user name and password", res["type"] == "form" and res["step_id"] == "user", res)
        res = await flow.async_configure(res["flow_id"], {"username": USER, "password": "falsch"})
        check("wrong password: the form comes back with the error 'auth'",
              res["type"] == "form" and res.get("errors") == {"base": "auth"}, res.get("errors"))
        await cloud.clear()
        res = await flow.async_configure(res["flow_id"], {"username": USER, "password": PASSWORD})
        check("right password: entry created, titled with the user name",
              res["type"] == "create_entry" and res["title"] == USER, {k: res.get(k) for k in ("type", "title", "errors")})
        await hass.async_block_till_done()
        entries = hass.config_entries.async_entries("ambientika")
        entry = entries[0] if entries else None
        check("entry loaded", entry is not None and entry.state.value == "loaded", entry.state if entry else None)
        counts = await cloud.req("GET", "/_ctl/counts")
        asked = counts.get("status", {})
        detail = "sign-ins %s (one of them checks the password in the dialog), status requests per unit %s" % (
            counts.get("auth"), sorted(asked.values()))
        if since_1_4:
            check("set-up: one sign-in after the one of the dialog, the status of every unit read once",
                  counts.get("auth") == 2 and sorted(asked) == SERIALS and set(asked.values()) == {1}, detail)
        else:
            note("what the set-up asks the cloud", detail)
        ents = er.async_entries_for_config_entry(ent, entry.entry_id) if entry else []
        devs = dr.async_entries_for_config_entry(dev, entry.entry_id) if entry else []
        check("five devices with the names of the app", sorted(d.name for d in devs) == sorted(NAMES),
              sorted(d.name for d in devs))
        by = {}
        for e in ents:
            by.setdefault(e.domain, []).append(e)
        counts = {k: len(v) for k, v in sorted(by.items())}
        check("entities per unit: 1 climate, 1 button, binary sensors, sensors (same for every unit)",
              counts.get("climate") == 5 and counts.get("button") == 5 and counts.get("binary_sensor", 0) % 5 == 0
              and counts.get("sensor", 0) % 5 == 0 and counts.get("sensor", 0) >= 10, counts)
        _LOGGER.info("entity counts: %s", counts)
        flur = next((d for d in devs if d.name == "Flur"), None)
        f_ents = {e.domain: [] for e in ents}
        for e in ents:
            if flur and e.device_id == flur.id:
                f_ents[e.domain].append(e.entity_id)
        climate = (f_ents.get("climate") or [None])[0]
        button = (f_ents.get("button") or [None])[0]
        all_ids = [e.entity_id for e in ents if not e.disabled_by]
        reading_ids = [i for i in all_ids if not i.startswith("button.")]
        # averaged values need two minutes of readings
        slow = {e.entity_id for e in ents if e.unique_id.endswith("_avg")} if since_1_6 else set()
        early = {i: st(i) for i in reading_ids if i not in slow and st(i) in (None, "unavailable", "unknown")}
        if since_1_4:
            check("right after the set-up every entity shows a value, without an update being asked for", not early, early)
        else:
            note("entities without a value right after the set-up", sorted(early))
        await refresh(all_ids)
        states = {i: st(i) for i in all_ids}
        bad = {k: v for k, v in states.items() if v in (None, "unavailable")}
        check("every entity has a state and is available", not bad, bad)
        flur_reading = [i for i in reading_ids if flur and ent.entities[i].device_id == flur.id]
        await cloud.clear()
        await refresh(flur_reading)
        asked = await cloud.asked()
        # a reading of all units that falls into this moment asks for both units
        own = asked.get(MASTER, 0) - asked.get(SLAVE2, 0)
        if since_1_4:
            check("update of the %d reading entities of one unit (homeassistant.update_entity): the cloud is asked once"
                  % len(flur_reading), own == 1 and (since_1_6 or len(flur_reading) == 5), asked)
        else:
            note("update of the %d reading entities of one unit" % len(flur_reading), "status requests: %s" % asked)
        check("climate of 'Flur': on, preset Smart, fan medium, 21 °C, 55 %",
              climate and st(climate) not in ("off", "unavailable") and attr(climate, "preset_mode") == "Smart"
              and attr(climate, "fan_mode") == "medium" and attr(climate, "current_temperature") == 21
              and attr(climate, "current_humidity") == 55,
              (climate, st(climate), hass.states.get(climate).attributes if climate and hass.states.get(climate) else None))
        check("fan modes that can be set: low, medium, high (Night and Turbo are the unit's own steps)",
              climate and attr(climate, "fan_modes") == ["low", "medium", "high"], attr(climate, "fan_modes"))
        check("twelve operating modes as presets", climate and len(attr(climate, "preset_modes") or []) == 12,
              attr(climate, "preset_modes"))
        _LOGGER.info("states of 'Flur': %s", {i: st(i) for d in f_ents.values() for i in d})

        # ---- commands
        await cloud.clear()
        await hass.services.async_call("climate", "set_preset_mode", {"entity_id": climate, "preset_mode": "Night"}, blocking=True)
        call = await until(lambda: cloud.log("change-mode"), 10)
        check("preset Night -> change-mode with mode 3, fan and humidity level kept",
              call and call[0]["ok"] and str(call[0]["body"]["operatingMode"]) == "3" and call[0]["body"]["fanSpeed"] == 1
              and call[0]["body"]["humidityLevel"] == 1, call)
        check("Home Assistant shows the preset Night right after the command, without an update being asked for",
              attr(climate, "preset_mode") == "Night", attr(climate, "preset_mode"))
        asked = await cloud.asked()
        own = asked.get(MASTER, 0) - asked.get(SLAVE2, 0)
        if since_1_4:
            check("after the command the status of that unit is read once", own == 1, asked)
        else:
            note("status requests after a command", asked)
        await refresh([climate])
        check("Home Assistant shows the preset Night", attr(climate, "preset_mode") == "Night", attr(climate, "preset_mode"))
        await cloud.clear()
        await hass.services.async_call("climate", "set_fan_mode", {"entity_id": climate, "fan_mode": "high"}, blocking=True)
        call = await until(lambda: cloud.log("change-mode"), 10)
        check("fan mode high -> fan 2, mode kept", call and call[0]["ok"] and call[0]["body"]["fanSpeed"] == 2
              and str(call[0]["body"]["operatingMode"]) == "3", call)
        await cloud.clear()
        await hass.services.async_call("climate", "set_humidity", {"entity_id": climate, "humidity": 3}, blocking=True)
        call = await until(lambda: cloud.log("change-mode"), 10)
        check("target humidity 3 -> humidity level Moist (2)", call and call[0]["ok"] and call[0]["body"]["humidityLevel"] == 2, call)
        await cloud.clear()
        await hass.services.async_call("climate", "turn_off", {"entity_id": climate}, blocking=True)
        call = await until(lambda: cloud.log("change-mode"), 10)
        check("turn off -> mode Off (11)", call and call[0]["ok"] and str(call[0]["body"]["operatingMode"]) == "11", call)
        await refresh([climate])
        check("Home Assistant shows off", st(climate) == "off", st(climate))
        await cloud.clear()
        await hass.services.async_call("climate", "turn_on", {"entity_id": climate}, blocking=True)
        call = await until(lambda: cloud.log("change-mode"), 10)
        check("turn on -> mode Auto (1)", call and call[0]["ok"] and str(call[0]["body"]["operatingMode"]) == "1", call)
        await cloud.clear()
        try:
            await hass.services.async_call("climate", "set_preset_mode", {"entity_id": climate, "preset_mode": "Hyperdrive"}, blocking=True)
            refused = "accepted"
        except Exception as exc:  # noqa: BLE001
            refused = type(exc).__name__
        await asyncio.sleep(1)
        check("a preset that does not exist is refused and nothing is sent (%s)" % refused,
              refused != "accepted" and not await cloud.log("change-mode"), await cloud.log("change-mode"))
        if since_1_6:
            await more_1_6(hass, cloud, ent, devs, ents, st, attr, refresh, climate, button, flow, entry)
        else:
            await cloud.clear()
            await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
            call = await until(lambda: cloud.log("reset-filter"), 10)
            check("filter reset button -> reset call for the unit", call and call[0]["serial"] == MASTER, call)
        check("nothing but the documented calls reached the cloud", not await cloud.log("UNEXPECTED"), await cloud.log("UNEXPECTED"))

        # ---- values and the unit's own fan steps
        await cloud.unit(MASTER, set={"temperature": 23.5, "humidity": 61, "airQuality": "Poor", "filtersStatus": "Bad",
                                      "humidityAlarm": True, "nightAlarm": True, "fanSpeed": "Night"})
        await refresh([e for e in all_ids if flur and ent.entities[e].device_id == flur.id])
        check("climate follows: 23.5 °C, 61 %, fan step 'night' shown",
              attr(climate, "current_temperature") == 23.5 and attr(climate, "current_humidity") == 61
              and attr(climate, "fan_mode") == "night", hass.states.get(climate).attributes)
        f_states = {i: st(i) for d in f_ents.values() for i in d}
        check("sensors follow (air quality poor, filter bad, both alarms on)",
              "poor" in f_states.values() and "bad" in f_states.values()
              and list(f_states.values()).count("on") >= 2, f_states)
        await cloud.clear()
        await hass.services.async_call("climate", "set_preset_mode", {"entity_id": climate, "preset_mode": "Smart"}, blocking=True)
        call = await until(lambda: cloud.log("change-mode"), 10)
        check("a command while the unit reports its night step is accepted by the cloud (no value out of range)",
              call and call[0]["ok"] and call[0]["body"]["fanSpeed"] in (0, 1, 2), call)
        await cloud.unit(MASTER, set={"fanSpeed": "Turbo"})
        await refresh([climate])
        check("fan step 'turbo' shown", attr(climate, "fan_mode") == "turbo", attr(climate, "fan_mode"))
        await cloud.unit(MASTER, set={"operatingMode": "BrandNewMode", "fanSpeed": "Medium"})
        await refresh([climate])
        check("a mode the library does not know leaves the entity available", st(climate) not in (None, "unavailable"),
              (st(climate), attr(climate, "preset_mode")))
        await cloud.unit(MASTER, set={"operatingMode": "Smart", "nightAlarm": False, "humidityAlarm": False})

        # ---- outages
        k_dev = next((d for d in devs if d.name == "Kinderzimmer"), None)
        k_ids = [e.entity_id for e in ents if k_dev and e.device_id == k_dev.id and not e.disabled_by]
        # diagnostics stay readable while the unit is unavailable (since 1.6)
        k_diag = {e.entity_id for e in ents if e.unique_id.endswith(("_last_status", "_poll_failures"))}
        k_ids = [i for i in k_ids if i not in k_diag]
        all_ids = [i for i in all_ids if i not in k_diag]
        reading_ids = [i for i in reading_ids if i not in k_diag]
        await cloud.unit(SLAVE2, fault="404")
        if since_1_6:
            await refresh(k_ids)
            check("one failed reading: the unit keeps its last values (unavailable after three in a row)",
                  all(st(i) not in (None, "unavailable") for i in k_ids if not i.startswith("button.")),
                  {i: st(i) for i in k_ids})
            await refresh(k_ids)
        await refresh(k_ids)
        k_states = {i: st(i) for i in k_ids}
        check("unit without status packet: climate and sensors become unavailable",
              all(v == "unavailable" for i, v in k_states.items() if i.startswith(("climate.", "sensor."))), k_states)
        k_binary = {i: v for i, v in k_states.items() if i.startswith("binary_sensor.")}
        if since_1_3:
            check("unit without status packet: its binary sensors are unavailable too",
                  k_binary and all(v == "unavailable" for v in k_binary.values()), k_binary)
        else:
            RESULTS.append({"name": "OBSERVATION binary sensors of a unit without status packet", "ok": True,
                            "detail": str(k_binary)})
        check("the other units stay available", st(climate) not in (None, "unavailable"))
        await cloud.unit(SLAVE2, fault=None)
        await refresh(k_ids)
        check("unit back: available again", all(st(i) not in (None, "unavailable") for i in k_ids), {i: st(i) for i in k_ids})
        await cloud.outage("500")
        for _ in range(fails):
            await refresh(all_ids)
        n_unavail = sum(1 for i in all_ids if st(i) == "unavailable")
        check("cloud answers 500: climate and sensors unavailable, Home Assistant keeps running (%d of %d entities)"
              % (n_unavail, len(all_ids)),
              all(st(i) == "unavailable" for i in all_ids if i.startswith(("climate.", "sensor."))),
              {i: st(i) for i in all_ids if st(i) != "unavailable"})
        await cloud.outage("ok")
        await refresh(all_ids)
        check("cloud back: everything available again", not [i for i in all_ids if st(i) in (None, "unavailable")],
              [i for i in all_ids if st(i) in (None, "unavailable")])

        # ---- nobody asks for an update: the integration reads by itself
        k_reading = [i for i in k_ids if not i.startswith("button.")]
        await cloud.unit(MASTER, set={"temperature": 19.5})
        await cloud.unit(SLAVE2, fault="404")
        await cloud.clear()
        del warnings.lines[:]
        await asyncio.sleep(62)
        asked = await cloud.asked()
        if since_1_6:
            await until(lambda: all(st(i) == "unavailable" for i in k_reading), 60)
        check("a changed value arrives by itself within a minute (19.5 °C)",
              attr(climate, "current_temperature") == 19.5, attr(climate, "current_temperature"))
        check("a unit that stops answering becomes unavailable by itself",
              all(st(i) == "unavailable" for i in k_reading), {i: st(i) for i in k_reading})
        detail = "status requests per unit in 62 s: %s" % {s[-2:]: asked.get(s, 0) for s in SERIALS}
        if since_1_4:
            check("one status request per unit every 30 s for all its entities (2 or 3 in 62 s, the same for every unit)",
                  sorted(asked) == SERIALS and all(2 <= n <= 3 for n in asked.values())
                  and max(asked.values()) - min(asked.values()) <= 1, detail)
            check("the unit that stopped answering is reported once in the log, not at every reading",
                  len(warnings.lines) == 1 and SLAVE2 in warnings.lines[0], warnings.lines[:8])
            if since_1_6:
                failed = next((e.entity_id for e in ents if e.unique_id == SLAVE2 + "_poll_failures"), None)
                check("Failed Readings of that unit counts and stays readable while the unit is unavailable",
                      failed and st(failed) not in (None, "unavailable", "unknown") and int(st(failed)) >= 3
                      and attr(failed, "consecutive_failures") >= 3, (failed, st(failed)))
        else:
            note("how often the cloud is asked", detail)
            note("log while one unit does not answer for 62 s", "%d warnings and errors, e.g. %s"
                 % (len(warnings.lines), warnings.lines[:1]))
        await cloud.unit(SLAVE2, fault=None)
        back = await until(lambda: all(st(i) not in (None, "unavailable") for i in k_reading), 75)
        check("the unit answers again: available by itself", back, {i: st(i) for i in k_reading})

        if since_1_4:
            # ---- requests the cloud accepts and never answers
            await cloud.unit(SLAVE2, fault="hang")
            await cloud.unit(MASTER, set={"temperature": 18.5})
            t0 = time.monotonic()
            followed = await until(lambda: attr(climate, "current_temperature") == 18.5, 50)
            check("the request for one unit is never answered: the other units are still read on time (%.0f s)"
                  % (time.monotonic() - t0), followed, attr(climate, "current_temperature"))
            gone = await until(lambda: all(st(i) == "unavailable" for i in k_reading), 80 * fails)
            check("  ... and that unit becomes unavailable after the time limit (%.0f s)" % (time.monotonic() - t0),
                  gone, {i: st(i) for i in k_reading})
            await cloud.unit(SLAVE2, fault=None)
            back = await until(lambda: all(st(i) not in (None, "unavailable") for i in k_reading), 80)
            check("  ... and is available again once it answers", back, {i: st(i) for i in k_reading})
            await cloud.outage("hang")
            t0 = time.monotonic()
            gone = await until(lambda: all(st(i) == "unavailable" for i in reading_ids), 100 * fails)
            check("the cloud answers no request at all: every unit unavailable after the time limit (%.0f s), "
                  "Home Assistant keeps running" % (time.monotonic() - t0), gone,
                  {i: st(i) for i in reading_ids if st(i) != "unavailable"})
            await cloud.outage("ok")
            t0 = time.monotonic()
            back = await until(lambda: not [i for i in reading_ids if st(i) in (None, "unavailable")], 100)
            check("the cloud answers again: everything available by itself (%.0f s)" % (time.monotonic() - t0), back,
                  [i for i in reading_ids if st(i) in (None, "unavailable")])

        # ---- reload and unload
        n_before = len(er.async_entries_for_config_entry(ent, entry.entry_id))
        check("reload of the entry", await hass.config_entries.async_reload(entry.entry_id) and entry.state.value == "loaded",
              entry.state)
        await hass.async_block_till_done()
        n_after = len(er.async_entries_for_config_entry(ent, entry.entry_id))
        check("no entity added or lost over the reload (%d)" % n_after, n_after == n_before
              and not [e.entity_id for e in er.async_entries_for_config_entry(ent, entry.entry_id)
                       if e.entity_id.endswith("_2")], (n_before, n_after))
        res = await flow.async_init("ambientika", context={"source": "user"})
        res = await flow.async_configure(res["flow_id"], {"username": USER, "password": PASSWORD})
        if since_1_3:
            check("second set-up with the same account: refused as already set up",
                  res.get("type") == "abort" and res.get("reason") == "already_configured"
                  and len(hass.config_entries.async_entries("ambientika")) == 1,
                  "%s %s" % (res.get("type"), res.get("reason") or res.get("title")))
        else:
            RESULTS.append({"name": "OBSERVATION second set-up with the same account", "ok": True,
                            "detail": "%s %s" % (res.get("type"), res.get("reason") or res.get("title"))})
        await hass.async_block_till_done()
        check("unload of the entry", await hass.config_entries.async_unload(entry.entry_id) and entry.state.value == "not_loaded",
              entry.state)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.exception("test driver failed")
        check("test driver ran through", False, repr(exc))
    finally:
        await cloud.session.close()

        def _write():
            with open(RESULT, "w", encoding="utf-8") as fh:
                json.dump(RESULTS, fh, indent=1, ensure_ascii=False)
        await hass.async_add_executor_job(_write)
        hass.async_create_task(hass.async_stop())


async def more_1_6(hass, cloud, ent, devs, ents, st, attr, refresh, climate, button, flow, entry):
    """What the integration does like the Ambientika MQTT Bridge since 1.6."""
    def eid(serial, key, domain="sensor"):
        return next((e.entity_id for e in ents if e.unique_id == "%s_%s" % (serial, key) and e.domain == domain), None)

    async def call_select(serial, key, option):
        await hass.services.async_call("select", "select_option",
                                       {"entity_id": eid(serial, key + "_select", "select"), "option": option}, blocking=True)

    SLAVE1 = "A0B1C2D3E401"
    await cloud.unit(MASTER, set={"operatingMode": "Smart", "fanSpeed": "Medium", "humidityLevel": "Normal",
                                  "lightSensorLevel": "Low"})
    await refresh([climate])
    per_unit = {}
    for e in ents:
        per_unit.setdefault(e.device_id, []).append(e)
    sizes = {len(v) for v in per_unit.values()}
    check("every unit has the same entities (%s each, some of them disabled until switched on)" % sorted(sizes),
          len(sizes) == 1 and min(sizes) >= 30, {k: len(v) for k, v in per_unit.items()})
    sel = {k: eid(MASTER, k + "_select", "select") for k in ("operating_mode", "fan_speed", "humidity_level", "light_sensor_level")}
    check("four select entities: mode smart, fan medium, humidity level normal, dusk sensor low",
          all(sel.values()) and [st(sel[k]) for k in sel] == ["smart", "medium", "normal", "low"], {k: st(v) for k, v in sel.items()})
    check("dusk sensor options: not available, off, low, medium",
          attr(sel["light_sensor_level"], "options") == ["not_available", "off", "low", "medium"], attr(sel["light_sensor_level"], "options"))

    # ---- the dusk sensor can be set, and no other command changes it
    await cloud.clear()
    await call_select(MASTER, "light_sensor_level", "medium")
    call = await cloud.log("change-mode")
    check("dusk sensor medium -> change-mode with lightSensorLevel 3, mode, fan and humidity level kept",
          len(call) == 1 and call[0]["ok"] and call[0]["body"].get("lightSensorLevel") == 3
          and str(call[0]["body"]["operatingMode"]) == "0" and call[0]["body"]["fanSpeed"] == 1
          and call[0]["body"]["humidityLevel"] == 1, call)
    check("Home Assistant shows the dusk sensor as medium right after the command",
          st(sel["light_sensor_level"]) == "medium" and st(eid(MASTER, "light_sensor_level")) == "medium", st(sel["light_sensor_level"]))
    await cloud.clear()
    await hass.services.async_call("climate", "set_fan_mode", {"entity_id": climate, "fan_mode": "low"}, blocking=True)
    call = await cloud.log("change-mode")
    check("a fan command sends the dusk sensor level along unchanged (3)",
          len(call) == 1 and call[0]["ok"] and call[0]["body"].get("lightSensorLevel") == 3 and call[0]["body"]["fanSpeed"] == 0, call)
    # a unit that is switched off reports a default level
    await hass.services.async_call("climate", "turn_off", {"entity_id": climate}, blocking=True)
    await cloud.unit(MASTER, set={"lightSensorLevel": "Off"})
    await refresh([climate])
    await cloud.clear()
    await hass.services.async_call("climate", "turn_on", {"entity_id": climate}, blocking=True)
    call = await cloud.log("change-mode")
    check("switched on again: the dusk sensor level set before (3) is sent, not the default the unit reported while off",
          len(call) == 1 and call[0]["body"].get("lightSensorLevel") == 3, call)

    # ---- several commands at once go out in one call
    await cloud.clear()
    await asyncio.gather(call_select(MASTER, "operating_mode", "manual_heat_recovery"),
                         call_select(MASTER, "fan_speed", "high"), call_select(MASTER, "humidity_level", "dry"))
    call = await cloud.log("change-mode")
    check("mode, fan and humidity level set together: one change-mode with all three",
          len(call) == 1 and call[0]["ok"] and str(call[0]["body"]["operatingMode"]) == "2" and call[0]["body"]["fanSpeed"] == 2
          and call[0]["body"]["humidityLevel"] == 0, call)
    await cloud.clear()
    await call_select(MASTER, "fan_speed", "turbo")
    call = await cloud.log("change-mode")
    check("fan step turbo selected: the nearest step that can be set is sent (high)",
          len(call) == 1 and call[0]["ok"] and call[0]["body"]["fanSpeed"] == 2, call)

    # ---- a Slave is shown with the mode of its zone Master
    await cloud.unit(SLAVE1, set={"operatingMode": "Surveillance"})
    await refresh([climate, eid(SLAVE1, "operating_mode")])
    check("Slave reports Surveillance, Master runs ManualHeatRecovery: the Slave's mode shows the Master's, 'Mode raw' its own",
          st(eid(SLAVE1, "operating_mode")) == "manual_heat_recovery" and st(eid(SLAVE1, "operating_mode_raw")) == "surveillance"
          and st(eid(SLAVE1, "operating_mode_select", "select")) == "manual_heat_recovery",
          (st(eid(SLAVE1, "operating_mode")), st(eid(SLAVE1, "operating_mode_raw"))))
    check("device role and zone of the Slave", st(eid(SLAVE1, "device_role")) == "slave_equal_master"
          and st(eid(SLAVE1, "zone_index")) == "0", (st(eid(SLAVE1, "device_role")), st(eid(SLAVE1, "zone_index"))))
    await cloud.unit(SLAVE1, set={"operatingMode": "Smart"})

    # ---- measured values: plausibility
    t_id, h_id = eid(MASTER, "temperature"), eid(MASTER, "humidity")
    await cloud.unit(MASTER, set={"temperature": 22, "humidity": 58})
    await refresh([t_id])
    check("temperature and humidity sensors: 22 °C, 58 %", st(t_id) == "22" and st(h_id) == "58", (st(t_id), st(h_id)))
    await cloud.unit(MASTER, set={"humidity": 6})
    await refresh([t_id])
    check("a single reading of 6 % is held back, the last plausible value stays (58 %)",
          st(h_id) == "58" and attr(climate, "current_humidity") == 58, (st(h_id), attr(climate, "current_humidity")))
    await cloud.unit(MASTER, set={"humidity": 118})
    await refresh([t_id])
    check("118 % (saturated sensor) shows as 100 %", st(h_id) == "100", st(h_id))
    await cloud.unit(MASTER, set={"humidity": 55, "temperature": 21})
    await refresh([t_id])

    # ---- filter reset: sent to the unit and its zone Master, checked against the status
    info = hass.states.get(button).attributes
    check("reset button of the Master says that the cloud reset clears its counter",
          info.get("cloud_reset_clears_counter") is True and info.get("device_role") == "Master", dict(info))
    await cloud.clear()
    await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
    await asyncio.sleep(1.5)
    check("filter already good: nothing is sent", not await cloud.log("reset-filter"), await cloud.log("reset-filter"))
    await cloud.unit(MASTER, set={"filtersStatus": "Medium"})
    await refresh([t_id])
    await cloud.clear()
    await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
    call = await until(lambda: cloud.log("reset-filter"), 10)
    check("filter medium (yellow): reset call for the unit", call and [c["serial"] for c in call] == [MASTER], call)
    await until(lambda: st(eid(MASTER, "filter_status")) == "good", 30)
    done = await until(lambda: hass.states.get(button).attributes.get("last_reset_result"), 5)
    check("the reset is confirmed against the status of the unit, the filter reads good",
          done == "confirmed" and st(eid(MASTER, "filter_status")) == "good", (done, st(eid(MASTER, "filter_status"))))
    s_button = eid(SLAVE2, "filter_reset", "button")
    await cloud.unit(SLAVE2, set={"filtersStatus": "Bad"})
    await refresh([eid(SLAVE2, "temperature")])
    info = hass.states.get(s_button).attributes
    check("reset button of a Slave says that its own counter stays, and names the zone Master",
          info.get("cloud_reset_clears_counter") is False and "Flur" in str(info.get("zone_master")), dict(info))
    await cloud.clear()
    await hass.services.async_call("button", "press", {"entity_id": s_button}, blocking=True)
    done = await until(lambda: hass.states.get(s_button).attributes.get("last_reset_result"), 30)
    call = await cloud.log("reset-filter")
    check("reset on a Slave: sent to the Slave and to its zone Master; result unconfirmed, its filter stays bad",
          [c["serial"] for c in call] == [SLAVE2, MASTER] and done == "unconfirmed"
          and st(eid(SLAVE2, "filter_status")) == "bad", (call, done))

    # ---- options: maintenance acknowledgement for Slaves
    async def options_page(page, values):
        """Open the options (a menu of three pages since NeuraCell-X is part of them) and save one page."""
        res = await hass.config_entries.options.async_init(entry.entry_id)
        if res["type"] == "menu":
            res = await hass.config_entries.options.async_configure(res["flow_id"], {"next_step_id": page})
        if values is None:
            return res
        res = await hass.config_entries.options.async_configure(res["flow_id"], values)
        await hass.async_block_till_done()
        return res

    res = await hass.config_entries.options.async_init(entry.entry_id)
    check("options dialog opens with a menu: general, radon, dewpoint",
          res["type"] == "menu" and list(res["menu_options"]) == ["general", "radon", "dewpoint"], res)
    res = await hass.config_entries.options.async_configure(res["flow_id"], {"next_step_id": "general"})
    check("page 'general' has the twelve options of the bridge",
          res["type"] == "form" and len(res["data_schema"].schema) == 12, res.get("type"))
    res = await hass.config_entries.options.async_configure(res["flow_id"], {
        "poll_interval": 30, "availability_failure_threshold": 3, "command_coalesce_ms": 800, "average_window_min": 30,
        "humidity_avg_hysteresis": 0, "temperature_avg_hysteresis": 0, "temperature_avg_window_min": 0,
        "slave_filter_soft_reset": True, "filter_ack_ttl_days": 90, "average_window_devices": "Gäste Bad=10"})
    await hass.async_block_till_done()
    check("options saved, the integration reloaded", res["type"] == "create_entry" and entry.state.value == "loaded"
          and entry.options.get("slave_filter_soft_reset") is True, (res.get("type"), entry.state))
    now_ents = er.async_entries_for_config_entry(ent, entry.entry_id)
    check("with the option on every unit has the two acknowledgement sensors",
          len([e for e in now_ents if e.unique_id.endswith(("_filter_ack_since", "_filter_ack_until"))]) == 10, len(now_ents))
    await cloud.clear()
    await hass.services.async_call("button", "press", {"entity_id": s_button}, blocking=True)
    done = await until(lambda: hass.states.get(s_button).attributes.get("last_reset_result"), 30)
    since = next((e.entity_id for e in now_ents if e.unique_id == SLAVE2 + "_filter_ack_since"), None)
    check("reset on a Slave with the option on: acknowledged - filter status good, raw filter status bad, acknowledged since set",
          done == "acknowledged" and st(eid(SLAVE2, "filter_status")) == "good" and st(eid(SLAVE2, "filter_status_raw")) == "bad"
          and st(since) not in (None, "unknown", "unavailable"),
          (done, st(eid(SLAVE2, "filter_status")), st(eid(SLAVE2, "filter_status_raw")), st(since)))
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    check("the acknowledgement holds over a reload", st(eid(SLAVE2, "filter_status")) == "good"
          and st(eid(SLAVE2, "filter_status_raw")) == "bad", (st(eid(SLAVE2, "filter_status")), st(eid(SLAVE2, "filter_status_raw"))))
    res = await options_page("general", {
        "poll_interval": 30, "availability_failure_threshold": 3, "command_coalesce_ms": 800, "average_window_min": 30,
        "humidity_avg_hysteresis": 0, "temperature_avg_hysteresis": 0, "temperature_avg_window_min": 0,
        "slave_filter_soft_reset": False, "filter_ack_ttl_days": 90})
    now_ents = er.async_entries_for_config_entry(ent, entry.entry_id)
    check("option off again: the two sensors are gone, the Slave's filter reads bad again",
          not [e for e in now_ents if e.unique_id.endswith(("_filter_ack_since", "_filter_ack_until"))]
          and st(eid(SLAVE2, "filter_status")) == "bad", st(eid(SLAVE2, "filter_status")))
    await cloud.unit(SLAVE2, set={"filtersStatus": "Good"})
    await cloud.unit(MASTER, set={"operatingMode": "Smart", "fanSpeed": "Medium", "humidityLevel": "Normal"})
    await refresh([climate])

    await neuracell(hass, cloud, ent, st, attr, refresh, entry, options_page, eid, climate)
    ents[:] = er.async_entries_for_config_entry(ent, entry.entry_id)

    # ---- averaged values appear once two minutes of readings are in
    avg = await until(lambda: st(eid(MASTER, "humidity_avg")) not in (None, "unknown", "unavailable"), 200, 2)
    check("Humidity (averaged) and Temperature (averaged) show a value after about two minutes (%s %%, %s °C)"
          % (st(eid(MASTER, "humidity_avg")), st(eid(MASTER, "temperature_avg"))),
          avg and 50 <= float(st(eid(MASTER, "humidity_avg"))) <= 100 and 20 <= float(st(eid(MASTER, "temperature_avg"))) <= 23,
          (st(eid(MASTER, "humidity_avg")), st(eid(MASTER, "temperature_avg"))))
    check("nothing but the documented calls reached the cloud (1.6 part)", not await cloud.log("UNEXPECTED"), await cloud.log("UNEXPECTED"))


async def neuracell(hass, cloud, ent, st, attr, refresh, entry, options_page, eid, climate):
    """NeuraCell-X: radon protection and dew-point control, fed by Home Assistant entities."""
    RADON, BLOCK = "sensor.radon_test", "binary_sensor.tps_block_test"
    MODE = {"Smart": "0", "Night": "3", "Intake": "8", "Off": "11"}

    def nc(key, domain="binary_sensor"):
        return next((e.entity_id for e in er.async_entries_for_config_entry(ent, entry.entry_id)
                     if e.unique_id == "%s_neuracell_%s" % (entry.entry_id, key) and e.domain == domain), None)

    async def cloud_modes():
        return {s[-2:]: u["operatingMode"] for s, u in (await cloud.req("GET", "/_ctl/state")).items()}

    async def sent():
        return {c["serial"][-2:]: str(c["body"]["operatingMode"]) for c in await cloud.log("change-mode") if c["ok"]}

    hass.states.async_set(RADON, "120", {"unit_of_measurement": "Bq/m³"})
    hass.states.async_set(BLOCK, "off")
    check("without a source NeuraCell-X adds no entities", nc("radon_protection") is None and nc("dewpoint_block") is None)
    res = await options_page("radon", None)
    check("page 'radon' has eleven options", res["type"] == "form" and len(res["data_schema"].schema) == 11,
          len(res["data_schema"].schema) if res.get("data_schema") else res)
    res = await hass.config_entries.options.async_configure(res["flow_id"], {
        "neuracell_enabled": True, "radon_source": "signal", "radon_entities": [RADON], "radon_threshold": 300,
        "radon_hysteresis": 50, "radon_protection_fan": "low", "radon_meter_timeout": 30})
    await hass.async_block_till_done()
    res = await options_page("dewpoint", None)
    check("page 'dewpoint' has eighteen options", res["type"] == "form" and len(res["data_schema"].schema) == 18,
          len(res["data_schema"].schema) if res.get("data_schema") else res)
    res = await hass.config_entries.options.async_configure(res["flow_id"], {
        "dewpoint_enabled": True, "dewpoint_source": "signal", "dewpoint_block_entity": BLOCK, "dewpoint_margin": 1.0,
        "dewpoint_hysteresis": 0.5, "dewpoint_lost_action": "keep", "dewpoint_signal_timeout": 0,
        "dewpoint_controller_timeout": 30, "dewpoint_fallback_timeout": 30, "dewpoint_fallback_fan": "low"})
    await hass.async_block_till_done()
    check("radon sensor and block entity saved, the first page kept (%s)" % entry.options.get("poll_interval"),
          entry.options.get("radon_entities") == [RADON] and entry.options.get("dewpoint_block_entity") == BLOCK
          and entry.options.get("poll_interval") == 30 and entry.state.value == "loaded", dict(entry.options))
    await asyncio.sleep(1)
    check("NeuraCell-X entities: radon protection off, radon level 120, meter connected, block off",
          st(nc("radon_protection")) == "off" and st(nc("radon_level", "sensor")) in ("120", "120.0")
          and st(nc("radon_meter_connected")) == "on" and st(nc("dewpoint_block")) == "off",
          (st(nc("radon_protection")), st(nc("radon_level", "sensor")), st(nc("radon_meter_connected")), st(nc("dewpoint_block"))))
    await cloud.unit(MASTER, set={"operatingMode": "Night", "fanSpeed": "Medium"})
    await refresh([climate])

    # ---- radon above the threshold
    await cloud.clear()
    hass.states.async_set(RADON, "350", {"unit_of_measurement": "Bq/m³"})
    got = await until(lambda: cloud_modes_all(cloud_modes, "Intake"), 20)
    calls = await cloud.log("change-mode")
    check("radon 350 Bq/m³: every unit is set to Intake at fan step low",
          got and len(calls) == 5 and all(str(c["body"]["operatingMode"]) == "8" and c["body"]["fanSpeed"] == 0 for c in calls),
          (await cloud_modes(), calls[:2]))
    check("radon protection shows on, with the value that set it off",
          st(nc("radon_protection")) == "on" and attr(nc("radon_protection"), "radon_value_alarm") is True
          and st(nc("radon_level", "sensor")) in ("350", "350.0"), st(nc("radon_protection")))
    await cloud.clear()
    await hass.services.async_call("climate", "set_preset_mode", {"entity_id": climate, "preset_mode": "Auto"}, blocking=True)
    check("a mode set while protection holds the unit is not sent now", not await cloud.log("change-mode"),
          await cloud.log("change-mode"))
    hass.states.async_set(RADON, "270", {"unit_of_measurement": "Bq/m³"})
    await asyncio.sleep(1.5)
    check("270 Bq/m³ (within the hysteresis of 50): protection stays", st(nc("radon_protection")) == "on"
          and not await cloud.log("change-mode"), st(nc("radon_protection")))
    hass.states.async_set(RADON, "200", {"unit_of_measurement": "Bq/m³"})
    got = await until(lambda: cloud_modes_all(cloud_modes, None, without="Intake"), 20)
    modes = await cloud_modes()
    check("200 Bq/m³: every unit gets its mode back - the one changed meanwhile gets the mode set then (Auto)",
          got and modes["02"] == "Auto" and all(m == "Smart" for k, m in modes.items() if k != "02")
          and st(nc("radon_protection")) == "off", modes)

    # ---- dew-point block, and radon has priority
    await cloud.clear()
    hass.states.async_set(BLOCK, "on")
    got = await until(lambda: cloud_modes_all(cloud_modes, "Off"), 20)
    check("block entity on: every unit is switched off, 'ventilation blocked' shows on",
          got and st(nc("dewpoint_block")) == "on", await cloud_modes())
    hass.states.async_set(RADON, "400", {"unit_of_measurement": "Bq/m³"})
    got = await until(lambda: cloud_modes_all(cloud_modes, "Intake"), 20)
    check("radon alarm during the block: radon has priority, every unit runs Intake", got, await cloud_modes())
    hass.states.async_set(RADON, "100", {"unit_of_measurement": "Bq/m³"})
    got = await until(lambda: cloud_modes_all(cloud_modes, "Off"), 20)
    check("radon back to safe: the block holds the units off again", got, await cloud_modes())

    # ---- a reload while protection is active keeps what the units return to
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await asyncio.sleep(2)
    check("reload during the block: the block is taken up again, the units stay off",
          st(nc("dewpoint_block")) == "on" and await cloud_modes_all(cloud_modes, "Off"), (st(nc("dewpoint_block")), await cloud_modes()))
    hass.states.async_set(BLOCK, "off")
    t0 = time.monotonic()
    got = await until(lambda: cloud_modes_all(cloud_modes, None, without="Off"), 150, 2)
    modes = await cloud_modes()
    check("block off after the reload: the units get the modes from before the block back (%.0f s, start-up grace 90 s)"
          % (time.monotonic() - t0), got and modes["02"] == "Auto" and all(m == "Smart" for k, m in modes.items() if k != "02"),
          modes)

    # ---- the source becomes unavailable
    hass.states.async_set(RADON, "unavailable")
    lost = await until(lambda: st(nc("radon_meter_connected")) == "off", 10)
    check("radon sensor unavailable: 'radon meter connected' shows off, protection state kept",
          lost and st(nc("radon_protection")) == "off", st(nc("radon_meter_connected")))
    hass.states.async_set(RADON, "90", {"unit_of_measurement": "Bq/m³"})
    back = await until(lambda: st(nc("radon_meter_connected")) == "on", 10)
    check("radon sensor back: connected again", back, st(nc("radon_meter_connected")))
    check("nothing but the documented calls reached the cloud (NeuraCell-X)", not await cloud.log("UNEXPECTED"),
          await cloud.log("UNEXPECTED"))

    # ---- the dew-point controller read from the cloud (source "device")
    TPS = "A0B1C2D3E404"
    await options_page("radon", {"neuracell_enabled": True, "radon_source": "signal", "radon_threshold": 300,
                                 "radon_hysteresis": 50, "radon_protection_fan": "low", "radon_meter_timeout": 30})
    await options_page("dewpoint", {
        "dewpoint_enabled": True, "dewpoint_source": "device", "dewpoint_device_serial": TPS,
        "dewpoint_device_block_modes": "Off", "dewpoint_margin": 1.0,
        "dewpoint_hysteresis": 0.5, "dewpoint_lost_action": "keep", "dewpoint_signal_timeout": 0,
        "dewpoint_controller_timeout": 30, "dewpoint_fallback_timeout": 30, "dewpoint_fallback_fan": "low"})
    hub = hass.data["ambientika"][entry.entry_id]
    check("source 'device': that device is only read, four ventilation units remain",
          sorted(d.serial_number[-2:] for d in hub.devices) == ["01", "02", "03", "05"] and st(nc("dewpoint_block")) == "off",
          [d.serial_number for d in hub.devices])
    await cloud.clear()
    await cloud.unit(TPS, set={"operatingMode": "Off"})
    got = await until(lambda: others_all(cloud_modes, TPS, "Off"), 50)
    check("the controller in the cloud reports Off: the four units are switched off, 'controller connected' shows on",
          got and st(nc("dewpoint_block")) == "on" and st(nc("dewpoint_controller_connected")) == "on"
          and TPS not in [c["serial"] for c in await cloud.log("change-mode")], await cloud_modes())
    await cloud.unit(TPS, set={"operatingMode": "Smart"})
    got = await until(lambda: others_all(cloud_modes, TPS, None, without="Off"), 50)
    modes = await cloud_modes()
    check("the controller reports Smart again: the units get their modes back",
          got and modes["02"] == "Auto" and st(nc("dewpoint_block")) == "off", modes)

    # ---- sources removed again: the entities go, the units are left alone
    await options_page("radon", {"neuracell_enabled": True, "radon_source": "signal", "radon_threshold": 300,
                                 "radon_hysteresis": 50, "radon_protection_fan": "low", "radon_meter_timeout": 30})
    await options_page("dewpoint", {
        "dewpoint_enabled": True, "dewpoint_source": "signal", "dewpoint_margin": 1.0,
        "dewpoint_hysteresis": 0.5, "dewpoint_lost_action": "keep", "dewpoint_signal_timeout": 0,
        "dewpoint_controller_timeout": 30, "dewpoint_fallback_timeout": 30, "dewpoint_fallback_fan": "low"})
    check("sources removed in the options: the NeuraCell-X entities are gone",
          nc("radon_protection") is None and nc("dewpoint_block") is None and nc("radon_level", "sensor") is None
          and entry.options.get("radon_entities") == [] and entry.options.get("dewpoint_block_entity") == "",
          dict(entry.options))
    await cloud.unit(MASTER, set={"operatingMode": "Smart", "fanSpeed": "Medium", "humidityLevel": "Normal"})
    await refresh([climate])


async def others_all(cloud_modes, serial, mode, without=None):
    """Like cloud_modes_all, for every unit but one."""
    modes = {k: m for k, m in (await cloud_modes()).items() if k != serial[-2:]}
    if without is not None:
        return all(m != without for m in modes.values())
    return all(m == mode for m in modes.values())


async def cloud_modes_all(cloud_modes, mode, without=None):
    """True when every unit has the mode (or, with without, no unit has that mode)."""
    modes = await cloud_modes()
    if without is not None:
        return all(m != without for m in modes.values())
    return all(m == mode for m in modes.values())


async def async_setup(hass: HomeAssistant, config) -> bool:
    async def started(_event):
        hass.async_create_task(run(hass))

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, started)
    return True
