#!/usr/bin/env python3
"""Local stand-in for the Ambientika cloud API over real HTTP, for system tests.

    python sim/fake_cloud.py PORT [STATE_FILE]

It answers the endpoints the bridge uses (users/authenticate, house/houses-info,
house/house-complete-info, device/device-status, device/change-mode,
device/reset-filter) with the same checks as the real server and keeps five
units (two zones, Masters and Slaves). Point the bridge at it with
AMBIENTIKA_HOST=http://127.0.0.1:PORT (or ambientika_host in options.json /
ambientika.host in config.yaml). Login: kunde@example.com / geheim#1 äöü.

A control API under /_ctl lets a test change values and switch on faults:

    GET  /_ctl/log      every call the bridge made (JSON list)
    POST /_ctl/clear    empty that log
    POST /_ctl/unit     {"serial": ..., "set": {...status fields...}, "name": "name in the app",
                         "fault": null | "404" | "500" | "hang" | "garbage" | "html"}
    POST /_ctl/outage   {"mode": "ok" | "500" | "hang" | "refuse-auth"}
    POST /_ctl/expire   invalidate the token (the bridge has to log in again)
    GET  /_ctl/state    the units as the cloud holds them
    POST /_ctl/reset    back to the initial state

"hang" accepts the request and never answers (until the client gives up).
"""
import asyncio
import json
import sys
import time

from aiohttp import web

PORT = int(sys.argv[1])
STATE_FILE = sys.argv[2] if len(sys.argv) > 2 else None

MODES = ["Smart", "Auto", "ManualHeatRecovery", "Night", "AwayHome", "Surveillance", "TimedExpulsion",
         "Expulsion", "Intake", "MasterSlaveFlow", "SlaveMasterFlow", "Off"]
FANS = ["Low", "Medium", "High"]
HUMS = ["Dry", "Normal", "Moist"]
LIGHTS = ["NotAvailable", "Off", "Low", "Medium"]

USER, PASSWORD = "kunde@example.com", "geheim#1 äöü"
HANG_S = 3600

UNITS = [  # serial, name, role, zone
    ("A0B1C2D3E402", "Flur", "Master", 0),
    ("A0B1C2D3E401", "Wohnzimmer", "SlaveEqualMaster", 0),
    ("A0B1C2D3E403", "Kinderzimmer", "SlaveOppositeMaster", 0),
    ("A0B1C2D3E405", "Gäste-Bad", "SlaveEqualMaster", 0),
    ("A0B1C2D3E404", "Schlafzimmer Eltern", "Master", 1),
]


def fresh():
    units = {}
    for serial, name, role, zone in UNITS:
        units[serial] = {
            "name": name, "role": role, "zone": zone, "fault": None,
            "status": {
                "operatingMode": "Smart", "fanSpeed": "Medium", "humidityLevel": "Normal",
                "lightSensorLevel": "Low", "temperature": 21, "humidity": 55, "airQuality": "Good",
                "humidityAlarm": False, "filtersStatus": "Good", "nightAlarm": False,
                "deviceRole": role, "lastOperatingMode": "Smart", "packetType": "DeviceStatus",
                "deviceType": "Smart", "deviceSerialNumber": serial,
            },
        }
    return {"units": units, "token_no": 1, "outage": "ok", "log": []}


S = fresh()
if STATE_FILE:
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            S = json.load(fh)
    except Exception:
        pass


def save():
    if STATE_FILE:
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(S, fh)


def log(kind, **kw):
    S["log"].append({"t": round(time.time(), 3), "kind": kind, **kw})
    save()


def token():
    return "tok-%d" % S["token_no"]


async def hang():
    await asyncio.sleep(HANG_S)
    return web.Response(status=504)


async def gate(req):
    """Outage modes and the token check, the first step of every API call."""
    mode = S["outage"]
    if mode == "500":
        return web.json_response({"message": "Internal Server Error"}, status=500)
    if mode == "hang":
        return await hang()
    if req.headers.get("Authorization") != "Bearer " + token():
        log("unauthorized", path=req.path)
        return web.json_response({"message": "Unauthorized"}, status=401)
    return None


async def auth(req):
    if S["outage"] == "500":
        return web.json_response({"message": "Internal Server Error"}, status=500)
    if S["outage"] == "hang":
        return await hang()
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"message": "Bad Request"}, status=400)
    ok = (isinstance(body, dict) and body.get("username") == USER and body.get("password") == PASSWORD
          and S["outage"] != "refuse-auth")
    log("auth", user=body.get("username") if isinstance(body, dict) else None, ok=ok)
    if not ok:
        return web.json_response({"message": "Username or password is incorrect"}, status=400)
    return web.json_response({"id": 7, "jwtToken": token()})


async def houses(req):
    bad = await gate(req)
    if bad:
        return bad
    return web.json_response([{"houseId": 1}])


async def house(req):
    bad = await gate(req)
    if bad:
        return bad
    devs = [{"id": i + 1, "deviceType": "Smart", "serialNumber": s, "userId": 7, "name": u["name"],
             "role": u["role"], "zoneIndex": u["zone"], "installation": "Wall", "roomId": 1}
            for i, (s, u) in enumerate(S["units"].items())]
    return web.json_response({
        "userId": 7, "id": 1, "name": "Zuhause", "zones": [], "hasZones": True, "hasDevices": True,
        "address": "", "latitude": 46.4, "longitude": 11.2,
        "rooms": [{"id": 1, "name": "Haus", "houseId": 1, "userId": 7, "devices": devs}]})


