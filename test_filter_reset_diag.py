"""Tests for the self-verifying filter-reset endpoint discovery (bridge.py).

device/reset-filter GET is a confirmed no-op; the official app resets over the
same REST API, so a working call exists at a different path or parameter shape.
_reset_filter walks a short list of filter-scoped variants and keeps the first
one that actually changes the real status. These tests drive that walk with a
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


# A) An alternative path clears via GET -> confirmed, and we stop before baseline.
r, contacted = run({("GET", "device/reset-filters"): (200, None, "")},
                   ["Bad", "Good"])
check("alt-path GET clears -> True", r is True)
check("first probe is the first alt path",
      contacted[0] == ("GET", "device/reset-filters", ("deviceSerialNumber",)))
check("stops before the baseline reset-filter call",
      ("GET", "device/reset-filter", ("deviceSerialNumber",)) not in contacted)
check("A: only filter-scoped paths", paths_are_filter_scoped(contacted))
check("A: no DELETE", no_delete(contacted))

# B) Everything is 404 or a 200 no-op -> honest False, baseline was tried.
r, contacted = run({("GET", "device/reset-filter"): (200, None, "")}, ["Bad"])
check("all no-op/404 -> False", r is False)
check("baseline reset-filter was contacted",
      any(p == "device/reset-filter" for _, p, _ in contacted))
check("B: only filter-scoped paths", paths_are_filter_scoped(contacted))
check("B: no DELETE", no_delete(contacted))

# C) An alt path answers 405/Allow: PUT, and PUT on that path clears -> True.
r, contacted = run({("GET", "device/filter-reset"): (405, "OPTIONS, PUT", ""),
                    ("PUT", "device/filter-reset"): (200, None, "")},
                   ["Bad", "Good"])
check("405 Allow=PUT then PUT clears -> True", r is True)
check("PUT was retried on the advertised path",
      ("PUT", "device/filter-reset", ("deviceSerialNumber",)) in contacted)
check("C: no DELETE", no_delete(contacted))

# D) Allow advertises DELETE -> it must never be sent.
r, contacted = run({("GET", "device/reset-filters"): (405, "OPTIONS, DELETE, GET", "")},
                   ["Bad"])
check("Allow lists DELETE -> still False", r is False)
check("DELETE from Allow is never sent", no_delete(contacted))

# E) A parameter-shape variant clears (deviceId) -> True; needs device.id present.
r, contacted = run({("GET", "device/reset-filter", ("deviceId",)): (200, None, "")},
                   ["Bad", "Good"], dev_id=42)
check("reset-filter deviceId variant clears -> True", r is True)
check("deviceId variant was contacted",
      ("GET", "device/reset-filter", ("deviceId",)) in contacted)
check("E: only filter-scoped paths", paths_are_filter_scoped(contacted))

# F) The verify budget bounds how many acknowledged calls we check.
check("verify budget is a small positive cap",
      isinstance(bridge.FILTER_RESET_MAX_VERIFIES, int)
      and 0 < bridge.FILTER_RESET_MAX_VERIFIES <= 20)


if __name__ == "__main__":
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    raise SystemExit(1 if FAIL else 0)
