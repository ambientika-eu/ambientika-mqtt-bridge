"""Test driver that runs inside a real Home Assistant.

Sets up the MQTT integration against the test broker, starts the real bridge
against the cloud stand-in, and checks what Home Assistant makes of it: devices,
entities, states, availability, commands. Results go to SIM_HA_RESULT.
"""
import asyncio
import json
import logging
import os
import signal
import time

import aiohttp
import voluptuous as vol

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

DOMAIN = "simtest"
CONFIG_SCHEMA = vol.Schema({DOMAIN: object}, extra=vol.ALLOW_EXTRA)
_LOGGER = logging.getLogger(__name__)

BROKER_PORT = int(os.environ["SIM_BROKER_PORT"])
CLOUD = os.environ["SIM_CLOUD_URL"]
BRIDGE = os.environ["SIM_BRIDGE"]
PYTHON = os.environ["SIM_PYTHON"]
RESULT = os.environ["SIM_HA_RESULT"]
WORK = os.environ["SIM_WORK"]
POLL = 2
MASTER, SLAVE, SLAVE2, MASTER2 = "A0B1C2D3E402", "A0B1C2D3E401", "A0B1C2D3E403", "A0B1C2D3E404"
NAMES = {MASTER: "Flur", SLAVE: "Wohnzimmer", SLAVE2: "Kinderzimmer", "A0B1C2D3E405": "Gäste-Bad",
         MASTER2: "Schlafzimmer Eltern"}

RESULTS = []


def check(name, cond, detail=None):
    RESULTS.append({"name": name, "ok": bool(cond), "detail": None if cond else str(detail)[:700]})
    _LOGGER.info("%s %s%s", "PASS" if cond else "FAIL", name, "" if cond else "  <<< %s" % str(detail)[:700])
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


class Bridge:
    def __init__(self):
        self.proc = None
        self.n = 0

    async def start(self):
        self.n += 1
        d = os.path.join(WORK, "bridge%d" % self.n)
        os.makedirs(d, exist_ok=True)
        env = dict(os.environ)
        env.update({
            "AMBIENTIKA_USERNAME": "kunde@example.com", "AMBIENTIKA_PASSWORD": "geheim#1 äöü",
            "AMBIENTIKA_HOST": CLOUD, "MQTT_HOST": "127.0.0.1", "MQTT_PORT": str(BROKER_PORT),
            "POLL_INTERVAL": str(POLL), "NEURACELL_STATE_FILE": os.path.join(d, "nc.json"),
            "PENDING_RESET_FILE": os.path.join(d, "pr.json"), "FILTER_ACK_PATH": os.path.join(d, "ack.json"),
            "CONFIG_YAML": os.path.join(d, "none.yaml"), "BRIDGE_VERSION": "sim",
        })
        self.log = await asyncio.get_running_loop().run_in_executor(None, open, os.path.join(d, "bridge.log"), "wb")
        self.proc = await asyncio.create_subprocess_exec(PYTHON, BRIDGE, env=env, cwd=d,
                                                         stdout=self.log, stderr=asyncio.subprocess.STDOUT)

    async def stop(self, sig=signal.SIGTERM):
        if self.proc and self.proc.returncode is None:
            self.proc.send_signal(sig)
            try:
                await asyncio.wait_for(self.proc.wait(), 20)
            except asyncio.TimeoutError:
                self.proc.kill()
        return self.proc.returncode if self.proc else None


async def setup_mqtt(hass):
    res = await hass.config_entries.flow.async_init("mqtt", context={"source": "user"})
    for _ in range(5):
        if res["type"] == "create_entry":
            return True
        if res["type"] == "menu":
            res = await hass.config_entries.flow.async_configure(res["flow_id"], {"next_step_id": "broker"})
        elif res["type"] == "form":
            res = await hass.config_entries.flow.async_configure(
                res["flow_id"], {"broker": "127.0.0.1", "port": BROKER_PORT})
        else:
            break
    _LOGGER.error("MQTT flow ended with %s", res)
    return False