async def status(req):
    bad = await gate(req)
    if bad:
        return bad
    serial = req.query.get("deviceSerialNumber", "")
    u = S["units"].get(serial)
    if u is None:
        return web.Response(status=404, text="Device not found")
    fault = u.get("fault")
    if fault == "404":
        return web.Response(status=404, text="Status packet not found")
    if fault == "500":
        return web.json_response({"message": "boom"}, status=500)
    if fault == "hang":
        log("hang", serial=serial)
        return await hang()
    if fault == "garbage":
        return web.Response(status=200, text='{"operatingMode": "Smart", "fanSp', content_type="application/json")
    if fault == "html":
        return web.Response(status=200, text="<html><body>502 Bad Gateway</body></html>", content_type="text/html")
    return web.json_response(u["status"])


async def change(req):
    bad = await gate(req)
    if bad:
        return bad
    try:
        b = await req.json()
    except Exception:
        return web.json_response({"message": "Bad Request"}, status=400)
    if not isinstance(b, dict):
        return web.json_response({"message": "Bad Request"}, status=400)
    serial = b.get("deviceSerialNumber")
    u = S["units"].get(serial)
    ok = (u is not None
          and str(b.get("operatingMode")) in [str(i) for i in range(12)]
          and type(b.get("fanSpeed")) is int and b.get("fanSpeed") in (0, 1, 2)
          and type(b.get("humidityLevel")) is int and b.get("humidityLevel") in (0, 1, 2)
          # older clients send no light sensor level; the unit keeps its own then
          and ("lightSensorLevel" not in b
               or (type(b["lightSensorLevel"]) is int and b["lightSensorLevel"] in (0, 1, 2, 3))))
    log("change-mode", serial=serial, body=b, ok=ok)
    if not ok:
        return web.json_response(
            {"message": "Value was either too large or too small for an unsigned byte"}, status=500)
    if u.get("fault"):
        # The real cloud accepts a command for a silent unit; it just never lands.
        return web.Response(status=200)
    st = u["status"]
    old = st["operatingMode"]
    st["operatingMode"] = MODES[int(b["operatingMode"])]
    if st["operatingMode"] != old:
        st["lastOperatingMode"] = old
    st["fanSpeed"] = FANS[b["fanSpeed"]]
    st["humidityLevel"] = HUMS[b["humidityLevel"]]
    if "lightSensorLevel" in b:
        st["lightSensorLevel"] = LIGHTS[b["lightSensorLevel"]]
    save()
    return web.Response(status=200)


async def reset(req):
    bad = await gate(req)
    if bad:
        return bad
    serial = req.query.get("deviceSerialNumber", "")
    u = S["units"].get(serial)
    log("reset-filter", serial=serial, known=u is not None)
    if u is None:
        return web.Response(status=404, text="Device not found")
    if u["role"] == "Master" and not u.get("fault"):
        u["status"]["filtersStatus"] = "Good"     # only a Master's counter clears
        save()
    return web.Response(status=200)


async def other(req):
    log("UNEXPECTED", method=req.method, path=req.path)
    return web.Response(status=404, text="not part of the API")


# ---- control API for the test driver ----
async def ctl_log(req):
    return web.json_response(S["log"])


async def ctl_clear(req):
    S["log"] = []
    save()
    return web.json_response({"ok": True})


async def ctl_unit(req):
    b = await req.json()
    u = S["units"][b["serial"]]
    if "set" in b:
        u["status"].update(b["set"])
    if "fault" in b:
        u["fault"] = b["fault"]
    if "name" in b:
        u["name"] = b["name"]
    save()
    return web.json_response({"ok": True})


async def ctl_outage(req):
    S["outage"] = (await req.json())["mode"]
    save()
    return web.json_response({"ok": True})


async def ctl_expire(req):
    S["token_no"] += 1
    save()
    return web.json_response({"token_no": S["token_no"]})


async def ctl_state(req):
    return web.json_response({s: {"fault": u["fault"], **u["status"]} for s, u in S["units"].items()})


async def ctl_reset(req):
    global S
    S = fresh()
    save()
    return web.json_response({"ok": True})


app = web.Application()
app.add_routes([
    web.post("/users/authenticate", auth),
    web.get("/house/houses-info", houses),
    web.get("/house/house-complete-info", house),
    web.get("/device/device-status", status),
    web.post("/device/change-mode", change),
    web.get("/device/reset-filter", reset),
    web.get("/_ctl/log", ctl_log),
    web.post("/_ctl/clear", ctl_clear),
    web.post("/_ctl/unit", ctl_unit),
    web.post("/_ctl/outage", ctl_outage),
    web.post("/_ctl/expire", ctl_expire),
    web.get("/_ctl/state", ctl_state),
    web.post("/_ctl/reset", ctl_reset),
    web.route("*", "/{tail:.*}", other),
])
web.run_app(app, host="127.0.0.1", port=PORT, print=None, shutdown_timeout=0.2)
