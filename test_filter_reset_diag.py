"""Tests for the self-verifying, Allow-header-driven filter reset (bridge.py)."""
import asyncio
import bridge
from returns.result import Success

bridge.FILTER_RESET_VERIFY_DELAY = 0  # no real wait in tests

PASS, FAIL = [], []
def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok    " if cond else "  FAIL  ") + name)


class Dev:
    def __init__(self, statuses):
        self.serial_number = "D1"
        self._st = list(statuses)
        self.api = object()  # presence enables the reset path

    async def status(self):
        fs = self._st.pop(0) if len(self._st) > 1 else self._st[0]
        return Success({"filters_status": fs})


def run(script, statuses):
    b = bridge.AmbientikaBridge(bridge.BridgeConfig())
    calls = []

    async def fake(self, device, method):
        calls.append(method.upper())
        return script.get(method.upper(), (None, None))

    b._reset_request = fake.__get__(b, bridge.AmbientikaBridge)
    r = asyncio.run(b._reset_filter(Dev(statuses)))
    return r, calls


# A) Allow names PUT, PUT clears the counter -> confirmed
r, calls = run({"POST": (405, "OPTIONS, PUT"), "PUT": (200, None)}, ["Bad", "Good"])
check("Allow=PUT, PUT clears -> True", r is True)
check("order POST then PUT", calls[:2] == ["POST", "PUT"])
check("no DELETE (A)", "DELETE" not in calls)

# B) only GET allowed and it is a no-op -> honest False
r, calls = run({"POST": (405, "OPTIONS, GET"), "GET": (200, None)}, ["Bad", "Bad"])
check("GET no-op -> False", r is False)

# C) no Allow header -> PUT/PATCH probe, PATCH clears -> True
r, calls = run({"POST": (405, None), "PUT": (405, None), "PATCH": (200, None)}, ["Bad", "Good"])
check("no Allow -> PATCH probe clears -> True", r is True and "PATCH" in calls)

# D) Allow lists DELETE -> must never be sent
r, calls = run({"POST": (405, "OPTIONS, DELETE"), "GET": (200, None)}, ["Bad", "Bad"])
check("DELETE from Allow is never sent", "DELETE" not in calls)


if __name__ == "__main__":
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    raise SystemExit(1 if FAIL else 0)