async def run(hass: HomeAssistant):
    cloud, bridge = Cloud(), Bridge()
    ent, dev = er.async_get(hass), dr.async_get(hass)

    def st(entity_id):
        s = hass.states.get(entity_id)
        return s.state if s else None

    def bridge_devices():
        return [d for d in dev.devices.values() if any(i[0] == "mqtt" for i in d.identifiers)]

    try:
        check("MQTT integration set up through its normal config flow", await setup_mqtt(hass))
        await bridge.start()

        # ---- discovery
        ok = await until(lambda: all(st("sensor.%s_temperature" % n) == "21" for n in
                                     ("flur", "wohnzimmer", "kinderzimmer", "gaste_bad", "schlafzimmer_eltern")), 60)
        check("five units appear with their temperature", ok, [s.entity_id for s in hass.states.async_all()][:40])
        units = {d.name: d for d in bridge_devices()}
        check("devices carry the names of the app", all(n in units for n in NAMES.values()), sorted(units))
        check("plus the devices 'Ambientika MQTT Bridge' and 'NeuraCell-X'",
              "Ambientika MQTT Bridge" in units and "NeuraCell-X" in units, sorted(units))
        flur = units.get("Flur")
        ents = [e for e in ent.entities.values() if flur and e.device_id == flur.id]
        by_domain = {}
        for e in ents:
            by_domain.setdefault(e.domain, []).append(e)
        check("unit 'Flur': 28 sensors, 2 binary sensors, 4 selects, 1 button",
              {k: len(v) for k, v in by_domain.items()} == {"sensor": 28, "binary_sensor": 2, "select": 4, "button": 1},
              {k: len(v) for k, v in by_domain.items()})
        disabled = sorted(e.original_name for e in ents if e.disabled_by)
        check("disabled by default: Humidity raw and Filter Reset Status",
              disabled == ["Filter Reset Status", "Humidity raw"], disabled)
        diag = sorted(e.original_name for e in ents if e.entity_category and e.entity_category.value == "diagnostic")
        check("diagnostic: Failed Polls, Filter Reset Status, Humidity raw, Last Status Packet",
              diag == ["Failed Polls", "Filter Reset Status", "Humidity raw", "Last Status Packet"], diag)
        check("manufacturer and model on the device", flur and flur.manufacturer and flur.model, (flur.manufacturer, flur.model) if flur else None)
        states = {e.entity_id: st(e.entity_id) for e in ents if not e.disabled_by}
        bad = {k: v for k, v in states.items() if v in (None, "unavailable")}
        check("every enabled entity of the unit has a state and is available", not bad, bad)
        unknown = {k: v for k, v in states.items() if v == "unknown" and not k.startswith("button.")
                   and "averaged" not in k and "_avg" not in k}
        check("no entity is stuck at unknown (averages need a few polls)", not unknown, unknown)
        check("values: mode Smart, fan Medium, humidity 55, air quality Good",
              st("select.flur_mode") == "Smart" and st("select.flur_fan_speed") == "Medium"
              and st("sensor.flur_humidity") == "55" and st("sensor.flur_air_quality") == "Good",
              {k: v for k, v in states.items() if "mode" in k or "fan" in k})
        check("bridge shown connected", st("binary_sensor.ambientika_mqtt_bridge_ambientika_bridge") == "on",
              [s.entity_id for s in hass.states.async_all("binary_sensor")])
        s = hass.states.get("sensor.flur_temperature")
        check("temperature with unit and device class", s and s.attributes.get("unit_of_measurement") == "°C"
              and s.attributes.get("device_class") == "temperature", s.attributes if s else None)
        s = hass.states.get("sensor.flur_failed_polls")
        check("Failed Polls 0 with its attributes", s and s.state == "0" and "last_outage_start" in s.attributes,
              s.attributes if s else None)

        # ---- values follow
        await cloud.unit(MASTER, set={"temperature": 23.5, "humidity": 61, "airQuality": "Poor",
                                      "humidityAlarm": True, "nightAlarm": True, "fanSpeed": "Night"})
        check("temperature follows the cloud", await until(lambda: st("sensor.flur_temperature") == "23.5", 15),
              st("sensor.flur_temperature"))
        check("alarms as binary sensors", await until(lambda: st("binary_sensor.flur_humidity_alarm") == "on"
                                                      and st("binary_sensor.flur_night_mode") == "on", 10),
              (st("binary_sensor.flur_humidity_alarm"), st("binary_sensor.flur_night_mode")))
        check("fan step Night of the unit is a valid option of the select (no 'invalid option')",
              st("select.flur_fan_speed") == "Night", st("select.flur_fan_speed"))
        await cloud.unit(SLAVE2, set={"humidity": 118})
        check("saturated sensor: Home Assistant shows 100 %",
              await until(lambda: st("sensor.kinderzimmer_humidity") == "100", 15), st("sensor.kinderzimmer_humidity"))
        await cloud.unit(MASTER, set={"fanSpeed": "Medium", "nightAlarm": False, "humidityAlarm": False})
        await cloud.unit(SLAVE2, set={"humidity": 55})

        # ---- commands
        await cloud.clear()
        await hass.services.async_call("select", "select_option",
                                       {"entity_id": "select.flur_mode", "option": "Night"}, blocking=True)
        call = await until(lambda: cloud.log("change-mode"), 10)
        check("select 'Mode' -> Night reaches the cloud as mode 3",
              call and call[0]["ok"] and str(call[0]["body"]["operatingMode"]) == "3", call)
        check("Home Assistant shows Night", await until(lambda: st("select.flur_mode") == "Night", 10))
        await cloud.clear()
        await hass.services.async_call("select", "select_option",
                                       {"entity_id": "select.schlafzimmer_eltern_fan_speed", "option": "High"},
                                       blocking=True)
        call = await until(lambda: cloud.log("change-mode"), 10)
        check("select 'Fan Speed' -> High", call and call[0]["body"]["fanSpeed"] == 2, call)
        await cloud.unit(MASTER, set={"filtersStatus": "Bad"})
        await until(lambda: st("sensor.flur_filter_status") == "Bad", 10)
        await cloud.clear()
        btn = next((e.entity_id for e in by_domain.get("button", [])), None)
        await hass.services.async_call("button", "press", {"entity_id": btn}, blocking=True)
        call = await until(lambda: cloud.log("reset-filter"), 10)
        check("button 'Filter reset' -> reset call for the unit", call and call[0]["serial"] == MASTER, (btn, call))
        check("filter Good again after the confirmed reset", await until(lambda: st("sensor.flur_filter_status") == "Good", 40),
              st("sensor.flur_filter_status"))

        # ---- a unit drops out
        await cloud.unit(SLAVE2, fault="404")
        check("unit without status packet: its entities become unavailable",
              await until(lambda: st("sensor.kinderzimmer_temperature") == "unavailable", 30), st("sensor.kinderzimmer_temperature"))
        check("its diagnostics stay readable (bound to the bridge)",
              st("sensor.kinderzimmer_failed_polls") not in (None, "unavailable", "unknown")
              and int(st("sensor.kinderzimmer_failed_polls")) >= 3, st("sensor.kinderzimmer_failed_polls"))
        check("the other units stay available", st("sensor.flur_temperature") == "23.5")
        await cloud.unit(SLAVE2, fault=None)
        check("unit back: entities available again", await until(lambda: st("sensor.kinderzimmer_temperature") == "21", 20))

        # ---- the bridge stops
        rc = await bridge.stop()
        check("bridge stops cleanly", rc == 0, rc)
        check("Home Assistant shows the bridge disconnected",
              await until(lambda: st("binary_sensor.ambientika_mqtt_bridge_ambientika_bridge") == "off", 10))
        await asyncio.sleep(3)
        unit_state = st("sensor.flur_temperature")
        RESULTS.append({"name": "OBSERVATION unit entities while the bridge is stopped", "ok": True,
                        "detail": "sensor.flur_temperature = %s, select.flur_mode = %s, sensor.flur_failed_polls = %s"
                                  % (unit_state, st("select.flur_mode"), st("sensor.flur_failed_polls"))})
        _LOGGER.info("OBSERVATION while the bridge is stopped: temperature=%s mode=%s failed_polls=%s",
                     unit_state, st("select.flur_mode"), st("sensor.flur_failed_polls"))
        check("diagnostics and NeuraCell-X entities become unavailable with the bridge",
              st("sensor.flur_failed_polls") == "unavailable", st("sensor.flur_failed_polls"))
        await bridge.start()
        check("bridge starts: connected again", await until(
            lambda: st("binary_sensor.ambientika_mqtt_bridge_ambientika_bridge") == "on", 40))
        await asyncio.sleep(2 * POLL)
        n_before = len(bridge_devices())
        await bridge.stop(signal.SIGKILL)
        check("killed bridge (Last Will): shown disconnected",
              await until(lambda: st("binary_sensor.ambientika_mqtt_bridge_ambientika_bridge") == "off", 15))
        await bridge.start()
        check("and connected again after the restart", await until(
            lambda: st("binary_sensor.ambientika_mqtt_bridge_ambientika_bridge") == "on", 40))
        await asyncio.sleep(2 * POLL)
        check("no device or entity was duplicated over the restarts",
              len(bridge_devices()) == n_before == 7
              and not [e.entity_id for e in ent.entities.values() if e.entity_id.endswith("_2")],
              (len(bridge_devices()), [e.entity_id for e in ent.entities.values() if e.entity_id.endswith("_2")]))
    except Exception as exc:  # noqa: BLE001
        _LOGGER.exception("test driver failed")
        check("test driver ran through", False, repr(exc))
    finally:
        await bridge.stop()
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
