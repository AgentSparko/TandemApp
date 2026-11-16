"""OPS-288, VIS-280, SRV-845: a bare TandemApp by default, and the page's switches (proxy/appparts.py,
/v1/fleet/app/parts): Chat and the conductor on and off, persisted in the controller's settings,
never flipped by a changed default.

  Gherkin (OPS-288):
  1  Given the switch's state rules, When nothing was switched, Then the install decides
     (TANDEM_CHAT off/external/own, TANDEM_CONDUCTOR a folder or off); When the page switched a
     part, Then its choice wins over the install's, and a conductor switched on in an install
     that said off keeps its documents in <state dir>/conductor
                                                                test_state_rules
  2  Given a Parts object on a config database, When a switch is planned, Then nothing changes and
     the plan says what would; When applied with the plan's confirm, Then the setting is stored,
     audited and read again by a new Parts (a controller restart); a stale or foreign confirm is
     409 stale_plan, a bad body 400, an unknown part 404; asking for the state that holds changes
     nothing                                                    test_plan_confirm_persist
  3  Given a controller started with TANDEM_CHAT=off (what every new install writes), When the
     Chat tab is opened, Then every chat route answers 503 chat_off, GET /v1/fleet/chat says
     owner off and its verbs 409, the health page has no chat or sandbox finding (its checks
     null); When an admin switches Chat on (plan, confirm), Then the controller's own chatd runs
     with <state>/chatd.env and the Chat tab answers, and with no sandbox runtime the view and the
     health page say so; When switched off, Then chatd stops (a hand stop) and the routes say off
     again; When the controller restarts with TANDEM_CHAT=own in its env file, Then the page's off
     still holds                                                test_chat_switch_on_a_controller
  4  Given a controller with the conductor off, When an admin switches it on, Then /v1/conductor/*
     answers from <state>/conductor at once (no restart); When switched off, Then it answers 404
     again and the documents stay; the switches need the admin from another machine (401), the
     view is a read                                             test_conductor_switch_and_rights
  5  Given an install that set TANDEM_CHAT=own explicitly and never used the page, When it starts
     on this release, Then Chat stays on with its own chatd: a changed default flips nothing
                                                                test_explicit_install_untouched

CPU only. Run: python tests/test_app_parts.py
"""

from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ports import free_port  # noqa: E402
from proxy import appchat, appparts  # noqa: E402
from proxy import config as config_mod  # noqa: E402
from test_app_host import ADMIN_A, FAR, Ctl, _kill_chatd, _until  # noqa: E402


def _cfg(d: str) -> config_mod.ConfigDB:
    return config_mod.ConfigDB(os.path.join(d, "config.sqlite3"), env={},
                               use_keychain=False).open()


class FakeApp:
    def __init__(self):
        self.calls = []

    def chat_status(self):
        return {"running": False, "url": "http://127.0.0.1:8016", "data": "/x/chat",
                "sandbox": "none", "sandbox_path": "/x/none.sock"}

    def set_chat(self, on, runner, who):
        self.calls.append(("chat", on, runner, who))
        return "done"

    def set_conductor(self, root, who):
        self.calls.append(("conductor", root, who))
        return "done"

    def backends(self):
        return [{"id": "eng", "label": "Engine", "kind": "tandemllm", "enabled": True,
                 "env": True}]

    def engines(self):
        return [{"id": "h1", "alias": "box", "transport": "ssh"}]


# ---------------------------------------------------------------------------- 1 the rules

def test_state_rules():
    d = tempfile.mkdtemp(prefix="parts-rules-")
    cfg = _cfg(d)
    try:
        assert [appparts.chat_on(cfg, m) for m in ("off", "external", "own")] == [False, True, True]
        assert [appparts.chat_runner(m) for m in ("off", "external", "own")] == \
            ["own", "external", "own"]
        assert appparts.conductor_root(cfg, "off", d) is None
        assert appparts.conductor_root(cfg, "/srv/c", d) == "/srv/c"
        cfg.set_setting("parts.chat", "off", who="t")
        cfg.set_setting("parts.conductor", "on", who="t")
        assert not appparts.chat_on(cfg, "own"), "the page's off wins over the install's own"
        assert appparts.conductor_root(cfg, "off", d) == os.path.join(d, "conductor")
        assert appparts.conductor_root(cfg, "/srv/c", d) == "/srv/c"
        cfg.set_setting("parts.chat", "on", who="t")
        cfg.set_setting("parts.conductor", "off", who="t")
        assert appparts.chat_on(cfg, "off")
        assert appparts.conductor_root(cfg, "/srv/c", d) is None
        # the keys are not app settings: the fleet's export never carries them
        assert cfg.settings_under("app.") == {}
    finally:
        cfg.close()
    return "install decides until switched; the page wins after; state dir for a new conductor"


