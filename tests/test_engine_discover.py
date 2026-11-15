"""SRV-827: Add engine lists what the LAN advertises and takes an address (proxy/engineonboard.py
`discover` and `probe`, proxy/mdns.py's Browser), against the pinned contract and fakes.

The LAN is loopback: tests/fake_engine.py's MdnsResponder answers a browse with bytes (the engine's
own from contracts/engine-pair-v1/fixtures/mdns.json, or an advertisement made here in the engine's
format), and the Browser's socket sends its multicast query to the responders instead
(`loop_socket`). Nothing is sent to 224.0.0.251 and nothing binds 5353.

  1  the browse is the contract's: the query this controller sends is fixtures/mdns.json's `query`
     byte for byte, and its `answer` reads as its `advert` (name, version, port, fp, addresses)
                                                                  test_the_wire_is_the_contract
  Gherkin (SRV-827, plan 13 item 7):
  2  Given an advertised engine on the LAN, When the user opens Add engine, Then it is listed with
     its name and fingerprint (and version, port, addresses; never `verified`; an engine already in
     the fleet names its node); two engines are two rows; a TXT key beyond the four is not passed
     on; GET answers the last browse                             test_advertised_engine_is_listed
  3  Given no engine is found, Then the user can type an address, a tailnet one included: the probe
     reads the window there (open, tries, fingerprint) and pairing starts at that address; a name
     that does not resolve, a closed port, an old engine and no window each answer their finding
     in a 200                                                     test_no_engine_found_type_an_address
  4  the LAN refused (macOS's Local Network permission off) or failing: the answer says so with its
     finding, and the address field still works; bad bodies are 400  test_lan_denied_and_bad_bodies

    python tests/test_engine_discover.py
"""

from __future__ import annotations

import errno
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fake_engine  # noqa: E402
from onboard_rig import ok, rig  # noqa: E402
from proxy import engineonboard, enginepair, mdns  # noqa: E402

MDNS = os.path.join(ROOT, "contracts", "engine-pair-v1", "fixtures", "mdns.json")


def _fixture() -> dict:
    with open(MDNS) as f:
        return json.load(f)


def advert_bytes(name: str, port: int, fp: str, version: str = "0.15.4",
                 address: str = "127.0.0.1", extra: dict | None = None) -> bytes:
    """An engine's answer to a browse, in its format (contracts/engine-pair-v1 README)."""
    inst, target = f"{name}._tandem._tcp.local", f"{name}-tandem.local"
    txt = {"name": name, "version": version, "port": str(port), "fp": fp, **(extra or {})}
    return mdns.pack(id_=0, flags=0x8400, answers=[
        mdns.Record("_tandem._tcp.local", mdns.PTR, inst, ttl=4500)], additional=[
        mdns.Record(inst, mdns.SRV, (0, 0, port, target), ttl=120, flush=True),
        mdns.Record(inst, mdns.TXT, txt, ttl=4500, flush=True),
        mdns.Record(target, mdns.A, address, ttl=120, flush=True)])


def _browser(*responders):
    return mdns.Browser(sock=fake_engine.loop_socket([r.addr for r in responders]))


def test_the_wire_is_the_contract():
    fx = _fixture()
    resp = fake_engine.MdnsResponder(bytes.fromhex(fx["answer"])).start()
    try:
        got = _browser(resp).browse([engineonboard.SERVICE], 0.4)
    finally:
        resp.stop()
    assert resp.queries and resp.queries[0] == bytes.fromhex(fx["query"]), resp.queries[:1]
    assert len(got) == 1, got
    svc = got[0]
    adv = fx["advert"]
    assert svc["txt"] == dict(item.split("=", 1) for item in fx["txt"]), svc["txt"]
    assert (svc["txt"]["name"], svc["txt"]["version"], int(svc["txt"]["port"]), svc["txt"]["fp"]) == \
        (adv["name"], adv["version"], adv["port"], adv["fp"])
    assert svc["addresses"] == adv["addresses"] and svc["port"] == adv["port"]
    return "query byte-equal, answer read as the fixture's advert"


