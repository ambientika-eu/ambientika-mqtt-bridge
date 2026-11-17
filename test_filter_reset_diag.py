"""Tests for the filter reset walk (bridge.py, _reset_filter).

The official cloud API documents exactly one filter reset:
GET device/reset-filter?deviceSerialNumber=... . _reset_filter sends that
documented GET to the target device and, if the target is a Slave, to the Master
of its zone, then reads the real status and reports what happened:

  "confirmed"    the counter really improved (or was already Good)
  "unconfirmed"  nothing could be sent, or the counter did not move
  "acknowledged" Slave with SLAVE_FILTER_SOFT_RESET: recorded bridge-side

These tests drive that walk with a scripted _reset_request and assert both the
result and the safety rule: only device/reset-filter is ever contacted, always
as GET, and DELETE is never sent."""
import asyncio
import json
import os
import tempfile

import bridge
from returns.result import Success, Failure

bridge.FILTER_RESET_VERIFY_DELAY = 0  # no real wait in tests
os.environ.pop("SLAVE_FILTER_SOFT_RESET", None)
os.environ["FILTER_ACK_PATH"] = os.path.join(tempfile.mkdtemp(), "filter_ack.json")

PASS, FAIL = [], []
def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok    " if cond else "  FAIL  ") + name + ("" if cond else "  <<< %r" % (extra,)))


class Dev:
    def __init__(self, serial, statuses=None, role="SlaveEqualMaster", zone=1, ok=True):
        self.serial_number = serial
        self.name = serial.title()
        self.id = 1
        self.role = role
        self.zone_index = zone
        self.api = object()
        self._st = list(statuses or ["Bad"])
        self._ok = ok
        self.reads = 0

    async def status(self):
        self.reads += 1
        if not self._ok:
            return Failure({"status_code": 500, "data": "boom"})
        fs = self._st.pop(0) if len(self._st) > 1 else self._st[0]
        return Success({
            "operating_mode": bridge.OperatingMode.Night,
            "fan_speed": bridge.FanSpeed.Medium,
            "humidity_level": bridge.HumidityLevel.Normal,
            "light_sensor_level": bridge.LightSensorLevel.Off,
            "filters_status": fs,
            "device_role": self.role,
        })


def run(target, statuses, others=(), script=None, ok=True):
    """target=Dev under test; others=extra devices in the house (e.g. the Master).
    script maps a serial -> (status, allow, body) for its reset-filter GET;
    default 200. Captures every (method, path, serial) sent."""
    b = bridge.AmbientikaBridge(bridge.BridgeConfig())
    target._st = list(statuses)
    target._ok = ok
    b.devices = {target.serial_number: target}
    for o in others:
        b.devices[o.serial_number] = o
    sent = []

    async def fake(self, device, method, path, body):
        ser = body.get("deviceSerialNumber")
        sent.append((method.upper(), path, ser))
        return (script or {}).get(ser, (200, None, ""))

    b._reset_request = fake.__get__(b, bridge.AmbientikaBridge)
    r = asyncio.run(b._reset_filter(target))
    return r, sent


def only_reset_filter(sent):
    return all(p == "device/reset-filter" and m == "GET" for m, p, _ in sent)

def no_delete(sent):
    return all(m != "DELETE" for m, _, _ in sent)

def serials(sent):
    return [s for _, _, s in sent]


# A) Slave whose counter stays Bad: sent to the unit and its zone Master, and the
#    bridge says plainly that it did not clear.
r, sent = run(Dev("SLAVE", role="SlaveEqualMaster", zone=1), ["Bad", "Bad", "Bad"],
              others=[Dev("MASTER", role="Master", zone=1)])
check("A: slave counter stays -> unconfirmed", r == "unconfirmed", r)
check("A: target device contacted first", sent and sent[0] == ("GET", "device/reset-filter", "SLAVE"), sent)
check("A: zone master contacted second", len(sent) == 2 and sent[1] == ("GET", "device/reset-filter", "MASTER"), sent)
check("A: only reset-filter GET", only_reset_filter(sent), sent)
check("A: no DELETE", no_delete(sent), sent)

# B) Slave whose status really reads Good afterwards -> confirmed.
r, sent = run(Dev("SLAVE", role="SlaveEqualMaster", zone=1), ["Bad", "Good"],
              others=[Dev("MASTER", role="Master", zone=1)])
check("B: counter really cleared -> confirmed", r == "confirmed", r)
check("B: unit and zone master, nothing else", serials(sent) == ["SLAVE", "MASTER"], sent)
check("B: only reset-filter GET", only_reset_filter(sent), sent)

# C) The device IS the master -> only itself is contacted.
r, sent = run(Dev("MASTER", role="Master", zone=1), ["Bad", "Good"],
              others=[Dev("SLAVE", role="SlaveEqualMaster", zone=1)])
