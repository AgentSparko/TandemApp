"""SRV-833: after pairing, bare engine and a model from the page (proxy/engineonboard.py `wizard`,
`set_mode`, `models`, `pull`, `progress`), against fakes.

The engine host is tests/onboard_rig.py's, paired by code: `mode get` and `mode set` go over SSH
to tests/fake_tandem_remote.py, the Setup Guide's routes over HTTP to tests/fake_engine.py with the
token pairing gave this controller (each of its jobs moves one step a read: check, the download
with its GB so far, then done).

  Gherkin (SRV-833, plan 13 item 10):
  1  Given a paired engine with no model, When the wizard continues, Then "bare engine" is
     preselected (`wizard.mode_choice` engine, `needs_model`, next model) And the user picks a model
     (the Setup Guide's list) and sees its download progress (pull: the plan's GB, a job; progress:
     the fetch step with its detail, steps done, then done) And no file was edited by hand (only
     the engine's verbs over the key's forced command and its Setup Guide's routes ran)
                                                         test_bare_engine_then_a_model
  2  the mode: unchanged when it is configured already; set (idle-gated) when it is chat or was
     only inferred from an empty QSE_MODE; chat with its extension, installed from this
     controller's wheel when the engine host lacks it; bad bodies 400; audited. Each change is a
     change set of one part through the settings interface (SRV-845): its id in the audit, the
     restart the settings follow, the engine's job in the answer
                                                         test_mode
  3  what goes wrong, each with its code: an unknown model, a plan with errors, a job running, the
     Setup Guide's admin password, a token the engine refuses, no token here, the engine down, its
     port blocked from here (told apart over SSH), a node that is not an engine host, bad bodies;
     dry_run plans only                                  test_after_pair_errors
  4  the token goes only into the Authorization header: no answer, error or log line holds it
                                                         test_token_only_in_the_header

    python tests/test_engine_after_pair.py
"""

from __future__ import annotations

import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fake_ssh  # noqa: E402
import fake_tandem_remote as ftr  # noqa: E402
from onboard_rig import ok, rig  # noqa: E402

MODEL = "qwen3.8-27b"


def _paired(r, **state) -> str:
    ftr.set_state(r.engine_dir, **state)
    doc = r.pair()
    return doc["engine"]["id"]


def _settled(r) -> None:
    """Until the restarts the settings follow have ended (the fake job ends at the first read):
    the watch's own SSH calls are done before the next one."""
    for _ in range(200):
        st, doc = r.request("GET", "/v1/settings/restarts")
        if st == 200 and all(x["state"] in ("done", "failed", "timeout", "cancelled")
                             for x in doc["restarts"]):
            return
        time.sleep(0.05)
    raise AssertionError(doc)


def test_bare_engine_then_a_model(tmp_path, monkeypatch):
    with rig(tmp_path, monkeypatch) as r:
        hid = _paired(r, model=None)
        st, eng = r.get(f"/v1/fleet/engines/{hid}")
        assert st == 200, eng
        ok(eng, "engine.schema.json")
        wz = eng["wizard"]
        assert (wz["mode_choice"], wz["needs_model"], wz["next"]) == ("engine", True, "model"), wz
        # bare engine: configured already, nothing changes
        st, m = r.post(f"/v1/fleet/engines/{hid}/mode", {})
        assert st == 200 and ok(m, "engine-mode.schema.json")["changed"] is False, m
        assert m["mode"] == "engine" and m["previous"]["mode"] == "engine" and m["job"] is None
        # the models, one picked, its download followed
        st, ms = r.get(f"/v1/fleet/engines/{hid}/models")
        assert st == 200 and [x["id"] for x in ok(ms, "engine-models.schema.json")["models"]] == [MODEL]
        st, pull = r.post(f"/v1/fleet/engines/{hid}/models/pull", {"model": MODEL})
        assert st == 200, pull
        ok(pull, "engine-pull.schema.json")
        assert pull["plan"]["ok"] and pull["plan"]["download_gb"] == 18.4 and pull["profile"] == "exl3-4.5"
        jid = pull["job"]["id"]
        assert pull["progress"]["state"] == "queued" and not pull["progress"]["finished"]
        assert r.engine.applied == [{"model": MODEL, "profile": "exl3-4.5", "components": {},
                                     "slots": 4, "window": 131072}], "the box's own selection"
        seen_fetch, done_counts = False, []
        for _ in range(12):
            st, pr = r.get(f"/v1/fleet/engines/{hid}/models/progress?job={jid}")
            assert st == 200, pr
            ok(pr, "engine-progress.schema.json")
            p = pr["progress"]
            done_counts.append(p["steps_done"])
            if p["downloading"]:
                seen_fetch = True
                assert p["step"] == "fetch" and p["detail"] == "9.2 of 18.4 GB", p
            if p["finished"]:
                break
        assert seen_fetch and p["state"] == "done" and p["steps_done"] == p["steps"] == 7, p
        assert done_counts == sorted(done_counts), done_counts
        st, pr = r.get(f"/v1/fleet/engines/{hid}/models/progress")
        assert st == 200 and pr["job"] is None, "no job runs now"
        # a profile of the user's choice replaces the box's
        st, pull = r.post(f"/v1/fleet/engines/{hid}/models/pull", {"model": MODEL, "profile": "exl3-3.0"})
        assert st == 200 and r.engine.applied[-1]["profile"] == "exl3-3.0"
        # no file edited by hand: only the engine's verbs, by the key's forced command, and its routes
        assert {tuple(c["tokens"][:2]) for c in r.calls()} <= {("status",), ("mode", "get")}
        runs = [x for x in fake_ssh.log(r.sshlog) if x["tool"] == "ssh" and x.get("remote")]
        assert runs and all(x["remote"] in ("status", "mode get") for x in runs), runs
        paths = {c[1].split("?")[0] for c in r.engine.calls}
        assert all(p.startswith(("/v1/engine/pair", "/v1/setup/")) for p in paths), paths
        assert all(ok_ for m_, p_, ok_ in r.engine.calls if p_.startswith("/v1/setup/"))
        assert len(r.lines()) == 1, "the engine's own pairing wrote the only line"
    return "bare engine preselected and kept, a model picked, its download followed"