# ---------------------------------------------------------------------------- 2 plan, confirm

def test_plan_confirm_persist():
    d = tempfile.mkdtemp(prefix="parts-plan-")
    cfg = _cfg(d)
    app = FakeApp()
    logs = []
    try:
        p = appparts.Parts(cfg, install_chat="off", install_conductor="off", state_dir=d, app=app,
                           log=logs.append)
        st, v = p.handle("GET", ["app", "parts"], None, "t")
        assert st == 200 and v["chat"] == {"on": False, "switched": False, "install": "off",
                                           "runner": None, "running": None, "sandbox": None,
                                           "note": v["chat"]["note"]}, v
        assert v["conductor"] == {"on": False, "switched": False, "install": "off", "dir": None}
        assert v["mcp"] == {"where": "chat", "available": False}
        assert v["engines"] == [{"id": "h1", "alias": "box"}]
        assert v["backends"] == [{"id": "eng", "label": "Engine", "kind": "tandemllm",
                                  "enabled": True, "env": True}]
        st, plan = p.handle("POST", ["app", "parts", "chat"], {"on": True}, "t")
        assert st == 200 and plan["changed"] and plan["needs_confirm"] and plan["confirm"], plan
        assert any("chatd starts" in c for c in plan["changes"]), plan
        assert any("without their sandbox tools" in c for c in plan["changes"]), plan
        assert app.calls == [] and cfg.setting("parts.chat") is None, "a plan changes nothing"
        # a wrong confirm, bad bodies, an unknown part
        assert p.handle("POST", ["app", "parts", "chat"], {"on": True, "confirm": "x" * 32},
                        "t")[0] == 409
        for bad in ({"on": "yes"}, {}, {"on": True, "force": 1}, {"on": True, "confirm": 3}, []):
            assert p.handle("POST", ["app", "parts", "chat"], bad, "t")[0] == 400, bad
        assert p.handle("POST", ["app", "parts", "memory"], {"on": True}, "t")[0] == 404
        assert p.handle("DELETE", ["app", "parts"], None, "t")[0] == 404
        # applied with the plan's confirm
        st, out = p.handle("POST", ["app", "parts", "chat"], {"on": True,
                                                              "confirm": plan["confirm"]}, "ada")
        assert st == 200 and out["done"] == {"part": "chat", "on": True, "said": "done"}, out
        assert out["chat"]["on"] and out["chat"]["switched"] and out["chat"]["runner"] == "own"
        assert out["chat"]["sandbox"] == "none" and "without their sandbox" in out["chat"]["note"]
        assert app.calls == [("chat", True, "own", "ada")]
        assert cfg.setting("parts.chat") == "on"
        assert any(r["action"] == "switch" and r["object"] == "part" for r in cfg.audit(20)), \
            cfg.audit(20)
        # the old confirm is stale now; the state that holds changes nothing
        assert p.handle("POST", ["app", "parts", "chat"], {"on": False,
                                                           "confirm": plan["confirm"]}, "t")[0] == 409
        st, same = p.handle("POST", ["app", "parts", "chat"], {"on": True, "confirm": "y"}, "t")
        assert st == 200 and same["changed"] is False and same["confirm"] is None, same
        # the conductor: on in an install that said off -> <state dir>/conductor
        st, cp = p.handle("POST", ["app", "parts", "conductor"], {"on": True}, "t")
        assert any(os.path.join(d, "conductor") in c for c in cp["changes"]), cp
        st, out = p.handle("POST", ["app", "parts", "conductor"],
                           {"on": True, "confirm": cp["confirm"]}, "t")
        assert st == 200 and app.calls[-1] == ("conductor", os.path.join(d, "conductor"), "t")
        assert out["conductor"] == {"on": True, "switched": True, "install": "off",
                                    "dir": os.path.join(d, "conductor")}, out
        # a controller restart: a new Parts reads the same settings
        p2 = appparts.Parts(cfg, install_chat="off", install_conductor="off", state_dir=d,
                            app=FakeApp())
        v2 = p2.view()
        assert v2["chat"]["on"] and v2["conductor"]["on"], v2
        # a plan made by another process (another secret) does not apply here
        st, cp = p2.handle("POST", ["app", "parts", "chat"], {"on": False}, "t")
        assert p.handle("POST", ["app", "parts", "chat"], {"on": False, "confirm": cp["confirm"]},
                        "t")[0] == 409
        assert any("Chat switched on" in m for m in logs), logs
    finally:
        cfg.close()
    return "plan changes nothing, the confirm applies, stored and audited, stale refused"