check("C: master cleared -> confirmed", r == "confirmed", r)
check("C: master device: only itself contacted", serials(sent) == ["MASTER"], sent)

# D) No master in the same zone -> only the target is contacted.
r, sent = run(Dev("SLAVE", role="SlaveEqualMaster", zone=1), ["Bad"],
              others=[Dev("OTHERMASTER", role="Master", zone=2)])
check("D: no zone master -> only target contacted", serials(sent) == ["SLAVE"], sent)
check("D: only reset-filter GET", only_reset_filter(sent), sent)

# E) Master whose counter never moves: re-checked three times, then unconfirmed.
m = Dev("MASTER", role="Master", zone=1)
r, sent = run(m, ["Bad"])
check("E: master counter never moves -> unconfirmed", r == "unconfirmed", r)
check("E: one read before and three checks after", m.reads == 4, m.reads)
check("E: sent once, GET only", sent == [("GET", "device/reset-filter", "MASTER")], sent)

# F) Medium (yellow) is a real reset case and counts as improved when it reads Good.
r, sent = run(Dev("MASTER", role="Master", zone=1), ["Medium", "Medium", "Good"])
check("F: Medium -> Good after the second check -> confirmed", r == "confirmed", r)
check("F: reset was sent for a Medium counter", serials(sent) == ["MASTER"], sent)

# G) Bad -> Medium is one step less urgent and counts as confirmed.
r, sent = run(Dev("MASTER", role="Master", zone=1), ["Bad", "Medium"])
check("G: Bad -> Medium -> confirmed", r == "confirmed", r)

# H) Already Good -> nothing to do, nothing sent.
r, sent = run(Dev("MASTER", role="Master", zone=1), ["Good"])
check("H: already Good -> confirmed", r == "confirmed", r)
check("H: nothing sent", sent == [], sent)

# I) Status unreadable -> the documented reset is still sent (no crash), unconfirmed.
r, sent = run(Dev("SLAVE", role="SlaveEqualMaster", zone=1), ["Bad"],
              others=[Dev("MASTER", role="Master", zone=1)], ok=False)
check("I: unreadable status -> unconfirmed (no crash)", r == "unconfirmed", r)
check("I: only reset-filter GET", only_reset_filter(sent) and len(sent) == 2, sent)
check("I: no DELETE", no_delete(sent), sent)

# J) The cloud refuses (HTTP 4xx/5xx) or cannot be reached -> unconfirmed, and
#    still nothing but the documented GET.
for code in (400, 404, 500):
    r, sent = run(Dev("SLAVE", role="SlaveEqualMaster", zone=1), ["Bad"],
                  others=[Dev("MASTER", role="Master", zone=1)],
                  script={"SLAVE": (code, None, "no"), "MASTER": (code, None, "no")})
    check("J: HTTP %d from the cloud -> unconfirmed" % code, r == "unconfirmed", r)
    check("J: HTTP %d: only reset-filter GET, no DELETE" % code,
          only_reset_filter(sent) and no_delete(sent) and serials(sent) == ["SLAVE", "MASTER"], sent)
r, sent = run(Dev("MASTER", role="Master", zone=1), ["Bad"], script={"MASTER": (None, None, "")})
check("J: cloud not reachable -> unconfirmed", r == "unconfirmed", r)

# K) Slave with the maintenance acknowledgement switched on: the raw counter
#    stays, the bridge records "serviced" -> acknowledged. Still GET only.
os.environ["SLAVE_FILTER_SOFT_RESET"] = "1"
try:
    r, sent = run(Dev("SLAVE", role="SlaveEqualMaster", zone=1), ["Bad"],
                  others=[Dev("MASTER", role="Master", zone=1)])
    check("K: slave + soft reset -> acknowledged", r == "acknowledged", r)
    check("K: only reset-filter GET, no DELETE", only_reset_filter(sent) and no_delete(sent), sent)
    try:
        stored = json.load(open(os.environ["FILTER_ACK_PATH"]))
    except Exception as e:
        stored = e
    check("K: acknowledgement stored for the slave only",
          isinstance(stored, dict) and list(stored) == ["SLAVE"], stored)
    # A Master is never acknowledged: its counter can really clear.
    r, sent = run(Dev("MASTER", role="Master", zone=1), ["Bad"])
    check("K: master is not acknowledged -> unconfirmed", r == "unconfirmed", r)
finally:
    os.environ.pop("SLAVE_FILTER_SOFT_RESET", None)


if __name__ == "__main__":
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    print("RESULT: ALL PASS" if not FAIL else "RESULT: FAILURES: %s" % FAIL)
    raise SystemExit(1 if FAIL else 0)
