"""Test driver inside a real Home Assistant for the integration 'Ambientika for Home Assistant'.

The integration is the released code; only its DEFAULT_HOST points at the local
stand-in for the Ambientika cloud. Results go to SIM_HA_RESULT.
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

DOMAIN = "simtest_hacs"
CONFIG_SCHEMA = vol.Schema({DOMAIN: object}, extra=vol.ALLOW_EXTRA)
_LOGGER = logging.getLogger(__name__)

CLOUD = os.environ["SIM_CLOUD_URL"]
RESULT = os.environ["SIM_HA_RESULT"]
USER, PASSWORD = "kunde@example.com", "geheim#1 äöü"
MASTER, SLAVE2 = "A0B1C2D3E402", "A0B1C2D3E403"
NAMES = ["Flur", "Wohnzimmer", "Kinderzimmer", "Gäste-Bad", "Schlafzimmer Eltern"]
RESULTS = []


def check(name, cond, detail=None):
    RESULTS.append({"name": name, "ok": bool(cond), "detail": None if cond else str(detail)[:900]})
    _LOGGER.info("%s %s%s", "PASS" if cond else "FAIL", name, "" if cond else "  <<< %s" % str(detail)[:900])
    return bool(cond)


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

    if os.environ.get("SIM_SCENARIO") == "same-name":
        # two units carry the same name in the app
        try:
            await cloud.req("POST", "/_ctl/unit", {"serial": "A0B1C2D3E401", "name": "Flur"})
            res = await flow.async_init("ambientika", context={"source": "user"})
            res = await flow.async_configure(res["flow_id"], {"username": USER, "password": PASSWORD})
            await hass.async_block_till_done()
            entry = hass.config_entries.async_entries("ambientika")[0]
            ents = [e for e in ent.entities.values() if e.config_entry_id == entry.entry_id]
            devs = [d for d in dev.devices.values() if entry.entry_id in d.config_entries]
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
        res = await flow.async_configure(res["flow_id"], {"username": USER, "password": PASSWORD})
        check("right password: entry created, titled with the user name",
              res["type"] == "create_entry" and res["title"] == USER, {k: res.get(k) for k in ("type", "title", "errors")})
        await hass.async_block_till_done()
        entries = hass.config_entries.async_entries("ambientika")
        entry = entries[0] if entries else None
        check("entry loaded", entry is not None and entry.state.value == "loaded", entry.state if entry else None)
        ents = [e for e in ent.entities.values() if entry and e.config_entry_id == entry.entry_id]
        devs = [d for d in dev.devices.values() if entry and entry.entry_id in d.config_entries]
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
        await refresh(all_ids)
        states = {i: st(i) for i in all_ids}
        bad = {k: v for k, v in states.items() if v in (None, "unavailable")}
        check("every entity has a state and is available", not bad, bad)
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
        RESULTS.append({"name": "OBSERVATION binary sensors of a unit without status packet", "ok": True,
                        "detail": str({i: v for i, v in k_states.items() if i.startswith("binary_sensor.")})})
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

        # ---- reload and unload
        n_before = len([e for e in ent.entities.values() if e.config_entry_id == entry.entry_id])
        check("reload of the entry", await hass.config_entries.async_reload(entry.entry_id) and entry.state.value == "loaded",
              entry.state)
        await hass.async_block_till_done()
        n_after = len([e for e in ent.entities.values() if e.config_entry_id == entry.entry_id])
        check("no entity added or lost over the reload (%d)" % n_after, n_after == n_before
              and not [e.entity_id for e in ent.entities.values() if e.entity_id.endswith("_2")], (n_before, n_after))
        res = await flow.async_init("ambientika", context={"source": "user"})
        res = await flow.async_configure(res["flow_id"], {"username": USER, "password": PASSWORD})
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