def test_advertised_engine_is_listed(tmp_path, monkeypatch):
    fx = _fixture()
    with rig(tmp_path, monkeypatch) as r:
        engine_fx = fake_engine.MdnsResponder(bytes.fromhex(fx["answer"])).start()
        ours = fake_engine.MdnsResponder(advert_bytes(
            "engine-b", r.engine.port, r.host_fp, extra={"token": "leak-me", "code": "123456"})).start()
        try:
            onb = r.fleet.engines()
            onb.browser = _browser(engine_fx, ours)
            st, doc = r.get("/v1/fleet/engines/discover")
            assert st == 200 and ok(doc, "engine-discover.schema.json")["lan"] == "not_run"
            st, doc = r.post("/v1/fleet/engines/discover", {"seconds": 0.5})
            assert st == 200, doc
            ok(doc, "engine-discover.schema.json")
            assert doc["lan"] == "ok" and doc["finding"] is None
            rows = {e["name"]: e for e in doc["engines"]}
            assert set(rows) == {"enginehost", "engine-b"}, rows
            a = rows["enginehost"]
            assert (a["fingerprint"], a["version"], a["port"], a["addresses"], a["address"]) == \
                (fx["advert"]["fp"], "0.15.4", 8000, ["192.0.2.10"], "192.0.2.10")
            assert a["verified"] is False and a["node"] is None
            b = rows["engine-b"]
            assert (b["fingerprint"], b["port"], b["address"]) == (r.host_fp, r.engine.port, "127.0.0.1")
            assert "leak-me" not in json.dumps(doc) and "123456" not in json.dumps(doc)
            # the row pairs: its address, port and fingerprint go to pair/start
            code = r.engine.open()
            p = r.start(address=b["address"], port=b["port"], fingerprint=b["fingerprint"],
                        name=b["name"])
            assert p["advertised"] == b["fingerprint"] == p["fingerprint"]
            st, done = r.post("/v1/fleet/engines/pair/code", {"pairing": p["pairing"], "code": code})
            assert st == 200 and done["state"] == "paired", done
            assert r.cfg.node(done["engine"]["id"])["alias"] == "engine-b"
            # browsed again: the engine already in the fleet names its node
            st, doc = r.post("/v1/fleet/engines/discover", {"seconds": 0.5})
            b = next(e for e in doc["engines"] if e["name"] == "engine-b")
            assert b["node"] == done["engine"]["id"] and b["alias"] == "engine-b"
            st, last = r.get("/v1/fleet/engines/discover")
            assert st == 200 and last["engines"] == doc["engines"]
        finally:
            engine_fx.stop()
            ours.stop()
    return "listed with name, fingerprint, version, port, addresses; paired from its row"


