"""Tests for the change-mode based filter reset (bridge.py).

Insight from the Advanced+ RS485 protocol: the filter reset is a flag on the
control message, not a dedicated endpoint. Its cloud form is POST
device/change-mode, so _reset_filter re-sends the device's CURRENT mode
(unchanged) with a filter-reset field added and keeps the first variant that
actually clears the counter. These tests drive that walk with a scripted
_reset_request and assert both correctness and safety: the running mode is never
altered, only change-mode/reset-filter are contacted, and DELETE is never sent."""
import asyncio
import bridge
from returns.result import Success, Failure

bridge.FILTER_RESET_VERIFY_DELAY = 0  # no real wait in tests

OM, FS, HL, LL = (bridge.OperatingMode, bridge.FanSpeed,
                  bridge.HumidityLevel, bridge.LightSensorLevel)

PASS, FAIL = [], []
def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok    " if cond else "  FAIL  ") + name)


class Dev:
    """Device whose status reports a fixed current mode + a filters_status queue."""
    def __init__(self, statuses, ok=True):
        self.serial_number = "D1"
        self.id = 7
        self.api = object()
        self._st = list(statuses)
        self._ok = ok  # False -> status() fails, so the mode is unreadable

    async def status(self):
        if not self._ok:
            return Failure({"status_code": 500, "data": "boom"})
        fs = self._st.pop(0) if len(self._st) > 1 else self._st[0]
        return Success({
            "operating_mode": OM.Night,
            "fan_speed": FS.Medium,
            "humidity_level": HL.Normal,
            "light_sensor_level": LL.Off,
            "filters_status": fs,
        })


def run(script, statuses, ok=True):
    """script maps (METHOD, path) -> (status, allow, body). Unlisted -> 404.
    Captures every (method, path, body) the walk sends."""
    b = bridge.AmbientikaBridge(bridge.BridgeConfig())
    sent = []

    async def fake(self, device, method, path, body):
        sent.append((method.upper(), path, dict(body)))
        return script.get((method.upper(), path), (404, None, ""))

    b._reset_request = fake.__get__(b, bridge.AmbientikaBridge)
    r = asyncio.run(b._reset_filter(Dev(statuses, ok)))
    return r, sent


def only_allowed_paths(sent):
    return all(p in ("device/change-mode", "device/reset-filter") for _, p, _ in sent)

def no_delete(sent):
    return all(m != "DELETE" for m, _, _ in sent)

def mode_never_changed(sent):
    """Every change-mode body must echo the current mode exactly."""
    for _, p, body in sent:
        if p == "device/change-mode":
            if (body.get("operatingMode") != str(OM.Night.value)
                    or body.get("fanSpeed") != FS.Medium.value
                    or body.get("humidityLevel") != HL.Normal.value
                    or body.get("lightSensorLevel") != LL.Off.value):
                return False
    return True


# A) change-mode with a reset flag clears the counter -> True, tried before baseline.
r, sent = run({("POST", "device/change-mode"): (200, None, "")}, ["Bad", "Good"])
check("change-mode clears -> True", r is True)
check("first probe is POST device/change-mode",
      bool(sent) and sent[0][0] == "POST" and sent[0][1] == "device/change-mode")
check("A: a filter-reset flag rides in the body",
      any(k in sent[0][2] for k in ("resetFilter", "filterReset", "resetFilterAlarm")))
check("A: running mode echoed unchanged", mode_never_changed(sent))
check("A: only change-mode / reset-filter contacted", only_allowed_paths(sent))
check("A: no DELETE", no_delete(sent))

# B) every change-mode is an accepted no-op + baseline GET no-op -> honest False,
#    and the baseline GET reset-filter is tried last.
r, sent = run({("POST", "device/change-mode"): (200, None, ""),
               ("GET", "device/reset-filter"): (200, None, "")}, ["Bad"])
check("all no-op -> False", r is False)
check("baseline GET reset-filter is contacted last",
      sent[-1] == ("GET", "device/reset-filter", {"deviceSerialNumber": "D1"}))
check("B: mode never changed", mode_never_changed(sent))
check("B: only change-mode / reset-filter", only_allowed_paths(sent))
check("B: no DELETE", no_delete(sent))

# C) A later change-mode variant clears (counter turns Good only after the 2nd
#    verify) -> True, and more than one variant was tried, all echoing the mode.
r, sent = run({("POST", "device/change-mode"): (200, None, "")},
              ["Bad", "Bad", "Good"])
cm_calls = [s for s in sent if s[1] == "device/change-mode"]
check("clears on a later change-mode variant -> True", r is True)
check("C: more than one change-mode variant tried", len(cm_calls) >= 2)
check("C: mode never changed across variants", mode_never_changed(sent))

# D) Status unreadable -> the mode can't be echoed, so NO change-mode is sent;
#    only the baseline GET is tried and the result is an honest False.
r, sent = run({("GET", "device/reset-filter"): (200, None, "")}, ["Bad"], ok=False)
check("unreadable status -> False", r is False)
check("D: no change-mode sent when the mode is unknown",
      all(p != "device/change-mode" for _, p, _ in sent))
check("D: only reset-filter baseline contacted", only_allowed_paths(sent))
check("D: no DELETE", no_delete(sent))

# E) The verify budget bounds how many acknowledged calls we check.
check("verify budget is a small positive cap",
      isinstance(bridge.FILTER_RESET_MAX_VERIFIES, int)
      and 0 < bridge.FILTER_RESET_MAX_VERIFIES <= 20)


if __name__ == "__main__":
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    raise SystemExit(1 if FAIL else 0)