def test_mode(tmp_path, monkeypatch):
    with rig(tmp_path, monkeypatch) as r:
        hid = _paired(r, mode="chat")
        n = len(r.calls())
        st, m = r.post(f"/v1/fleet/engines/{hid}/mode", {})
        assert st == 200 and m["changed"] and m["mode"] == "engine" and m["previous"]["mode"] == "chat", m
        ok(m, "engine-mode.schema.json")
        assert m["job"]["kind"] == "mode" and m["job"]["when"]["mode"] == "idle"
        toks = [c["tokens"] for c in r.calls()[n:]]
        assert toks[0] == ["mode", "get"] and toks[-1] == ["mode", "set", "engine", "--when", "idle",
                                                           "--deadline", "600"], toks
        assert {tuple(t[:2]) for t in toks} <= {("mode", "get"), ("mode", "set"), ("status",)}, toks
        assert ftr.state(r.engine_dir)["mode"] == "engine"
        a = [x for x in r.cfg.audit(50) if x["action"] == "mode" and x["object_id"] == hid]
        assert a and a[0]["detail"]["mode"] == "engine" and a[0]["detail"]["from"] == "chat"
        cs = a[0]["detail"]["change_set"]
        assert cs.startswith("cs-") and {x["action"] for x in r.cfg.audit(50, change_set=cs)} >= \
            {"settings.apply", "settings.change_set"}, "a change set of its own"
        _settled(r)
        # an engine mode only inferred from an empty QSE_MODE is written explicitly
        ftr.set_state(r.engine_dir, mode="engine", inferred=True)
        st, m = r.post(f"/v1/fleet/engines/{hid}/mode", {"mode": "engine", "when": "now"})
        assert st == 200 and m["changed"] and m["previous"]["inferred"] is True, m
        assert r.calls()[-1]["tokens"] == ["mode", "set", "engine", "--when", "now"]
        _settled(r)
        # chat with its extension, which the engine host has
        ftr.set_state(r.engine_dir, ext={"tandemapp": {"module": "tandemapp.engine_ext",
                                                       "version": "0.15.0", "sha256": "0" * 64,
                                                       "wheel": "tandemapp-0.15.0-py3-none-any.whl"}})
        st, m = r.post(f"/v1/fleet/engines/{hid}/mode", {"mode": "chat", "ext": "tandemapp.engine_ext"})
        assert st == 200 and m["changed"], m
        assert r.calls()[-1]["tokens"] == ["mode", "set", "chat", "--ext", "tandemapp.engine_ext",
                                           "--when", "idle", "--deadline", "600"]
        _settled(r)
        # chat without it: this controller's wheel goes first
        wheel = os.path.join(str(tmp_path), "tandemapp-0.15.0-py3-none-any.whl")
        with open(wheel, "wb") as fh:
            fh.write(b"PK\x03\x04 the controller's own wheel")
        monkeypatch.setenv("TANDEM_NODE_WHEEL", wheel)
        ftr.set_state(r.engine_dir, mode="engine", ext={})
        n = len(r.calls())
        st, m = r.post(f"/v1/fleet/engines/{hid}/mode", {"mode": "chat"})
        assert st == 200 and m["changed"], m
        toks = [c["tokens"][:2] for c in r.calls()[n:]]
        assert toks.index(["ext", "install"]) < toks.index(["mode", "set"]), toks
        _settled(r)
        for body in ({"mode": "setup"}, {"mode": "engine", "when": "later"}, {"mode": "engine", "ext": "x"},
                     {"mode": "chat", "ext": "a b"}, {"x": 1}):
            st, e = r.post(f"/v1/fleet/engines/{hid}/mode", body)
            assert st == 400 and e["error"]["code"] == "bad_request", (body, e)
        # busy: a lifecycle job acts
        ftr.set_state(r.engine_dir, mode="chat", busy=True)
        st, e = r.post(f"/v1/fleet/engines/{hid}/mode", {"mode": "engine"})
        assert st == 409 and e["error"]["code"] == "busy", e
    return "unchanged when configured, set when chat or inferred, chat with ext, refusals"