# ---------------------------------------------------------------------------- 3 Chat on a controller

def _switch(c: Ctl, part: str, on: bool, token=None, headers=None):
    st, plan = c.send("POST", f"/v1/fleet/app/parts/{part}", {"on": on}, token=token,
                      headers=headers)
    if st != 200 or not plan.get("changed"):
        return st, plan
    return c.send("POST", f"/v1/fleet/app/parts/{part}", {"on": on, "confirm": plan["confirm"]},
                  token=token, headers=headers)


def _chatd_env(c: Ctl) -> tuple[int, str]:
    port = free_port()
    with open(os.path.join(c.state, "chatd.env"), "w") as f:
        f.write(f"QSE_CHAT_URL=http://127.0.0.1:{port}\n"
                f"QSE_SANDBOX_SOCKET={os.path.join(c.home, 'no-runtime.sock')}\n")
    return port, os.path.join(c.state, "chat")


CHAT_CODES = ("chatd_down", "chatd_silent", "chat_unreachable", "sandbox_off",
              "sandbox_unreachable", "sandbox_no_image", "sandbox_unavailable", "no_memory_cgroup")


def test_chat_switch_on_a_controller():
    c = Ctl("parts-chat", lines=["TANDEM_CHAT=off"])
    port, data = _chatd_env(c)
    try:
        c.start()
        st, body = c.get("/v1/chat-app/capabilities")
        assert st == 503 and body["error"]["type"] == "chat_off", (st, body)
        assert c.send("POST", "/v1/chat-app/conversations", {"title": "x"})[0] == 503
        assert c.get("/v1/fleet/chat") == (200, {"own": False, "owner": "off"})
        st, body = c.send("POST", "/v1/fleet/chat/start")
        assert st == 409 and body["error"]["type"] == "chat_off", body
        st, h = c.get("/v1/fleet/health/controller")
        assert st == 200 and h["chat"] == "off", h
        assert not [f for f in h["findings"] if f["code"] in CHAT_CODES], h["findings"]
        checks = {x["check"]: x["ok"] for x in h["checks"]}
        assert checks["chatd"] is None and checks["sandbox"] is None, checks
        st, v = c.get("/v1/fleet/app/parts")
        assert st == 200 and not v["chat"]["on"] and not v["conductor"]["on"], v
        assert v["backends"] == [] and v["engines"] == [] and not v["mcp"]["available"], v
        assert not os.path.exists(os.path.join(data, "chatd.pid"))
        # on from the page: the controller's own chatd with its env file
        st, out = _switch(c, "chat", True)
        assert st == 200 and out["chat"]["on"] and out["done"]["on"], out
        assert out["chat"]["runner"] == "own" and out["chat"]["sandbox"] == "none", out
        assert "without their sandbox tools" in out["chat"]["note"], out
        caps = _until(lambda: (lambda r: r if r[0] == 200 else None)(
            c.get("/v1/chat-app/capabilities")), 30)
        assert caps, (c.get("/v1/chat-app/capabilities"), c.tail())
        st, chat = c.get("/v1/fleet/chat")
        assert chat["owner"] == "controller" and chat["state"] == "running", chat
        assert chat["url"] == f"http://127.0.0.1:{port}"
        pid = chat["pid"]
        assert c.get("/v1/fleet/app/parts")[1]["mcp"]["available"] is True
        # the health page: chat checked again, and says the sandboxes are off (no runtime)
        h = _until(lambda: (lambda r: r[1] if r[0] == 200 and r[1]["chat"] == "own" else None)(
            c.get("/v1/fleet/health/controller?fresh=1")), 30, 3.1)
        assert h and any(f["code"] == "sandbox_off" for f in h["findings"]), h
        # off: chatd stops, a hand stop; the routes say off again
        st, out = _switch(c, "chat", False)
        assert st == 200 and not out["chat"]["on"] and "stopped" in out["done"]["said"], out
        assert _until(lambda: not appchat.is_chatd(pid), 40), "chatd still runs"
        assert c.get("/v1/chat-app/capabilities")[0] == 503
        assert c.get("/v1/fleet/chat")[1] == {"own": False, "owner": "off"}
        # a restart with own in the env file: the page's off holds
        c.stop()
        with open(c.env_file) as f:
            text = f.read()
        with open(c.env_file, "w") as f:
            f.write(text.replace("TANDEM_CHAT=off", "TANDEM_CHAT=own"))
        c.start()
        assert c.get("/v1/chat-app/capabilities")[0] == 503
        assert c.get("/v1/fleet/app/parts")[1]["chat"]["switched"] is True
        assert not appchat.is_chatd(appchat.AppChat.load(c.state, env={}).pid())
        assert "Traceback" not in c.tail(2000), c.tail()
    finally:
        c.stop()
        _kill_chatd(data)
    return "off by default and said so, on from the page runs chatd, off stops it, restart holds"


