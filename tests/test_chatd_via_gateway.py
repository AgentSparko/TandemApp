"""SRV-843: the chatd suite (tests/test_chatd.py) with chatd off the engine host: every test of it
runs with chatd's engine at a controller's `/chat-engine` (proxy/chatengine.py) and the fake
engine behind that controller as a TandemLLM backend on another host that wants its HTTP token
(tests/chatd_util.py THE GATEWAY MODE). chatd never reaches the fake but through the gateway.

The few tests that watch what only a direct engine does are listed in DIRECT_ONLY with the reason
the gateway changes it; they run in tests/test_chatd.py as before.

CPU only. Run: python tests/test_chatd_via_gateway.py
"""

from __future__ import annotations

import chatd_util

chatd_util.GATEWAY.on = True

import test_chatd as suite  # noqa: E402

#: test -> why it watches the engine itself, not chatd through the gateway
DIRECT_ONLY: dict[str, str] = {}


def main() -> None:
    import traceback
    passed, direct, failed = 0, 0, []
    for name, fn in sorted(vars(suite).items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        if name in DIRECT_ONLY:
            print(f"  {name:64s} direct only: {DIRECT_ONLY[name]}")
            direct += 1
            continue
        try:
            fn()
        except Exception:                                          # noqa: BLE001
            print(f"  {name:64s} FAIL")
            traceback.print_exc()
            failed.append(name)
            continue
        print(f"  {name:64s} ok")
        passed += 1
    if failed:
        raise SystemExit(f"{len(failed)} FAILED: {', '.join(failed)}")
    assert passed >= 40, passed
    print(f"{passed} passed ({direct} direct only)")


if __name__ == "__main__":
    main()
