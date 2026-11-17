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
            check("two units with the same name: every unit has all its entities (30 in total)",
                  len(ents) == 30, counts)
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
        early = {i: st(i) for i in reading_ids if st(i) in (None, "unavailable", "unknown")}
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
                  % len(flur_reading), own == 1 and len(flur_reading) == 5, asked)
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
        await cloud.unit(SLAVE2, fault="404")
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
            gone = await until(lambda: all(st(i) == "unavailable" for i in k_reading), 80)
            check("  ... and that unit becomes unavailable after the time limit (%.0f s)" % (time.monotonic() - t0),
                  gone, {i: st(i) for i in k_reading})
            await cloud.unit(SLAVE2, fault=None)
            back = await until(lambda: all(st(i) not in (None, "unavailable") for i in k_reading), 80)
            check("  ... and is available again once it answers", back, {i: st(i) for i in k_reading})
            await cloud.outage("hang")
            t0 = time.monotonic()
            gone = await until(lambda: all(st(i) == "unavailable" for i in reading_ids), 100)
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


async def async_setup(hass: HomeAssistant, config) -> bool:
    async def started(_event):
        hass.async_create_task(run(hass))

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, started)
    return True
