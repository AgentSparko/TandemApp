"""SRV-829: pair an engine host from the page, click and type the code (proxy/enginepair.py,
proxy/engineonboard.py, proxy/enginecodes.py), against the pinned contract and fakes.

The engine is tests/fake_engine.py: its pairing window written again from
contracts/engine-pair-v1's README and held here to the contract's fixtures byte for byte. The
engine host's SSH and `tandem remote` are tests/fake_ssh.py and tests/fake_tandem_remote.py
(tests/onboard_rig.py puts the three together). No network beyond loopback, no sshd, no engine.

  1  the shared math: fixtures/spake2.json's runs (the engine's identities and the node's) replay
     through proxy/pairing.py; the group, M, N and the AAD are the engine's    test_spake2_fixtures
  2  the exchange: fixtures/exchange.json's requests come out of TandemApp's side byte for byte
     from its x, its plaintext out of the seal; the fake engine, fed the fixture's y, session,
     nonce and token, answers the fixture's bytes; every message holds to its pinned schema; the
     fixture's swapped host key fails                               test_exchange_fixture
  Gherkin (SRV-829, plan 13 item 8):
  3  Given a found engine and its code, When the user types the code, Then TandemApp's key is
     installed (the forced-command line), the host key pinned, the HTTP token stored sealed, And
     the engine's status is shown (status over SSH ran)               test_click_type_the_code_done
  4  When the code is wrong Then the page says so (wrong_code, the tries left) and how to get a
     new one; five wrong codes spend it (code_spent: run `tandem pair` again); an expired code and
     no window the same; nothing stored in any of them              test_wrong_code_says_so
  5  a host key swapped on the way: alone (its fingerprint does not match) or with a consistent
     fingerprint (cB fails as a wrong code); an advertisement that disagrees with the window;
     nothing stored, nothing installed                               test_swapped_host_key
  6  pairing again replaces the key pair and keeps the id, alias and node; abandon forgets a
     started pairing; a pairing id that is unknown is not_found; an engine before 1.9 and
     something that is not an engine are told apart                test_again_abandon_and_old
  7  every code has its sentence and fix and the page's table is the code's   test_codes_table
  8  no pairing code, private key or HTTP token in any log line, answer, error, audit row, event,
     fleet record, ledger row, fake ssh or engine record, or the controller's file bytes
                                                                    test_no_secret_anywhere

    python tests/test_engine_pair.py
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fake_engine  # noqa: E402
from onboard_rig import ok, pem_lines, rig  # noqa: E402
from proxy import enginecodes, enginectl, enginepair, pairing  # noqa: E402
from proxy import schema as schema_mod  # noqa: E402

PAIR_DIR = os.path.join(ROOT, "contracts", "engine-pair-v1")


def _fixture(name: str) -> dict:
    with open(os.path.join(PAIR_DIR, "fixtures", name)) as f:
        return json.load(f)


def pair_ok(doc, name: str) -> dict:
    with open(os.path.join(PAIR_DIR, name)) as f:
        errs = schema_mod.validate(doc, json.load(f))
    assert not errs, (name, errs[:5], doc)
    return doc


# ---------------------------------------------------------------------------- 1, 2 the fixtures

def test_spake2_fixtures():
    fx = _fixture("spake2.json")
    g = fx["group"]
    assert (int(g["p"], 16), int(g["q"], 16), g["g"]) == (pairing.P, pairing.Q, pairing.G)
    assert (int(g["M"], 16), int(g["N"], 16)) == (pairing.M, pairing.N)
    assert g["aad"].encode() == pairing.AAD and g["elem_bytes"] == pairing.ELEM_BYTES
    k = fx["keys"]
    assert enginepair.fingerprint(k["app"].split()[1]) == k["app_fingerprint"]
    assert enginepair.fingerprint(k["host"].split()[1]) == k["host_fingerprint"]
    runs = fx["engine"] + fx["node_identities"]
    for r in runs:
        x, y, w = int(r["x"], 16), int(r["y"], 16), pairing.w_of(r["code"])
        assert format(w, "x") == r["w"], r["code"]
        pa = pow(pairing.G, x, pairing.P) * pow(pairing.M, w, pairing.P) % pairing.P
        pb = pow(pairing.G, y, pairing.P) * pow(pairing.N, w, pairing.P) % pairing.P
        assert (format(pa, "x"), format(pb, "x")) == (r["pA"], r["pB"])
        ka = pow(pb * pow(pairing.N, pairing.Q - w, pairing.P) % pairing.P, x, pairing.P)
        kb = pow(pa * pow(pairing.M, pairing.Q - w, pairing.P) % pairing.P, y, pairing.P)
        assert ka == kb and format(ka, "x") == r["K"]
        kca, kcb, tt = pairing.run_keys(r["ida"], r["idb"], pa, pb, ka, w)
        assert (kca.hex(), kcb.hex(), tt.hex()) == (r["KcA"], r["KcB"], r["TT"])
        assert pairing.confirm_mac(kca, tt) == r["cA"] and pairing.confirm_mac(kcb, tt) == r["cB"]
        assert pairing.ke_of(tt).hex() == r["Ke"]
    eng = fx["engine"][0]
    assert eng["ida"].startswith("tandemapp-key SHA256:") and eng["idb"].startswith("tandemengine ")
    node = fx["node_identities"][0]
    assert node["ida"] == pairing.identity_a(node["ida"].split(":", 1)[1])
    assert node["idb"] == pairing.identity_b(node["idb"].split(":", 1)[1])
    for r in fx["engine"]:
        fp, kid = r["ida"].split()[1:]
        assert r["ida"] == enginepair.identity_a(fp, kid) and r["idb"] == enginepair.identity_b(
            k["host_fingerprint"])
    return f"{len(runs)} runs replayed, node identities included"


def test_exchange_fixture():
    fx = _fixture("exchange.json")
    r1, a1 = fx["step1"]["request"], fx["step1"]["answer"]
    r2, a2 = fx["step2"]["request"], fx["step2"]["answer"]
    for doc, name in ((r1, "step1-request"), (a1, "step1"), (r2, "step2-request"), (a2, "step2"),
                      (json.loads(fx["plaintext"]), "answer")):
        pair_ok(doc, f"{name}.schema.json")
    # TandemApp's side, from the fixture's x
    ex = enginepair.Exchange(fx["code"], r1["public_key"], r1["key_id"], name=r1["name"],
                             x=int(fx["x"], 16))
    assert ex.request1() == r1
    assert ex.check1(a1, expect=a1["fingerprint"]) == r2
    plain = ex.open2(a2)
    assert json.dumps(plain, sort_keys=True, separators=(",", ":")) == fx["plaintext"]
    assert pairing.ke_of(ex._tt).hex() == fx["transcript"]["Ke"]
    assert enginepair.identity_a(ex.key_fp, ex.key_id) == fx["transcript"]["ida"]
    # the fake engine, from the fixture's y, session, nonce and token, answers the same bytes
    doc = json.loads(fx["plaintext"])
    eng = fake_engine.FakeEngine(a1["host_key"], ssh_user=doc["ssh_user"],
                                 engine_dir="/home/u/TandemLLM", version=doc["engine_version"],
                                 mode=doc["mode"], y=int(fx["y"], 16), session=a1["session"],
                                 nonce=bytes.fromhex(a2["sealed"]["nonce"]), token=doc["http_token"])
    eng.open(fx["code"])
    assert eng.step1(r1) == a1
    assert eng.step2(r2) == a2
    assert eng.status()["state"] == "paired"
    # the seal: TandemApp's seal makes the engine's bytes; a changed byte does not open
    assert pairing.seal(pairing.ke_of(ex._tt), fx["plaintext"].encode(),
                        bytes.fromhex(a2["sealed"]["nonce"])) == a2["sealed"]
    bad = dict(a2["sealed"], box=("0" if a2["sealed"]["box"][0] != "0" else "1") + a2["sealed"]["box"][1:])
    try:
        pairing.open_seal(pairing.ke_of(ex._tt), bad)
        raise AssertionError("a changed box opened")
    except ValueError:
        pass
    # the fixture's swapped host key: TandemApp's check fails before anything is trusted
    for swap, code in (({"host_key": fx["swapped_host_key"]["host_key"]}, "host_key_mismatch"),
                       ({"host_key": fx["swapped_host_key"]["host_key"],
                         "fingerprint": enginepair.fingerprint(fx["swapped_host_key"]["host_key"].split()[1])},
                        "wrong_code")):
        ex = enginepair.Exchange(fx["code"], r1["public_key"], r1["key_id"], name=r1["name"],
                                 x=int(fx["x"], 16))
        try:
            ex.check1(dict(a1, **swap))
            raise AssertionError("a swapped host key passed")
        except enginecodes.OnboardError as exc:
            assert exc.code == code, (exc.code, code)
        assert ex._tt is None and ex.host_key is None
    return "requests, answers and plaintext byte-equal both ways; the swap refused"


# ---------------------------------------------------------------------------- 3 the happy path

def test_click_type_the_code_done(tmp_path, monkeypatch):
    with rig(tmp_path, monkeypatch) as r:
        code = r.engine.open()
        p = r.start(name="engine-a")
        assert p["window"]["open"] and p["window"]["tries_left"] == 5 and p["fingerprint"] == r.host_fp
        assert r.lines() == [], "nothing is installed before the code"
        st, doc = r.post("/v1/fleet/engines/pair/code", {"pairing": p["pairing"], "code": code})
        assert st == 200, doc
        ok(doc, "engine-paired.schema.json")
        assert doc["state"] == "paired" and doc["via"] == "code" and doc["finding"] is None
        eng = ok(dict(doc["engine"], contract_version="1.0"), "engine.schema.json")
        hid = eng["id"]
        # the key installed with the forced command
        lines = r.lines()
        assert len(lines) == 1 and lines[0].startswith(
            f'command="/bin/bash {r.engine_dir}/bin/tandem remote --key-id {p["key_id"]}",no-pty,')
        assert r.cfg.host_key(hid)["key_id"] == p["key_id"]
        assert r.cfg.host_key(hid)["public"].split()[1] in lines[0]
        # the host key pinned, from the code
        assert enginectl.pinned_key(r.fleet.trust().root, hid) == (r.host_type, r.host_b64)
        assert eng["host_key"] == r.host_fp and eng["host_key_type"] == r.host_type
        # the HTTP token stored sealed: the engine's own, a hint in the answer
        assert r.cfg.engine_token(hid) in r.engine.tokens
        assert eng["http"]["token"]["set"] and eng["http"]["port"] == r.engine.port
        # the fleet node: transport ssh, its trust
        row = r.cfg.node(hid)
        assert row["transport"] == "ssh" and row["alias"] == "engine-a"
        t = row["trust"]
        assert (t["kind"], t["via"], t["host"], t["user"], t["port"], t["key_id"]) == \
            ("ssh", "code", "127.0.0.1", "tandem", 22, p["key_id"])
        ok(doc["node"], "node.schema.json")
        assert doc["node"]["pairing"]["state"] == "paired" and doc["node"]["transport"] == "ssh"
        # the engine's status is shown: `status` ran over SSH with the new key
        assert eng["status"]["ok"] and eng["status"]["verb"] == "status"
        assert [c["tokens"] for c in r.calls()] == [["status"]]
        assert r.engine.status()["state"] == "paired"
        # the audit row and the engine host's list and view
        a = [x for x in r.cfg.audit(50) if x["action"] == "pair" and x["object_id"] == hid]
        assert a and a[0]["detail"]["via"] == "code" and a[0]["detail"]["host_key"] == r.host_fp
        st, lst = r.get("/v1/fleet/engines")
        assert st == 200 and [e["id"] for e in ok(lst, "engines.schema.json")["engines"]] == [hid]
        st, one = r.get(f"/v1/fleet/engines/{hid}")
        assert st == 200 and ok(one, "engine.schema.json")["status"]["ok"]
        # a fleet node that is not an engine host over SSH, and one that does not exist
        r.cfg.add_node({"id": "n-lan", "alias": "a node", "endpoint": "https://192.0.2.9:7701",
                        "address": "192.0.2.9", "trust": {}})
        st, err = r.get("/v1/fleet/engines/n-lan")
        assert st == 409 and err["error"]["code"] == "not_ssh"
        st, err = r.get("/v1/fleet/engines/eh-nothing")
        assert st == 404 and err["error"]["code"] == "not_found"
    return "key installed, host key pinned, token sealed, status over SSH shown"


# ---------------------------------------------------------------------------- 4 a wrong code

def _nothing_stored(r) -> None:
    assert r.ssh_nodes() == [], r.ssh_nodes()
    assert r.lines() == []
    root = os.path.join(r.fleet.trust().root, "ssh", "hosts")
    assert not os.path.isdir(root) or os.listdir(root) == []
    con = __import__("sqlite3").connect(r.cfg.path)
    assert con.execute("SELECT COUNT(*) FROM engine_host_keys").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM engine_http_tokens").fetchone()[0] == 0
    con.close()


def test_wrong_code_says_so(tmp_path, monkeypatch):
    with rig(tmp_path, monkeypatch) as r:
        code = r.engine.open()
        wrong = f"{(int(code) + 1) % 10 ** 6:06d}"
        p = r.start()
        for left in (4, 3, 2, 1):
            st, doc = r.post("/v1/fleet/engines/pair/code", {"pairing": p["pairing"], "code": wrong})
            e = doc["error"]
            assert st == 403 and e["code"] == "wrong_code" and e["tries_left"] == left, doc
            assert e["message"] == f"The code does not match: {left} tries left."
            assert "tandem pair" in e["fix"]
            assert r.engine.status()["tries_left"] == left
        st, doc = r.post("/v1/fleet/engines/pair/code", {"pairing": p["pairing"], "code": wrong})
        assert st == 410 and doc["error"]["code"] == "code_spent", doc
        assert "`tandem pair` on the engine host again" in doc["error"]["fix"]
        # the right code no longer helps
        st, doc = r.post("/v1/fleet/engines/pair/code", {"pairing": p["pairing"], "code": code})
        assert st == 410 and doc["error"]["code"] == "code_spent", doc
        _nothing_stored(r)
        # starting again on the spent window says how to get a new code
        st, doc = r.post("/v1/fleet/engines/pair/start", {"address": "127.0.0.1", "port": r.engine.port})
        assert st in (409, 410) and doc["error"]["code"] in ("code_spent", "no_window"), doc
        assert "tandem pair" in doc["error"]["fix"]
        # not 6 digits
        r.engine.open()
        p = r.start()
        for bad in ("12345", "abcdef", 123456, "1234567"):
            st, doc = r.post("/v1/fleet/engines/pair/code", {"pairing": p["pairing"], "code": bad})
            assert st == 400 and doc["error"]["code"] in ("bad_code", "bad_request"), (bad, doc)
        assert r.engine.status()["tries_left"] == 5, "a malformed code spends no try"
        # an expired code
        now = [0.0]
        r.engine.clock = lambda: 1e9 + now[0]
        code = r.engine.open()
        p = r.start()
        now[0] = 301.0
        st, doc = r.post("/v1/fleet/engines/pair/code", {"pairing": p["pairing"], "code": code})
        assert st == 410 and doc["error"]["code"] == "code_spent", doc
        # no window at all: start says to run `tandem pair`
        r.engine.window = None
        st, doc = r.post("/v1/fleet/engines/pair/start", {"address": "127.0.0.1", "port": r.engine.port})
        assert st == 409 and doc["error"]["code"] == "no_window", doc
        assert doc["error"]["fix"] == "Run `tandem pair` on the engine host and type the code it prints."
        # a closed window answers the same
        r.engine.clock = __import__("time").time
        r.engine.open()
        p = r.start()
        r.engine.close()
        st, doc = r.post("/v1/fleet/engines/pair/code", {"pairing": p["pairing"], "code": "000000"})
        assert st == 410 and doc["error"]["code"] == "code_spent", doc
        _nothing_stored(r)
    return "wrong code with tries left, spent after five, expired and no window; nothing stored"


# ---------------------------------------------------------------------------- 5 a swapped host key

def test_swapped_host_key(tmp_path, monkeypatch):
    from proxy import hostkeys
    with rig(tmp_path, monkeypatch) as r:
        _pem, other = hostkeys.generate()
        other_fp = enginepair.fingerprint(other.split()[1])
        code = r.engine.open()
        p = r.start()
        r.engine.swap = {"host_key": other}
        st, doc = r.post("/v1/fleet/engines/pair/code", {"pairing": p["pairing"], "code": code})
        e = doc["error"]
        assert st == 409 and e["code"] == "host_key_mismatch", doc
        assert e["expected"] == r.host_fp and e["offered"] == other_fp
        st, doc = r.post("/v1/fleet/engines/pair/code", {"pairing": p["pairing"], "code": code})
        assert st == 404 and doc["error"]["code"] == "not_found", "a mismatch forgets the pairing"
        # a consistent swap (the fingerprint too): the code's confirmation fails
        p = r.start()
        r.engine.swap = {"host_key": other, "fingerprint": other_fp}
        st, doc = r.post("/v1/fleet/engines/pair/code", {"pairing": p["pairing"], "code": code})
        assert st == 409 and doc["error"]["code"] == "host_key_mismatch", doc
        # ... and with no fingerprint seen before (a window read that was swapped too): the code's
        # confirmation fails, as a wrong code does
        pid = r.start()["pairing"]
        r.fleet.engines()._get(pid, "pair")["expect"] = None
        st, doc = r.post("/v1/fleet/engines/pair/code", {"pairing": pid, "code": code})
        assert st == 403 and doc["error"]["code"] == "wrong_code", doc
        assert r.engine.status()["tries_left"] == 2
        r.engine.swap = {}
        _nothing_stored(r)
        # an advertisement that disagrees with the window
        st, doc = r.post("/v1/fleet/engines/pair/start", {"address": "127.0.0.1", "port": r.engine.port,
                                                          "fingerprint": other_fp})
        assert st == 409 and doc["error"]["code"] == "host_key_mismatch", doc
        assert doc["error"]["expected"] == other_fp and doc["error"]["offered"] == r.host_fp
        _nothing_stored(r)
    return "swapped alone, swapped consistently, advertised another: nothing stored"


# ---------------------------------------------------------------------------- 6 again, abandon, old

def test_again_abandon_and_old(tmp_path, monkeypatch):
    with rig(tmp_path, monkeypatch) as r:
        first = r.pair()
        hid = first["engine"]["id"]
        k1 = r.cfg.host_key(hid)["key_id"]
        r.cfg.update_node(hid, {"alias": "kept-alias"})
        second = r.pair()
        assert second["engine"]["id"] == hid and r.cfg.node(hid)["alias"] == "kept-alias"
        assert r.cfg.host_key(hid)["key_id"] != k1 and len(r.ssh_nodes()) == 1
        assert len(r.lines()) == 2
        # abandon
        r.engine.open()
        p = r.start()
        st, doc = r.post("/v1/fleet/engines/pair/abandon", {"pairing": p["pairing"]})
        assert st == 200 and ok(doc, "engine-pair-abandon.schema.json")["abandoned"] is True
        st, doc = r.post("/v1/fleet/engines/pair/code", {"pairing": p["pairing"], "code": "123456"})
        assert st == 404 and doc["error"]["code"] == "not_found"
        st, doc = r.post("/v1/fleet/engines/pair/abandon", {"pairing": p["pairing"]})
        assert st == 200 and doc["abandoned"] is False
        # bad bodies
        for body in ({}, {"address": "-oProxyCommand=x"}, {"address": "a b"},
                     {"address": "127.0.0.1", "port": 0}, {"address": "127.0.0.1", "extra": 1},
                     {"address": "127.0.0.1", "fingerprint": "MD5:xx"}):
            st, doc = r.post("/v1/fleet/engines/pair/start", body)
            assert st == 400 and doc["error"]["code"] == "bad_request", (body, doc)
        # an engine before 1.9: no pairing routes, but its /health answers
        r.engine.pair_routes = False
        st, doc = r.post("/v1/fleet/engines/pair/start", {"address": "127.0.0.1", "port": r.engine.port})
        assert st == 409 and doc["error"]["code"] == "pairing_unsupported", doc
        # something that is not an engine, and nothing at all
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
        s.close()
        st, doc = r.post("/v1/fleet/engines/pair/start", {"address": "127.0.0.1", "port": dead})
        assert st == 502 and doc["error"]["code"] == "unreachable", doc
        assert doc["error"]["address"] == "127.0.0.1" and doc["error"]["port"] == dead
        assert r.px.port != r.engine.port
        st, doc = r.post("/v1/fleet/engines/pair/start", {"address": "127.0.0.1", "port": r.px.port})
        assert st in (409, 502) and doc["error"]["code"] in ("not_an_engine", "pairing_unsupported",
                                                             "no_window"), doc
        r.engine.pair_routes = True
        r.engine.allow = False
        r.engine.open()
        st, doc = r.post("/v1/fleet/engines/pair/start", {"address": "127.0.0.1", "port": r.engine.port})
        assert st == 403 and doc["error"]["code"] == "not_allowed", doc
        # the controller's own limit on tries
        r.engine.allow = True
        code = r.engine.open()
        p = r.start()
        onb = r.fleet.engines()
        onb._tries.extend([onb.clock()] * 10)
        st, doc = r.post("/v1/fleet/engines/pair/code", {"pairing": p["pairing"], "code": code})
        assert st == 429 and doc["error"]["code"] == "too_many", doc
        onb._tries.clear()
    return "a second pairing replaces the key; abandon; bad bodies; old engine; not an engine"


# ---------------------------------------------------------------------------- 7 the codes

def test_codes_table():
    with open(os.path.join(ROOT, "contracts", "fleet-v1", "engine-codes.json")) as f:
        doc = json.load(f)
    table = {k: (v["status"], v["message"], v["fix"]) for k, v in doc["codes"].items()}
    assert table == enginecodes.CATALOG, set(table) ^ set(enginecodes.CATALOG)
    for code, (status, msg, fix) in enginecodes.CATALOG.items():
        assert 400 <= status < 600 and msg.strip() and fix.strip(), code
        e = enginecodes.error(code)
        assert "?" not in e.message + e.fix and "{" not in e.message + e.fix, (code, e.message, e.fix)
        ok(e.body(), "engine-error.schema.json")
        ok(e.finding(), "engine-finding.schema.json")
        # one sentence: ends once, no line break
        assert "\n" not in e.message and e.message.endswith((".", ":")) or e.message.endswith("given"), \
            (code, e.message)
    e = enginecodes.error("rate_limited", "slow down", retry_after=17, address="h", port=1)
    assert e.fix == "Wait 17 seconds, then type the code again." and e.detail == "slow down"
    assert enginecodes.error("wrong_code", password="x", token="y", pem="z").facts == {}, \
        "a secret is no fact"
    return f"{len(table)} codes, each with its sentence and fix"


# ---------------------------------------------------------------------------- 8 no secret

def test_no_secret_anywhere(tmp_path, monkeypatch):
    with rig(tmp_path, monkeypatch) as r:
        codes = []
        code = r.engine.open()
        codes.append(code)
        p = r.start()
        wrong = f"{(int(code) + 5) % 10 ** 6:06d}"
        codes.append(wrong)
        r.post("/v1/fleet/engines/pair/code", {"pairing": p["pairing"], "code": wrong})
        doc = r.post("/v1/fleet/engines/pair/code", {"pairing": p["pairing"], "code": code})[1]
        hid = doc["engine"]["id"]
        r.get(f"/v1/fleet/engines/{hid}")
        token = r.cfg.engine_token(hid)
        pem = r.cfg.host_key_private(hid)
        assert token and pem
        seen = r.seen() + "\n" + r.db_rows()
        for c in codes:
            assert f'"{c}"' not in seen and f" {c} " not in seen and f"={c}" not in seen, c
        assert token not in seen
        for line in pem_lines(pem):
            assert line not in seen
        raw = r.files()
        assert token.encode() not in raw
        assert all(line.encode() not in raw for line in pem_lines(pem))
    return "no code, private key or token in texts, rows or file bytes"


if __name__ == "__main__":
    from scriptmode import run
    run(globals())
