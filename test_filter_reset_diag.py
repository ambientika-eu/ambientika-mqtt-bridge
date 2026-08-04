"""Tests for the device+zone-Master filter reset (bridge.py).

The official cloud API documents exactly one filter reset:
GET device/reset-filter?deviceSerialNumber=... . Per the RS485 protocol the
MASTER of a coupled group applies it, so _reset_filter sends that documented GET
to the target device and, if the target is a Slave, to the Master of its zone;
it keeps the first that actually clears the counter. These tests drive that walk
with a scripted _reset_request and assert both correctness and safety: only
device/reset-filter is ever contacted and DELETE is never sent."""
import asyncio
import bridge
from returns.result import Success, Failure

bridge.FILTER_RESET_VERIFY_DELAY = 0  # no real wait in tests

PASS, FAIL = [], []
def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok    " if cond else "  FAIL  ") + name)


class Dev:
    def __init__(self, serial, statuses=None, role="SlaveEqualMaster", zone=1, ok=True):
        self.serial_number = serial
        self.id = 1
        self.role = role
        self.zone_index = zone
        self.api = object()
        self._st = list(statuses or ["Bad"])
        self._ok = ok

    async def status(self):
        if not self._ok:
            return Failure({"status_code": 500, "data": "boom"})
        fs = self._st.pop(0) if len(self._st) > 1 else self._st[0]
        return Success({
            "operating_mode": bridge.OperatingMode.Night,
            "fan_speed": bridge.FanSpeed.Medium,
            "humidity_level": bridge.HumidityLevel.Normal,
            "light_sensor_level": bridge.LightSensorLevel.Off,
            "filters_status": fs,
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


# A) Slave: target reset alone does nothing, the ZONE MASTER reset clears it -> True.
master = Dev("MASTER", role="Master", zone=1)
r, sent = run(Dev("SLAVE", role="SlaveEqualMaster", zone=1), ["Bad", "Bad", "Good"],
              others=[master])
check("master reset clears the slave -> True", r is True)
check("A: target device contacted first", sent and sent[0] == ("GET", "device/reset-filter", "SLAVE"))
check("A: zone master contacted second", len(sent) >= 2 and sent[1] == ("GET", "device/reset-filter", "MASTER"))
check("A: only reset-filter GET", only_reset_filter(sent))
check("A: no DELETE", no_delete(sent))

# B) Target reset alone already clears it -> True, master never contacted.
r, sent = run(Dev("SLAVE", role="SlaveEqualMaster", zone=1), ["Bad", "Good"],
              others=[Dev("MASTER", role="Master", zone=1)])
check("target reset alone clears -> True", r is True)
check("B: master not contacted when target already cleared",
      [s for _, _, s in sent] == ["SLAVE"])

# C) The device IS the master -> only itself is contacted (no separate master).
r, sent = run(Dev("MASTER", role="Master", zone=1), ["Bad", "Good"],
              others=[Dev("SLAVE", role="SlaveEqualMaster", zone=1)])
check("master device: only itself contacted", [s for _, _, s in sent] == ["MASTER"])

# D) No master in the same zone -> only the target is contacted.
r, sent = run(Dev("SLAVE", role="SlaveEqualMaster", zone=1), ["Bad"],
              others=[Dev("OTHERMASTER", role="Master", zone=2)])
check("no zone master -> only target contacted", [s for _, _, s in sent] == ["SLAVE"])
check("D: only reset-filter GET", only_reset_filter(sent))

# E) Neither device nor master clears it -> honest False, both were contacted.
r, sent = run(Dev("SLAVE", role="SlaveEqualMaster", zone=1), ["Bad"],
              others=[Dev("MASTER", role="Master", zone=1)])
check("neither clears -> False", r is False)
check("E: both device and master were tried", [s for _, _, s in sent] == ["SLAVE", "MASTER"])
check("E: no DELETE", no_delete(sent))

# F) Status unreadable -> still sends the documented reset (no crash), honest False.
r, sent = run(Dev("SLAVE", role="SlaveEqualMaster", zone=1), ["Bad"],
              others=[Dev("MASTER", role="Master", zone=1)], ok=False)
check("unreadable status -> False (no crash)", r is False)
check("F: only reset-filter GET", only_reset_filter(sent))

# G) verify budget is a small positive cap.
check("verify budget is a small positive cap",
      isinstance(bridge.FILTER_RESET_MAX_VERIFIES, int)
      and 0 < bridge.FILTER_RESET_MAX_VERIFIES <= 20)


if __name__ == "__main__":
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    raise SystemExit(1 if FAIL else 0)