def test_after_pair_errors(tmp_path, monkeypatch):
    with rig(tmp_path, monkeypatch) as r:
        hid = _paired(r, model=None)
        base = f"/v1/fleet/engines/{hid}"
        st, e = r.post(f"{base}/models/pull", {"model": "no-such-model"})
        assert st == 404 and e["error"]["code"] == "unknown_model" and e["error"]["model"] == "no-such-model"
        st, d = r.post(f"{base}/models/pull", {"model": MODEL, "dry_run": True})
        assert st == 200 and d["job"] is None and d["plan"]["ok"] and r.engine.applied == [], d
        r.engine.plan_errors = ["needs 98 GiB, the box has 61 free"]
        st, e = r.post(f"{base}/models/pull", {"model": MODEL})
        assert st == 409 and e["error"]["code"] == "setup_refused" and "98 GiB" in e["error"]["detail"]
        r.engine.plan_errors = []
        st, d = r.post(f"{base}/models/pull", {"model": MODEL})
        assert st == 200
        st, e = r.post(f"{base}/models/pull", {"model": MODEL})
        assert st == 409 and e["error"]["code"] == "busy", e
        # the Setup Guide's admin password, then a token the engine refuses
        r.engine.setup_password = True
        st, e = r.get(f"{base}/models")
        assert st == 403 and e["error"]["code"] == "setup_locked", e
        r.engine.setup_password = False
        kept = set(r.engine.tokens)
        r.engine.tokens.clear()
        st, e = r.get(f"{base}/models")
        assert st == 401 and e["error"]["code"] == "token_refused" and "Rotate" in e["error"]["fix"], e
        r.engine.tokens |= kept
        # no token here
        r.cfg.delete_engine_token(hid)
        st, e = r.get(f"{base}/models")
        assert st == 409 and e["error"]["code"] == "token_missing", e
        r.cfg.put_engine_token(hid, next(iter(kept)))
        # no HTTP answer: blocked from here while the engine runs, or the engine down (over SSH)
        r.engine.stop()
        st, e = r.get(f"{base}/models")
        assert st == 502 and e["error"]["code"] == "http_blocked", e
        assert e["error"]["port"] == r.engine.port and str(r.engine.port) in e["error"]["fix"]
        ftr.set_state(r.engine_dir, engine="down")
        st, e = r.get(f"{base}/models")
        assert st == 503 and e["error"]["code"] == "engine_down" and e["error"]["fix"] == "Start it.", e
        fake_ssh.server(r.server, unreachable=True)
        st, e = r.get(f"{base}/models")
        assert st == 502 and e["error"]["code"] == "ssh_unreachable", e
        st, eng = r.get(base)
        assert st == 200 and eng["status"] is None and eng["finding"]["code"] == "ssh_unreachable"
        assert eng["wizard"]["next"] is None
        # bad bodies and ids
        for body in ({}, {"model": "../x"}, {"model": MODEL, "profile": "a b"},
                     {"model": MODEL, "dry_run": "yes"}, {"model": MODEL, "x": 1}):
            st, e = r.post(f"{base}/models/pull", body)
            assert st == 400 and e["error"]["code"] == "bad_request", (body, e)
        st, e = r.get(f"{base}/models/progress?job=../../x")
        assert st == 400 and e["error"]["code"] == "bad_request"
        r.cfg.add_node({"id": "n-lan", "alias": "a node", "endpoint": "https://192.0.2.9:7701",
                        "address": "192.0.2.9", "trust": {}})
        for method, path in (("GET", "/v1/fleet/engines/n-lan/models"),
                             ("POST", "/v1/fleet/engines/n-lan/mode")):
            st, e = r.request(method, path, {} if method == "POST" else None)
            assert st == 409 and e["error"]["code"] == "not_ssh", e
        st, e = r.get("/v1/fleet/engines/eh-none/models")
        assert st == 404 and e["error"]["code"] == "not_found"
    return "each failure with its code: model, plan, busy, locked, token, blocked, down, SSH"


def test_token_only_in_the_header(tmp_path, monkeypatch):
    with rig(tmp_path, monkeypatch) as r:
        hid = _paired(r, model=None)
        token = r.cfg.engine_token(hid)
        r.get(f"/v1/fleet/engines/{hid}/models")
        r.post(f"/v1/fleet/engines/{hid}/models/pull", {"model": MODEL})
        r.get(f"/v1/fleet/engines/{hid}/models/progress")
        r.engine.tokens.clear()
        r.get(f"/v1/fleet/engines/{hid}/models")
        assert token not in r.seen() + "\n" + r.db_rows()
        assert token.encode() not in r.files()
        assert any(c[2] for c in r.engine.calls if c[1].startswith("/v1/setup/")), "sent as the bearer"
        assert json.dumps(r.engine.applied).find(token) < 0
    return "the token in the Authorization header only"


if __name__ == "__main__":
    from scriptmode import run
    run(globals())
