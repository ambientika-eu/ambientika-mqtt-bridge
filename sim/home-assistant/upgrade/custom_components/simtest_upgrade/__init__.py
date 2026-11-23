"""Test driver inside a real Home Assistant for updates of 'Ambientika for Home Assistant'.

Every run of Home Assistant is one phase of a scenario (SIM_SCENARIO, SIM_PHASE):
one version of the integration sets things up, Home Assistant stops, another
version is put in its place and Home Assistant starts again with the same
configuration folder. upgrade_test.py drives the phases and exchanges the
integration; this driver looks at the registries, the states and the calls
that reach the stand-in for the Ambientika cloud. Results go to SIM_HA_RESULT.
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
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er

DOMAIN = "simtest_upgrade"
CONFIG_SCHEMA = vol.Schema({DOMAIN: object}, extra=vol.ALLOW_EXTRA)
_LOGGER = logging.getLogger(__name__)

CLOUD = os.environ["SIM_CLOUD_URL"]
RESULT = os.environ["SIM_HA_RESULT"]
WORK = os.environ["SIM_WORK"]
SCENARIO = os.environ.get("SIM_SCENARIO", "")
PHASE = os.environ.get("SIM_PHASE", "")
USER, PASSWORD = "kunde@example.com", "geheim#1 äöü"
FLUR, WOHNEN, KIND, BAD, SCHLAFEN = ("A0B1C2D3E402", "A0B1C2D3E401", "A0B1C2D3E403",
                                    "A0B1C2D3E405", "A0B1C2D3E404")
KEYS = {"sensor": ["air_quality", "filter_status"], "binary_sensor": ["humidity_alarm", "night_alarm"],
        "button": ["filter_reset"], "climate": ["climate"]}
# Entity IDs of a new installation with the five units of the stand-in. Up to
# 1.2 the button had no name of its own and the climate entity carried the name
# of the unit twice; which entity IDs came out of that depends on the version
# of Home Assistant (button.flur or button.flur_none, climate.flur_flur or
# climate.flur).
SLUGS = ["flur", "wohnzimmer", "kinderzimmer", "gaste_bad", "schlafzimmer_eltern"]
SENSOR_IDS = ([f"sensor.{s}_{k}" for s in SLUGS for k in KEYS["sensor"]]
              + [f"binary_sensor.{s}_{k}" for s in SLUGS for k in KEYS["binary_sensor"]])
OLD_ENTITY_IDS = [sorted([climate.format(s=s) for s in SLUGS] + [button.format(s=s) for s in SLUGS] + SENSOR_IDS)
                  for climate in ("climate.{s}_{s}", "climate.{s}") for button in ("button.{s}", "button.{s}_none")]
NEW_ENTITY_IDS = sorted([f"climate.{s}" for s in SLUGS] + [f"button.{s}_filter_reset" for s in SLUGS] + SENSOR_IDS)
RESULTS = []


def check(name, cond, detail=None):
    RESULTS.append({"name": name, "ok": bool(cond), "detail": None if cond else str(detail)[:1500]})
    _LOGGER.info("%s %s%s", "PASS" if cond else "FAIL", name, "" if cond else "  <<< %s" % str(detail)[:1500])
    return bool(cond)


def note(name, detail):
    RESULTS.append({"name": "OBSERVATION " + name, "ok": True, "detail": str(detail)[:1500]})
    _LOGGER.info("NOTE %s: %s", name, str(detail)[:1500])


async def until(fn, timeout=20.0, step=0.25):
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


def key_of(domain, unique_id):
    """Which entity of a unit a unique ID stands for, in the old or the new form."""
    if domain == "climate":
        return "climate"
    for key in KEYS.get(domain, []):
        if unique_id.endswith("_" + key):
            return key
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

    async def unit(self, serial, **kw):
        await self.req("POST", "/_ctl/unit", {"serial": serial, **kw})

    async def outage(self, mode):
        await self.req("POST", "/_ctl/outage", {"mode": mode})

    async def expire(self):
        await self.req("POST", "/_ctl/expire", {})


class Ctx:
    def __init__(self, hass: HomeAssistant):
        self.hass = hass
        self.cloud = Cloud()
        self.ent = er.async_get(hass)
        self.dev = dr.async_get(hass)

    # ---- config entry
    def entries(self):
        return self.hass.config_entries.async_entries("ambientika")

    def entry(self):
        entries = self.entries()
        return entries[0] if entries else None

    def state_of_entry(self):
        entry = self.entry()
        return entry.state.value if entry else None

    async def flow(self, user=USER, password=PASSWORD):
        flow = self.hass.config_entries.flow
        res = await flow.async_init("ambientika", context={"source": "user"})
        if res["type"] == "form":
            res = await flow.async_configure(res["flow_id"], {"username": user, "password": password})
        await self.hass.async_block_till_done()
        return res

    async def set_up(self):
        res = await self.flow()
        ok = res.get("type") == "create_entry" and await until(lambda: self.state_of_entry() == "loaded", 60)
        check("set-up through the config flow, entry loaded", ok,
              ({k: res.get(k) for k in ("type", "reason", "errors")}, self.state_of_entry()))
        return ok

    async def loaded(self, timeout=90):
        return await until(lambda: self.state_of_entry() == "loaded", timeout)

    # ---- registries
    def entities(self):
        """The six entities per unit every version has. What version 1.6 adds is in extras()."""
        found = [e for entry in self.entries() for e in er.async_entries_for_config_entry(self.ent, entry.entry_id)
                 if e.platform == "ambientika" and key_of(e.domain, e.unique_id) is not None]
        return sorted(found, key=lambda e: e.entity_id)

    def extras(self):
        """The entities version 1.6 adds (select entities, further sensors, diagnostics)."""
        found = [e for entry in self.entries() for e in er.async_entries_for_config_entry(self.ent, entry.entry_id)
                 if e.platform == "ambientika" and key_of(e.domain, e.unique_id) is None]
        return sorted(found, key=lambda e: e.entity_id)

    async def reset_idle(self):
        """Wait until no filter reset is under way (since 1.6 a reset is checked for some seconds)."""
        hub = self.hub()
        await until(lambda: not getattr(hub, "_reset_inflight", None) or None, 60)
        await asyncio.sleep(0)

    def since_1_6(self):
        hub = self.hub()
        return hub is not None and hasattr(hub, "view")

    def serial(self, device_id):
        device = self.dev.async_get(device_id) if device_id else None
        if device is None:
            return None
        return next((i[1] for i in device.identifiers if i[0] == "ambientika"), None)

    def devices(self):
        found = {d.id: d for entry in self.entries() for d in dr.async_entries_for_config_entry(self.dev, entry.entry_id)}
        return list(found.values())

    def device(self, serial):
        return next((d for d in self.devices() if self.serial(d.id) == serial), None)

    def of(self, serial, key):
        """The registry entries of one entity of a unit, live ones first."""
        found = [e for e in self.entities() if self.serial(e.device_id) == serial
                 and key_of(e.domain, e.unique_id) == key]
        return sorted(found, key=lambda e: (self.live(e.entity_id) is False, e.entity_id))

    def one(self, serial, key):
        found = self.of(serial, key)
        return found[0].entity_id if found else None

    def snapshot(self):
        return {
            "entities": [
                {"id": e.id, "entity_id": e.entity_id, "domain": e.domain, "unique_id": e.unique_id,
                 "serial": self.serial(e.device_id), "device_id": e.device_id, "name": e.name,
                 "icon": e.icon, "area_id": e.area_id,
                 "disabled_by": e.disabled_by.value if e.disabled_by else None,
                 "hidden_by": e.hidden_by.value if e.hidden_by else None,
                 "friendly_name": self.attr(e.entity_id, "friendly_name"), "live": self.live(e.entity_id)}
                for e in self.entities()],
            "devices": [
                {"id": d.id, "serial": self.serial(d.id), "name": d.name, "name_by_user": d.name_by_user,
                 "area_id": d.area_id}
                for d in sorted(self.devices(), key=lambda d: self.serial(d.id) or "")],
        }

    async def save(self, name, data):
        def _save():
            with open(os.path.join(WORK, "snap-%s.json" % name), "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1, ensure_ascii=False)
        await self.hass.async_add_executor_job(_save)

    async def load(self, name):
        def _load():
            with open(os.path.join(WORK, "snap-%s.json" % name), encoding="utf-8") as fh:
                return json.load(fh)
        return await self.hass.async_add_executor_job(_load)

    async def touch(self, name):
        await self.hass.async_add_executor_job(lambda: open(os.path.join(WORK, name), "w").close())

    # ---- states
    def st(self, entity_id):
        s = self.hass.states.get(entity_id)
        return s.state if s else None

    def attr(self, entity_id, name):
        s = self.hass.states.get(entity_id)
        return s.attributes.get(name) if s else None

    def live(self, entity_id):
        """False for a registry entry no entity stands behind (shown as 'restored')."""
        s = self.hass.states.get(entity_id)
        if s is None:
            return None
        return not s.attributes.get("restored", False)

    async def refresh(self, ids=None):
        ids = ids if ids is not None else [e.entity_id for e in self.entities()
                                           if not e.disabled_by and self.live(e.entity_id)]
        if ids:
            await self.hass.services.async_call("homeassistant", "update_entity", {"entity_id": ids}, blocking=True)
        return ids

    async def all_available(self, name, expect=None):
        ids = await self.refresh()
        # a button that was never pressed is 'unknown'
        bad = {i: self.st(i) for i in ids if self.st(i) in (None, "unavailable")
               or (self.st(i) == "unknown" and not i.startswith("button."))}
        ok = check("%s (%d entities)" % (name, len(ids)), not bad and (expect is None or len(ids) == expect),
                   (len(ids), bad))
        extras = [e for e in self.extras() if not e.disabled_by]
        if extras and self.since_1_6():
            # averaged values need two minutes of readings, timestamps may be empty
            late = ("_avg", "_last_status", "_filter_ack_since", "_filter_ack_until")
            bad = {e.entity_id: self.st(e.entity_id) for e in extras
                   if self.st(e.entity_id) in (None, "unavailable")
                   or (self.st(e.entity_id) == "unknown" and not e.unique_id.endswith(late))}
            per_unit = len(extras) / max(1, len(self.devices()))
            check("%s: also the %d entities version 1.6 adds (%g per unit)" % (name, len(extras), per_unit),
                  not bad and per_unit == int(per_unit), bad)
        return ok

    def hub(self):
        entry = self.entry()
        return self.hass.data.get("ambientika", {}).get(entry.entry_id) if entry else None


# ------------------------------------------------------------------ building blocks
def old_form(e):
    """True for a unique ID in the form used up to 1.2."""
    key = key_of(e["domain"], e["unique_id"])
    if key is None:
        return False
    if key == "climate":
        return e["unique_id"].startswith("climate_") and e["unique_id"].endswith("_%s" % e["serial"])
    return e["unique_id"] != "%s_%s" % (e["serial"], key)


def new_id(e):
    return "%s_%s" % (e["serial"], key_of(e["domain"], e["unique_id"]))


def compare(ctx, before, untouched=(), added=0, what="after the update"):
    """Compare the registries with a snapshot taken before the integration was exchanged."""
    now = {e.id: e for e in ctx.entities()}
    gone = [b["entity_id"] for b in before["entities"] if b["id"] not in now]
    check("%s: no entry of the entity registry is gone" % what, not gone, gone)
    renamed = {b["entity_id"]: now[b["id"]].entity_id for b in before["entities"]
               if b["id"] in now and now[b["id"]].entity_id != b["entity_id"]}
    check("%s: every entity ID is unchanged" % what, not renamed, renamed)
    wrong, kept = {}, {}
    for b in before["entities"]:
        e = now.get(b["id"])
        if e is None:
            continue
        if b["entity_id"] in untouched or not old_form(b):
            if e.unique_id != b["unique_id"]:
                kept[b["entity_id"]] = (b["unique_id"], e.unique_id)
        elif e.unique_id != new_id(b):
            wrong[b["entity_id"]] = (b["unique_id"], e.unique_id, "expected " + new_id(b))
    moved = sum(1 for b in before["entities"] if b["entity_id"] not in untouched and old_form(b))
    check("%s: %d unique IDs now are serial number + key" % (what, moved), not wrong, wrong)
    check("%s: %d other entries are untouched" % (what, len(before["entities"]) - moved), not kept, kept)
    custom = {}
    for b in before["entities"]:
        e = now.get(b["id"])
        if e is None:
            continue
        have = {"name": e.name, "icon": e.icon, "area_id": e.area_id, "device_id": e.device_id,
                "disabled_by": e.disabled_by.value if e.disabled_by else None,
                "hidden_by": e.hidden_by.value if e.hidden_by else None}
        diff = {k: (b[k], v) for k, v in have.items() if b[k] != v}
        if diff:
            custom[b["entity_id"]] = diff
    check("%s: names, icons, areas, hidden and disabled entities and the device of every entity are kept" % what,
          not custom, custom)
    devices = {d.id: d for d in ctx.devices()}
    dev_diff = {b["serial"]: (b, devices.get(b["id"]) and devices[b["id"]].name_by_user)
                for b in before["devices"]
                if b["id"] not in devices or devices[b["id"]].name_by_user != b["name_by_user"]
                or devices[b["id"]].area_id != b["area_id"]}
    check("%s: the devices are the same (%d), with their own names and areas" % (what, len(before["devices"])),
          not dev_diff and len(devices) == len(before["devices"]), (dev_diff, len(devices)))
    total = len(ctx.entities())
    check("%s: %d entries in the entity registry" % (what, len(before["entities"]) + added),
          total == len(before["entities"]) + added, total)
    dup = {}
    for e in ctx.entities():
        dup.setdefault((e.domain, e.unique_id), []).append(e.entity_id)
    check("%s: no unique ID twice" % what, not [v for v in dup.values() if len(v) > 1],
          [v for v in dup.values() if len(v) > 1])


async def commands(ctx, serial_climate=FLUR, serial_button=KIND):
    """A command and a filter reset reach the cloud for the right unit."""
    climate, button = ctx.one(serial_climate, "climate"), ctx.one(serial_button, "filter_reset")
    await ctx.cloud.clear()
    await ctx.hass.services.async_call("climate", "set_preset_mode", {"entity_id": climate, "preset_mode": "Night"},
                                       blocking=True)
    call = await until(lambda: ctx.cloud.log("change-mode"), 10)
    check("preset Night on %s -> change-mode for unit %s" % (climate, serial_climate),
          call and call[0]["ok"] and call[0]["serial"] == serial_climate and str(call[0]["body"]["operatingMode"]) == "3",
          call)
    await ctx.hass.services.async_call("climate", "set_preset_mode", {"entity_id": climate, "preset_mode": "Smart"},
                                       blocking=True)
    # since 1.6 a filter that already reads good is not reset
    await ctx.reset_idle()
    await ctx.cloud.unit(serial_button, set={"filtersStatus": "Medium"})
    await ctx.cloud.clear()
    await ctx.hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
    call = await until(lambda: ctx.cloud.log("reset-filter"), 10)
    await ctx.cloud.unit(serial_button, set={"filtersStatus": "Good"})
    check("button %s -> filter reset for unit %s" % (button, serial_button), call and call[0]["serial"] == serial_button, call)
    check("nothing but the documented calls reached the cloud", not await ctx.cloud.log("UNEXPECTED"),
          await ctx.cloud.log("UNEXPECTED"))


async def stable(ctx, name, live=None):
    """A further start changes nothing."""
    check("entry loaded", await ctx.loaded(), ctx.state_of_entry())
    before = await ctx.load(name)
    now = ctx.snapshot()
    diff = [(b, n) for b, n in zip(before["entities"], now["entities"])
            if {k: v for k, v in b.items() if k != "friendly_name"} != {k: v for k, v in n.items() if k != "friendly_name"}]
    check("second start: the entity registry is exactly as after the first start (%d entries)" % len(before["entities"]),
          not diff and len(before["entities"]) == len(now["entities"]), diff[:3])
    check("second start: the devices are exactly as after the first start", before["devices"] == now["devices"],
          (before["devices"], now["devices"]))
    await ctx.all_available("second start: every entity has a value", live)


# ------------------------------------------------------------------ phases
async def old_setup(ctx, customise=False, expect=30, snap="old"):
    if not await ctx.set_up():
        return
    ents = ctx.entities()
    check("old version: %d entities" % expect, len(ents) == expect, len(ents))
    snapshot = ctx.snapshot()
    check("old version: every unique ID is built from the unit's name",
          all(old_form(e) or e["unique_id"] == new_id(e) and e["domain"] != "climate" for e in snapshot["entities"]),
          [e["unique_id"] for e in snapshot["entities"] if not old_form(e)])
    if customise:
        check("old version: the entity IDs of the five units",
              sorted(e.entity_id for e in ents) in OLD_ENTITY_IDS, sorted(e.entity_id for e in ents))
        note("old version: entity IDs and names of the climate entity and the button of 'Flur'",
             [(i, ctx.attr(i, "friendly_name")) for i in (ctx.one(FLUR, "climate"), ctx.one(FLUR, "filter_reset"))])
        await customise_installation(ctx)
    await ctx.all_available("old version: every entity has a value")
    await ctx.save(snap, ctx.snapshot())


async def customise_installation(ctx, own_area="climate"):
    """What a user changes in Home Assistant: an entity ID, a name, an icon, areas, hidden and disabled entities.

    own_area names the entity that gets an area of its own. Since 1.3 the
    climate entity is the unit itself (no name of its own), and newer versions
    of Home Assistant give such an entity the area of its device only.
    """
    areas = ar.async_get(ctx.hass)
    wohnen, schlafen = areas.async_create("Wohnen"), areas.async_create("Schlafen")
    ctx.ent.async_update_entity(ctx.one(FLUR, "air_quality"), new_entity_id="sensor.luft_im_flur")
    ctx.ent.async_update_entity(ctx.one(FLUR, "filter_status"), name="Mein Filter", icon="mdi:air-filter")
    ctx.ent.async_update_entity(ctx.one(KIND, "night_alarm"), disabled_by=er.RegistryEntryDisabler.USER)
    ctx.ent.async_update_entity(ctx.one(WOHNEN, "humidity_alarm"), hidden_by=er.RegistryEntryHider.USER)
    ctx.ent.async_update_entity(ctx.one(SCHLAFEN, own_area), area_id=schlafen.id)
    ctx.dev.async_update_device(ctx.device(WOHNEN).id, area_id=wohnen.id)
    ctx.dev.async_update_device(ctx.device(BAD).id, name_by_user="Gästebad unten")
    await ctx.hass.async_block_till_done()
    await asyncio.sleep(1)
    check("customised: renamed entity ID, own name and icon, one entity disabled, one hidden, areas, own device name",
          ctx.ent.async_get("sensor.luft_im_flur") is not None and ctx.st(ctx.one(KIND, "night_alarm")) is None,
          (ctx.one(FLUR, "air_quality"), ctx.st(ctx.one(KIND, "night_alarm"))))


async def new_first(ctx, snap="old", save="new", untouched=(), added=0, live=None, with_commands=True):
    check("entry loaded after the update", await ctx.loaded(), ctx.state_of_entry())
    before = await ctx.load(snap)
    compare(ctx, before, untouched=untouched, added=added)
    await ctx.all_available("after the update: every entity has a value", live)
    names = {}
    for b in before["entities"]:
        if not b["live"] or b["entity_id"] in untouched:
            continue
        device = ctx.dev.async_get(b["device_id"])
        shown = device.name_by_user or device.name
        expected = b["friendly_name"]
        if b["name"] is None and b["domain"] == "climate":
            expected = shown  # was: the name of the unit twice
        elif b["name"] is None and b["domain"] == "button":
            expected = shown + " Filter Reset"  # was: only the name of the unit
        elif b["name"] is None and ctx.since_1_6() and b["unique_id"].endswith("_night_alarm"):
            expected = shown + " Night Mode"  # since 1.6, was: Night Alarm
        if ctx.attr(b["entity_id"], "friendly_name") != expected:
            names[b["entity_id"]] = (b["friendly_name"], ctx.attr(b["entity_id"], "friendly_name"), expected)
    check("after the update: sensors keep their names, the climate entity is named like the unit "
          "(no longer twice), the button is named 'Filter Reset'", not names, names)
    if with_commands:
        await commands(ctx)
    await ctx.save(save, ctx.snapshot())


async def plain_old(ctx):
    await old_setup(ctx, customise=True)


async def plain_new_first(ctx):
    await new_first(ctx, live=29)
    check("the entity renamed by the user is still sensor.luft_im_flur and has a value",
          ctx.st("sensor.luft_im_flur") not in (None, "unavailable"), ctx.st("sensor.luft_im_flur"))
    check("the entity disabled by the user is still disabled", ctx.st(ctx.one(KIND, "night_alarm")) is None,
          ctx.st(ctx.one(KIND, "night_alarm")))


async def plain_new_second(ctx):
    await stable(ctx, "new", 29)


async def same_old(ctx):
    await old_setup(ctx, expect=25)
    per = {s: len([e for e in ctx.entities() if ctx.serial(e.device_id) == s]) for s in (FLUR, WOHNEN)}
    note("old version, two units named 'Flur'", "entities per unit: %s" % per)
    check("old version: one of the two units has only its climate entity", sorted(per.values()) == [1, 6], per)


async def same_new_first(ctx):
    before = await ctx.load("old")
    poor = next(s for s in (FLUR, WOHNEN) if len([e for e in before["entities"] if e["serial"] == s]) == 1)
    await new_first(ctx, added=5, live=30, with_commands=False)
    per = {s: sorted(key_of(e.domain, e.unique_id) for e in ctx.entities() if ctx.serial(e.device_id) == s)
           for s in (FLUR, WOHNEN)}
    check("both units named 'Flur' have all six entities", all(len(v) == 6 and len(set(v)) == 6 for v in per.values()), per)
    old_ids = {e["id"] for e in before["entities"]}
    fresh = [e for e in ctx.entities() if e.id not in old_ids]
    check("the five new entities belong to the unit that had only its climate entity",
          len(fresh) == 5 and all(ctx.serial(e.device_id) == poor for e in fresh)
          and all(e.unique_id == "%s_%s" % (poor, key_of(e.domain, e.unique_id)) for e in fresh),
          [(e.entity_id, e.unique_id, ctx.serial(e.device_id)) for e in fresh])
    note("entity IDs of the five new entities", sorted(e.entity_id for e in fresh))
    for serial in (FLUR, WOHNEN):
        await ctx.cloud.clear()
        button = ctx.one(serial, "filter_reset")
        await ctx.reset_idle()
        await ctx.cloud.unit(serial, set={"filtersStatus": "Medium"})  # since 1.6 a good filter is not reset
        await ctx.cloud.clear()
        await ctx.hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
        call = await until(lambda: ctx.cloud.log("reset-filter"), 10)
        check("button %s resets the filter of unit %s" % (button, serial), call and call[0]["serial"] == serial, call)
    await commands(ctx, serial_climate=WOHNEN, serial_button=KIND)
    await ctx.save("new", ctx.snapshot())


async def same_new_second(ctx):
    await stable(ctx, "new", 30)


async def renamed_old(ctx):
    await old_setup(ctx)


async def renamed_old_again(ctx):
    """The old version after the unit was renamed in the app."""
    check("entry loaded", await ctx.loaded(), ctx.state_of_entry())
    before = await ctx.load("old")
    old_ids = {e["id"] for e in before["entities"]}
    fresh = [e for e in ctx.entities() if e.id not in old_ids]
    dead = [e.entity_id for e in ctx.entities() if e.id in old_ids and ctx.live(e.entity_id) is False]
    note("old version after renaming 'Flur' to 'Gang' in the app",
         "%d entries in the registry, %d new ones (%s), %d left behind without an entity (%s)"
         % (len(ctx.entities()), len(fresh), sorted(e.entity_id for e in fresh), len(dead), sorted(dead)))
    check("old version: the rename created six new entities and left six behind", len(fresh) == 6 and len(dead) == 6,
          (len(fresh), len(dead)))
    await ctx.all_available("old version: every entity has a value", 30)
    await ctx.save("old2", ctx.snapshot())


async def renamed_new_first(ctx):
    before = await ctx.load("old2")
    left = [e["entity_id"] for e in before["entities"] if e["live"] is False]
    await new_first(ctx, snap="old2", untouched=left, live=30)
    check("the entities the unit has under its current name are the ones in use",
          all(ctx.live(i) for i in (ctx.one(FLUR, k) for ks in KEYS.values() for k in ks)),
          {k: ctx.one(FLUR, k) for ks in KEYS.values() for k in ks})
    check("the six entries left behind by the rename are as before (no entity behind them)",
          all(ctx.live(i) is False for i in left), {i: ctx.live(i) for i in left})


async def renamed_new_second(ctx):
    await stable(ctx, "new", 30)


async def norestart_old(ctx):
    await old_setup(ctx)


async def norestart_new_first(ctx):
    before = await ctx.load("old")
    check("entry loaded after the update", await ctx.loaded(), ctx.state_of_entry())
    compare(ctx, before)
    await ctx.all_available("after the update: every entity has a value", 30)
    device = ctx.device(FLUR)
    check("the device now carries the new name of the unit", device and device.name == "Gang", device and device.name)
    climate = ctx.one(FLUR, "climate")
    was = next(b["entity_id"] for b in before["entities"] if b["serial"] == FLUR and b["domain"] == "climate")
    check("the entities keep their entity IDs (%s) and are shown under the new name" % climate,
          climate == was and ctx.attr(climate, "friendly_name") == "Gang"
          and ctx.attr(ctx.one(FLUR, "air_quality"), "friendly_name") == "Gang Air Quality",
          (was, climate, ctx.attr(climate, "friendly_name"), ctx.attr(ctx.one(FLUR, "air_quality"), "friendly_name")))
    await commands(ctx)
    await ctx.save("new", ctx.snapshot())


async def norestart_new_second(ctx):
    await stable(ctx, "new", 30)


async def fresh_new(ctx):
    flow = ctx.hass.config_entries.flow
    res = await flow.async_init("ambientika", context={"source": "user"})
    check("config flow opens with user name and password", res["type"] == "form" and res["step_id"] == "user", res)
    res = await flow.async_configure(res["flow_id"], {"username": USER, "password": "falsch"})
    check("wrong password: the form comes back with the error 'auth'",
          res["type"] == "form" and res.get("errors") == {"base": "auth"}, res.get("errors"))
    await ctx.cloud.outage("500")
    res = await flow.async_configure(res["flow_id"], {"username": USER, "password": PASSWORD})
    check("cloud answers with an error: the form comes back with the error 'connection'",
          res["type"] == "form" and res.get("errors") == {"base": "connection"}, res.get("errors"))
    await ctx.cloud.outage("ok")
    res = await flow.async_configure(res["flow_id"], {"username": USER, "password": PASSWORD})
    await ctx.hass.async_block_till_done()
    check("right password: entry created and loaded",
          res["type"] == "create_entry" and await ctx.loaded(), (res.get("type"), ctx.state_of_entry()))
    snapshot = ctx.snapshot()
    check("30 entities: climate.<unit>, button.<unit>_filter_reset, two sensors and two binary sensors per unit",
          sorted(e["entity_id"] for e in snapshot["entities"]) == sorted(i.replace("_night_alarm", "_night_mode") if ctx.since_1_6() else i for i in NEW_ENTITY_IDS),
          sorted(e["entity_id"] for e in snapshot["entities"]))
    shown = {i: ctx.attr(i, "friendly_name") for i in ("climate.flur", "button.flur_filter_reset", "sensor.flur_air_quality")}
    check("names: 'Flur', 'Flur Filter Reset', 'Flur Air Quality'",
          list(shown.values()) == ["Flur", "Flur Filter Reset", "Flur Air Quality"], shown)
    check("every unique ID is serial number + key",
          all(e["unique_id"] == new_id(e) and e["serial"] for e in snapshot["entities"]),
          [e["unique_id"] for e in snapshot["entities"]])
    await ctx.all_available("every entity has a value", 30)
    await commands(ctx)
    # ---- the same account a second time
    for user in (USER, "  Kunde@Example.COM "):
        res = await ctx.flow(user=user)
        check("the account %r a second time: refused as already set up" % user,
              res.get("type") == "abort" and res.get("reason") == "already_configured" and len(ctx.entries()) == 1,
              ({k: res.get(k) for k in ("type", "reason", "errors")}, len(ctx.entries())))
    # ---- a unit is renamed in the app
    ids = sorted(e.entity_id for e in ctx.entities())
    await ctx.cloud.unit(KIND, name="Kind")
    check("reload after a unit was renamed in the app",
          await ctx.hass.config_entries.async_reload(ctx.entry().entry_id) and await ctx.loaded(), ctx.state_of_entry())
    await ctx.hass.async_block_till_done()
    check("rename: no entity added, none left behind, entity IDs unchanged",
          sorted(e.entity_id for e in ctx.entities()) == ids, sorted(e.entity_id for e in ctx.entities()))
    check("rename: the device carries the new name", ctx.device(KIND).name == "Kind", ctx.device(KIND).name)
    await ctx.all_available("rename: every entity has a value", 30)
    # ---- two units get the same name
    await ctx.cloud.unit(WOHNEN, name="Flur")
    check("reload after a second unit was named 'Flur'",
          await ctx.hass.config_entries.async_reload(ctx.entry().entry_id) and await ctx.loaded(), ctx.state_of_entry())
    await ctx.hass.async_block_till_done()
    check("same name: still 30 entities with unchanged entity IDs",
          sorted(e.entity_id for e in ctx.entities()) == ids, sorted(e.entity_id for e in ctx.entities()))
    await ctx.all_available("same name: every entity of both units has a value", 30)
    await ctx.save("new", ctx.snapshot())


async def fresh_new_second(ctx):
    await stable(ctx, "new", 30)


async def downgrade_new(ctx):
    if await ctx.set_up():
        await ctx.all_available("new version: every entity has a value", 30)
        await ctx.save("new", ctx.snapshot())


async def downgrade_old(ctx):
    """Back to the old version: what it does with the registry of the new one."""
    check("entry loaded", await ctx.loaded(), ctx.state_of_entry())
    before = await ctx.load("new")
    old_ids = {e["id"] for e in before["entities"]}
    fresh = [e for e in ctx.entities() if e.id not in old_ids]
    note("going back to the old version",
         "%d entries in the registry; the old version created %d entities of its own (%s ...) and left the %d of the "
         "new version without an entity" % (len(ctx.entities()), len(fresh), sorted(e.entity_id for e in fresh)[:3],
                                            len([i for i in before["entities"] if ctx.live(i["entity_id"]) is False])))
    await ctx.save("old", ctx.snapshot())


async def downgrade_new_again(ctx):
    check("entry loaded", await ctx.loaded(), ctx.state_of_entry())
    before, first = await ctx.load("old"), await ctx.load("new")
    now = {e.id: e for e in ctx.entities()}
    changed = {b["entity_id"]: (b["unique_id"], now[b["id"]].unique_id) for b in before["entities"]
               if b["id"] in now and now[b["id"]].unique_id != b["unique_id"]}
    check("forward again: both forms are in the registry, nothing is moved and nothing breaks (%d entries)"
          % len(before["entities"]), not changed and len(now) == len(before["entities"]), (changed, len(now)))
    live = sorted(e.entity_id for e in ctx.entities() if ctx.live(e.entity_id))
    check("forward again: the entities of the new version are in use again",
          live == sorted(e["entity_id"] for e in first["entities"]), live)
    await ctx.all_available("forward again: every entity has a value", 30)
    await commands(ctx)


async def outage_old(ctx):
    await old_setup(ctx)


async def start_without_cloud(ctx, comes_back):
    """Home Assistant starts while the cloud cannot be used; comes_back() brings the cloud back."""
    before = await ctx.load("old")
    state = await until(lambda: ctx.state_of_entry() == "setup_retry", 30)
    reason = ctx.entry().reason
    check("start without the cloud: the entry waits and tries again by itself (setup_retry), and says why",
          state and reason and "Ambientika cloud" in reason, (ctx.state_of_entry(), reason))
    note("what Home Assistant shows while it waits", reason)
    now = {e.id: e.unique_id for e in ctx.entities()}
    check("start without the cloud: the entity registry is not touched",
          all(now.get(b["id"]) == b["unique_id"] for b in before["entities"]) and len(now) == len(before["entities"]),
          len(now))
    await comes_back()
    started = time.monotonic()
    ok = await ctx.loaded(150)
    check("cloud back: the integration starts by itself (after %d s)" % (time.monotonic() - started), ok,
          ctx.state_of_entry())
    compare(ctx, before, what="after the cloud is back")
    await ctx.all_available("after the cloud is back: every entity has a value", 30)
    await commands(ctx)


async def outage_new_http(ctx):
    await start_without_cloud(ctx, lambda: ctx.cloud.outage("ok"))


async def outage_new_refused(ctx):
    async def wait_for_cloud():
        # upgrade_test.py starts the stand-in again once it sees this file
        await ctx.touch("cloud-may-return")

        async def up():
            try:
                await ctx.cloud.log()
            except Exception:  # noqa: BLE001
                return False
            return True
        check("the stand-in for the cloud is reachable again", await until(up, 120, 1.0))
    await start_without_cloud(ctx, wait_for_cloud)


async def outage_old_start(ctx):
    """The old version when Home Assistant starts without the cloud."""
    first = ctx.state_of_entry()
    await ctx.cloud.outage("ok")
    await asyncio.sleep(75)
    second = ctx.state_of_entry()
    note("old version, Home Assistant started while the cloud answered with errors",
         "entry state at start: %s; 75 s after the cloud was back: %s" % (first, second))
    check("old version: the entry stays failed until someone reloads it (the defect fixed in 1.3)",
          first == "setup_error" and second == "setup_error", (first, second))


async def expire_new(ctx):
    if not await ctx.set_up():
        return
    await ctx.all_available("every entity has a value", 30)
    hub = ctx.hub()
    # ---- the sign-in expires
    await ctx.cloud.clear()
    await ctx.cloud.expire()
    await ctx.all_available("sign-in expired: every entity has a value again at the next reading", 30)
    auth = await ctx.cloud.log("auth")
    check("sign-in expired: the integration signed in again exactly once (%d refused requests before)"
          % len(await ctx.cloud.log("unauthorized")), len(auth) == 1 and auth[0]["ok"], auth)
    await commands(ctx)
    # ---- it expires again right away: not more than one sign-in per minute
    await ctx.cloud.clear()
    await ctx.cloud.expire()
    await ctx.refresh()
    await ctx.refresh()
    auth = await ctx.cloud.log("auth")
    if ctx.since_1_6():
        await ctx.refresh()     # unavailable after three failed readings in a row
    waiting = {i: ctx.st(i) for i in await ctx.refresh() if not i.startswith("button.")}
    check("expired again within a minute: no second sign-in yet, the %d reading entities wait as unavailable" % len(waiting),
          not auth and len(waiting) == 25 and set(waiting.values()) == {"unavailable"}, (auth, waiting))
    hub._sign_in_not_before = 0.0  # the minute has passed
    await ctx.all_available("after the minute: signed in again, every entity has a value", 30)
    # ---- a command is the first request after the sign-in expired
    hub._sign_in_not_before = 0.0
    await ctx.cloud.expire()
    await commands(ctx)
    check("expired before a command: the command went out after one new sign-in",
          len([a for a in await ctx.cloud.log("auth")]) <= 1, await ctx.cloud.log("auth"))
    # ---- the cloud no longer accepts the password
    hub._sign_in_not_before = 0.0
    await ctx.cloud.clear()
    await ctx.cloud.outage("refuse-auth")
    await ctx.cloud.expire()
    for _ in range(3):
        ids = await ctx.refresh()
    auth = await ctx.cloud.log("auth")
    check("password no longer accepted: one attempt to sign in, then the integration waits (no storm of sign-ins)",
          len(auth) == 1 and not auth[0]["ok"], auth)
    check("password no longer accepted: the reading entities are unavailable",
          all(ctx.st(i) == "unavailable" for i in ids if not i.startswith("button.")),
          {i: ctx.st(i) for i in ids if ctx.st(i) != "unavailable"})
    await ctx.cloud.outage("ok")
    hub._sign_in_not_before = 0.0  # half an hour has passed
    await ctx.all_available("password accepted again: every entity has a value", 30)
    # ---- the cloud cannot be reached while the sign-in has expired
    hub._sign_in_not_before = 0.0
    await ctx.cloud.clear()
    await ctx.cloud.expire()
    await ctx.cloud.outage("500")
    for _ in range(3 if ctx.since_1_6() else 1):
        ids = await ctx.refresh()
    check("cloud answers with errors: the reading entities are unavailable",
          all(ctx.st(i) == "unavailable" for i in ids if not i.startswith("button.")),
          {i: ctx.st(i) for i in ids if ctx.st(i) != "unavailable"})
    await ctx.cloud.outage("ok")
    await ctx.all_available("cloud back: every entity has a value (new sign-in included)", 30)


async def expire_old(ctx):
    """The old version when the sign-in expires."""
    if not await ctx.set_up():
        return
    await ctx.all_available("old version: every entity has a value", 30)
    await ctx.cloud.clear()
    await ctx.cloud.expire()
    ids = await ctx.refresh()
    await ctx.refresh()
    states = {i: ctx.st(i) for i in ids}
    auth = await ctx.cloud.log("auth")
    note("old version after the sign-in expired",
         "%d of %d entities unavailable, %d unknown, %d new sign-ins"
         % (list(states.values()).count("unavailable"), len(ids), list(states.values()).count("unknown"), len(auth)))
    check("old version: the units stay unavailable and nobody signs in again (the defect fixed in 1.3)",
          not auth and all(v in ("unavailable", "unknown") for v in states.values()), (auth, states))


async def password_old(ctx):
    await old_setup(ctx)


async def password_new(ctx):
    """The cloud does not accept the stored password when Home Assistant starts."""
    before = await ctx.load("old")
    state = ctx.state_of_entry()
    check("password not accepted at the start: the entry reports an error (setup_error), no endless retries",
          state == "setup_error", state)
    reason = ctx.entry().reason
    check("password not accepted at the start: Home Assistant shows the reason",
          reason and "did not accept user name and password" in reason, reason)
    await asyncio.sleep(20)
    auth = await ctx.cloud.log("auth")
    check("password not accepted at the start: exactly one attempt to sign in within 20 s",
          len(auth) == 1 and not auth[0]["ok"], auth)
    now = {e.id: e.unique_id for e in ctx.entities()}
    check("password not accepted at the start: the entity registry is not touched",
          all(now.get(b["id"]) == b["unique_id"] for b in before["entities"]), len(now))
    await ctx.cloud.outage("ok")
    await ctx.hass.config_entries.async_reload(ctx.entry().entry_id)
    check("password accepted again and entry reloaded: loaded", await ctx.loaded(60), ctx.state_of_entry())
    compare(ctx, before, what="after the reload")
    await ctx.all_available("after the reload: every entity has a value", 30)


async def previous_setup(ctx):
    """The last released version sets the installation up, and the user customises it."""
    if not await ctx.set_up():
        return
    snapshot = ctx.snapshot()
    check("previous version: 30 entities with the entity IDs of a new installation",
          sorted(e["entity_id"] for e in snapshot["entities"]) == sorted(i.replace("_night_alarm", "_night_mode") if ctx.since_1_6() else i for i in NEW_ENTITY_IDS),
          sorted(e["entity_id"] for e in snapshot["entities"]))
    check("previous version: every unique ID is serial number + key",
          all(e["unique_id"] == new_id(e) and e["serial"] for e in snapshot["entities"]),
          [e["unique_id"] for e in snapshot["entities"]])
    await customise_installation(ctx, own_area="filter_status")
    await ctx.all_available("previous version: every entity has a value")
    await ctx.save("previous", ctx.snapshot())


def same_names(ctx, before, what):
    changed = {b["entity_id"]: (b["friendly_name"], ctx.attr(b["entity_id"], "friendly_name"))
               for b in before["entities"] if b["live"] and ctx.attr(b["entity_id"], "friendly_name") != b["friendly_name"]
               # since 1.6 "Night Alarm" is named "Night Mode" (same entity, same entity ID)
               and not b["unique_id"].endswith("_night_alarm")}
    check("%s: every entity is shown under the same name as before" % what, not changed, changed)


async def previous_new_first(ctx):
    check("entry loaded after the update", await ctx.loaded(), ctx.state_of_entry())
    before = await ctx.load("previous")
    compare(ctx, before)
    await ctx.all_available("after the update: every entity has a value", 29)
    same_names(ctx, before, "after the update")
    check("the entity renamed by the user is still sensor.luft_im_flur and has a value",
          ctx.st("sensor.luft_im_flur") not in (None, "unavailable"), ctx.st("sensor.luft_im_flur"))
    check("the entity disabled by the user is still disabled", ctx.st(ctx.one(KIND, "night_alarm")) is None,
          ctx.st(ctx.one(KIND, "night_alarm")))
    await commands(ctx)
    await ctx.save("new", ctx.snapshot())


async def previous_new_second(ctx):
    await stable(ctx, "new", 29)


async def previous_back(ctx):
    """Back on the previous version, which knows the same unique IDs."""
    check("entry loaded", await ctx.loaded(), ctx.state_of_entry())
    before = await ctx.load("new")
    compare(ctx, before, what="back on the previous version")
    await ctx.all_available("back on the previous version: every entity has a value", 30)
    same_names(ctx, before, "back on the previous version")
    await commands(ctx)


async def previous_forward(ctx):
    check("entry loaded", await ctx.loaded(), ctx.state_of_entry())
    before = await ctx.load("new")
    compare(ctx, before, what="forward again")
    await ctx.all_available("forward again: every entity has a value", 30)
    same_names(ctx, before, "forward again")
    await commands(ctx)


PHASES = {
    "previous-update:previous": previous_setup, "previous-update:new-first": previous_new_first,
    "previous-update:new-second": previous_new_second,
    "previous-downgrade:new": downgrade_new, "previous-downgrade:previous": previous_back,
    "previous-downgrade:new-again": previous_forward,
    "plain:old": plain_old, "plain:new-first": plain_new_first, "plain:new-second": plain_new_second,
    "same-name:old": same_old, "same-name:new-first": same_new_first, "same-name:new-second": same_new_second,
    "renamed:old": renamed_old, "renamed:old-again": renamed_old_again,
    "renamed:new-first": renamed_new_first, "renamed:new-second": renamed_new_second,
    "renamed-no-restart:old": norestart_old, "renamed-no-restart:new-first": norestart_new_first,
    "renamed-no-restart:new-second": norestart_new_second,
    "fresh:new": fresh_new, "fresh:new-second": fresh_new_second,
    "downgrade:new": downgrade_new, "downgrade:old": downgrade_old, "downgrade:new-again": downgrade_new_again,
    "start-http-error:old": outage_old, "start-http-error:new": outage_new_http,
    "start-no-connection:old": outage_old, "start-no-connection:new": outage_new_refused,
    "start-http-error-old-version:old": outage_old, "start-http-error-old-version:old-start": outage_old_start,
    "sign-in-expires:new": expire_new, "sign-in-expires-old-version:old": expire_old,
    "password-not-accepted:old": password_old, "password-not-accepted:new": password_new,
}


async def run(hass: HomeAssistant):
    # Home Assistant 2026.11 tries out a changed HTTP configuration (the port of
    # this test) and restarts after five minutes unless someone confirms it.
    try:
        from homeassistant.components.http.config import DATA_STORE  # noqa: PLC0415
        hass.data[DATA_STORE].async_cancel_revert()
    except Exception:  # noqa: BLE001  (older versions have no such trial)
        pass
    ctx = Ctx(hass)
    try:
        await PHASES["%s:%s" % (SCENARIO, PHASE)](ctx)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.exception("test driver failed")
        check("test driver ran through", False, repr(exc))
    finally:
        await ctx.cloud.session.close()

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
