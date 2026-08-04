"""Tests for the self-verifying filter-reset endpoint discovery (bridge.py).

Field result (1.6.3): only deviceSerialNumber resolves the device and GET
device/reset-filter returns HTTP 200 without clearing the counter; every other
path 404s and every other parameter name 400s. Since path and field names are
ruled out, 1.6.4 varies the HTTP *method* (a reset is a mutation, so the app
most likely POSTs/PUTs). _reset_filter walks the candidates and keeps the first
that actually changes the real status. These tests drive that walk with a
scripted _reset_request and assert both correctness and safety (only
filter-scoped paths are ever contacted; DELETE is never sent)."""
import asyncio
import bridge
from returns.result import Success

bridge.FILTER_RESET_VERIFY_DELAY = 0  # no real wait in tests

PASS, FAIL = [], []
def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok    " if cond else "  FAIL  ") + name)


class Dev:
    def __init__(self, statuses, dev_id=None):
        self.serial_number = "D1"
        self.id = dev_id
        self._st = list(statuses)
        self.api = object()  # presence enables the reset path

    async def status(self):
        fs = self._st.pop(0) if len(self._st) > 1 else self._st[0]
        return Success({"filters_status": fs})


def run(script, statuses, dev_id=None):
    """script maps (METHOD, path[, paramkeys]) -> (status, allow, body).

    Unlisted requests answer 404 (no such route), as the real server would."""
    b = bridge.AmbientikaBridge(bridge.BridgeConfig())
    contacted = []

    async def fake(self, device, method, path, params):
        m = method.upper()
        pk = tuple(sorted(params.keys()))
        contacted.append((m, path, pk))
        if (m, path, pk) in script:
            return script[(m, path, pk)]
        if (m, path) in script:
            return script[(m, path)]
        return (404, None, "")

    b._reset_request = fake.__get__(b, bridge.AmbientikaBridge)
    r = asyncio.run(b._reset_filter(Dev(statuses, dev_id)))
    return r, contacted


def paths_are_filter_scoped(contacted):
    return all("filter" in p for _, p, _ in contacted)

def no_delete(contacted):
    return all(m != "DELETE" for m, _, _ in contacted)


# A) POST on the resolved route clears the counter -> confirmed True, and POST is
#    tried before the GET baseline.
r, contacted = run({("POST", "device/reset-filter"): (200, None, "")},
                   ["Bad", "Good"])
check("POST clears -> True", r is True)
check("first probe is POST on the resolved route",
      contacted[0] == ("POST", "device/reset-filter", ("deviceSerialNumber",)))
check("A: only filter-scoped paths", paths_are_filter_scoped(contacted))
check("A: no DELETE", no_delete(contacted))

# B) Only the GET baseline answers (200 no-op), everything else 404 -> honest
#    False, and the GET baseline was tried last.
r, contacted = run({("GET", "device/reset-filter"): (200, None, "")}, ["Bad"])
check("all no-op/404 -> False", r is False)
check("GET baseline was contacted",
      ("GET", "device/reset-filter", ("deviceSerialNumber",)) in contacted)
check("GET baseline is contacted last",
      contacted[-1] == ("GET", "device/reset-filter", ("deviceSerialNumber",)))
check("B: only filter-scoped paths", paths_are_filter_scoped(contacted))
check("B: no DELETE", no_delete(contacted))

# C) POST answers 405/Allow: PUT, and PUT on that route clears -> True.
r, contacted = run({("POST", "device/reset-filter"): (405, "OPTIONS, PUT", ""),
                    ("PUT", "device/reset-filter"): (200, None, "")},
                   ["Bad", "Good"])
check("405 Allow=PUT then PUT clears -> True", r is True)
check("PUT was retried on the advertised route",
      ("PUT", "device/reset-filter", ("deviceSerialNumber",)) in contacted)
check("C: no DELETE", no_delete(contacted))

# D) Allow advertises DELETE -> it must never be sent.
r, contacted = run({("POST", "device/reset-filter"): (405, "OPTIONS, DELETE, GET", "")},
                   ["Bad"])
check("Allow lists DELETE -> still False", r is False)
check("DELETE from Allow is never sent", no_delete(contacted))

# E) The deviceSerialNumber+deviceId shape clears via POST -> True (needs id).
r, contacted = run({("POST", "device/reset-filter", ("deviceId", "deviceSerialNumber")):
                    (200, None, "")}, ["Bad", "Good"], dev_id=42)
check("POST deviceSerialNumber+deviceId clears -> True", r is True)
check("the serial+deviceId shape was contacted",
      ("POST", "device/reset-filter", ("deviceId", "deviceSerialNumber")) in contacted)
check("E: only filter-scoped paths", paths_are_filter_scoped(contacted))

# F) The verify budget bounds how many acknowledged calls we check.
check("verify budget is a small positive cap",
      isinstance(bridge.FILTER_RESET_MAX_VERIFIES, int)
      and 0 < bridge.FILTER_RESET_MAX_VERIFIES <= 20)


if __name__ == "__main__":
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    raise SystemExit(1 if FAIL else 0)