# ---------------------------------------------------------------------------- 4 the conductor

def test_conductor_switch_and_rights():
    c = Ctl("parts-cond", lines=["TANDEM_CHAT=off", f"TANDEM_ADMIN_TOKEN={ADMIN_A}"])
    try:
        c.start()
        assert c.get("/v1/conductor/profiles", ADMIN_A)[0] == 404
        # another machine: the view is a read, a switch needs the admin
        assert c.send("GET", "/v1/fleet/app/parts", headers=FAR)[0] == 200
        st, body = c.send("POST", "/v1/fleet/app/parts/conductor", {"on": True}, headers=FAR)
        assert st == 401, (st, body)
        st, out = _switch(c, "conductor", True, token=ADMIN_A, headers=FAR)
        assert st == 200 and out["conductor"]["on"], out
        assert out["conductor"]["dir"] == os.path.join(c.state, "conductor"), out
        st, prof = c.get("/v1/conductor/profiles", ADMIN_A)
        assert st == 200, (st, prof, c.tail())
        assert os.path.isdir(os.path.join(c.state, "conductor"))
        st, out = _switch(c, "conductor", False, token=ADMIN_A, headers=FAR)
        assert st == 200 and not out["conductor"]["on"], out
        assert c.get("/v1/conductor/profiles", ADMIN_A)[0] == 404
        assert os.path.isdir(os.path.join(c.state, "conductor")), "the documents stay"
        assert "Traceback" not in c.tail(2000), c.tail()
    finally:
        c.stop()
    return "conductor on and off without a restart; switches need the admin, the view is a read"


# ---------------------------------------------------------------------------- 5 explicit installs

def test_explicit_install_untouched():
    c = Ctl("parts-own", lines=["TANDEM_CHAT=own"])
    port, data = _chatd_env(c)
    try:
        c.start()
        v = _until(lambda: (lambda d: d if d.get("state") == "running" else None)(
            c.get("/v1/fleet/chat")[1]), 40)
        assert v and v["owner"] == "controller", (c.get("/v1/fleet/chat"), c.tail())
        st, parts = c.get("/v1/fleet/app/parts")
        assert parts["chat"]["on"] and not parts["chat"]["switched"], parts
        assert parts["chat"]["install"] == "own"
        assert c.get("/v1/chat-app/capabilities")[0] != 503
    finally:
        c.stop()
        _kill_chatd(data)
    return "an install's own stays on: no default flips it"


if __name__ == "__main__":
    from scriptmode import run
    run(globals())