def test_no_engine_found_type_an_address(tmp_path, monkeypatch):
    with rig(tmp_path, monkeypatch) as r:
        onb = r.fleet.engines()
        silent = fake_engine.MdnsResponder(b"", service=b"\xff-nothing-matches").start()
        try:
            onb.browser = _browser(silent)
            st, doc = r.post("/v1/fleet/engines/discover", {"seconds": 0.3})
            assert st == 200 and doc["engines"] == [] and doc["lan"] == "ok", doc
        finally:
            silent.stop()
        # the address typed: the probe reads the window there
        r.engine.open()
        st, doc = r.post("/v1/fleet/engines/probe", {"address": "127.0.0.1", "port": r.engine.port})
        assert st == 200, doc
        ok(doc, "engine-probe.schema.json")
        assert doc["reachable"] and doc["engine"] and doc["finding"] is None
        assert doc["pairing"]["open"] and doc["pairing"]["tries_left"] == 5
        assert doc["fingerprint"] == r.host_fp and doc["node"] is None
        # a tailnet name and a tailnet address are addresses like any other: pairing starts there
        seen = []

        def window(address, port):
            seen.append((address, port))
            return enginepair.Window("127.0.0.1", r.engine.port)
        onb.window = window
        for addr in ("engine-a.tail1234.ts.net", "100.101.102.103"):
            st, doc = r.post("/v1/fleet/engines/probe", {"address": addr, "port": 8000})
            assert st == 200 and doc["address"] == addr and doc["engine"], doc
            p = r.start(address=addr, port=8000)
            assert p["address"] == addr and p["fingerprint"] == r.host_fp
        assert seen == [("engine-a.tail1234.ts.net", 8000)] * 2 + [("100.101.102.103", 8000)] * 2
        onb.window = lambda a, p: enginepair.Window(a, p, timeout=3.0)
        # nothing there: a name that does not resolve, a port nobody listens on
        st, doc = r.post("/v1/fleet/engines/probe", {"address": "engine.invalid", "port": 8000})
        assert st == 200 and not doc["reachable"] and doc["finding"]["code"] == "unreachable", doc
        assert doc["finding"]["address"] == "engine.invalid" and doc["finding"]["port"] == 8000
        assert "engine.invalid:8000" in doc["finding"]["message"]
        ok(doc["finding"], "engine-finding.schema.json")
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
        s.close()
        st, doc = r.post("/v1/fleet/engines/probe", {"address": "127.0.0.1", "port": dead})
        assert st == 200 and doc["finding"]["code"] == "unreachable" and not doc["reachable"]
        # no window: the finding says to run `tandem pair`; an old engine says to update
        r.engine.close()
        st, doc = r.post("/v1/fleet/engines/probe", {"address": "127.0.0.1", "port": r.engine.port})
        assert st == 200 and doc["reachable"] and doc["finding"]["code"] == "no_window", doc
        assert "tandem pair" in doc["finding"]["fix"]
        r.engine.pair_routes = False
        st, doc = r.post("/v1/fleet/engines/probe", {"address": "127.0.0.1", "port": r.engine.port})
        assert st == 200 and doc["engine"] and doc["finding"]["code"] == "pairing_unsupported", doc
    return "empty LAN, address probe, tailnet names and addresses, nothing there, no window, old"


def test_lan_denied_and_bad_bodies(tmp_path, monkeypatch):
    class Refused:
        def __init__(self, err):
            self.err = err

        def sendto(self, data, addr):
            raise OSError(self.err, os.strerror(self.err))

        def settimeout(self, t):
            pass

        def recvfrom(self, n):
            raise BlockingIOError()

        def close(self):
            pass

    with rig(tmp_path, monkeypatch) as r:
        onb = r.fleet.engines()
        onb.browser = mdns.Browser(sock=lambda _i=None: Refused(errno.EHOSTUNREACH), platform="darwin")
        st, doc = r.post("/v1/fleet/engines/discover", {})
        assert st == 200 and doc["lan"] == "denied" and doc["engines"] == [], doc
        f = ok(doc["finding"], "engine-finding.schema.json")
        assert f["code"] == "lan_denied" and "Local Network" in f["fix"] and "address" in f["fix"]
        onb.browser = mdns.Browser(sock=lambda _i=None: Refused(errno.ENETUNREACH), platform="linux")
        st, doc = r.post("/v1/fleet/engines/discover", {"seconds": 0.3})
        assert st == 200 and doc["lan"] == "failed" and doc["finding"]["code"] == "lan_failed", doc
        # the address still works
        r.engine.open()
        st, doc = r.post("/v1/fleet/engines/probe", {"address": "127.0.0.1", "port": r.engine.port})
        assert st == 200 and doc["pairing"]["open"]
        for path, body in (("discover", {"seconds": 9}), ("discover", {"seconds": "2"}),
                           ("discover", {"interface": "eth0"}), ("discover", {"x": 1}),
                           ("probe", {}), ("probe", {"address": "-oProxyCommand=sh"}),
                           ("probe", {"address": "a;b"}), ("probe", {"address": "h", "port": 70000})):
            st, doc = r.post(f"/v1/fleet/engines/{path}", body)
            assert st == 400 and doc["error"]["code"] == "bad_request", (path, body, doc)
    return "denied and failed LANs answer their finding; bad bodies 400"


if __name__ == "__main__":
    from scriptmode import run
    run(globals())
