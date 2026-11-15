"""SRV-835: the health page's backend (proxy/fleethealth.py, GET /v1/fleet/health, POST .../fix),
against fakes.

The engine host is tests/onboard_rig.py's, paired by code: `status`, `start`, `token rotate` and
`upgrade` go over SSH (tests/fake_ssh.py) to tests/fake_tandem_remote.py, /health and /v1/models
over HTTP to tests/fake_engine.py. The controller's chatd is a fake with appchat's surface, its
sandbox runtime a unix socket that answers as the Docker API does, its disk and memory cgroup
injected. The health service's clock is a test clock, so the cache and the limits are exact.

  Gherkin (SRV-835, plan 13 item 11):
  1  Given a stopped engine, When the user opens Health, Then it reads "The engine is not running"
     with a Start button; When the user presses Start, Then the engine starts and the finding
     clears                                                       test_stopped_engine_start
  2  Given a changed host key, Then both fingerprints are shown and no button connects anyway
     (the only action shows how to pair again; Start and Rotate are refused; no token goes to
     that address)                                               test_changed_host_key
  3  every engine finding on a fake host, each with its sentence, fix and action, and each
     action's effect: SSH unreachable (retry; a warning while HTTP serves), key refused
     (re_pair), no model (pick_model), token refused and missing (Rotate clears them, the new
     token sealed), engine too old (Upgrade: a plan, then the apply with its confirm), another
     major (no action), allow-from (no action), port blocked (retry), a refused pairing code
                                                                  test_engine_findings
  4  the controller: chatd down and silent (Restart clears it), the sandbox socket missing,
     silent, without its image and fine, the memory cgroup, low disk (warning, error), a backend
     down (retry probes it), no engine yet (setup.needs_setup, add_engine), chat elsewhere
     unreachable                                                  test_controller_findings
  5  the cache and the limits: a second read is cached with its age, fresh=1 within 3 s is
     limited, an action not offered is 409, six fixes a minute then 429, bad bodies 400
                                                                  test_cache_and_limits
  6  the rights: reads and retry with the reads' rule; Start with the Setup Guide's; Rotate and
     Upgrade with pairing's (from this machine only while no password is set)
                                                                  test_auth_rules
  7  no secret: the token, the rotated token and the private key are in no answer, finding,
     log, audit row, call record or file of the controller       test_no_secret
  8  the tables: engine-codes.json's findings and actions equal the code's; each finding code
     is in the codes table; memory_cgroup on a fake /sys          test_tables_and_cgroup

    python tests/test_fleet_health.py
"""

from __future__ import annotations

import collections
import json
import os
import socket
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fake_engine  # noqa: E402
import fake_ssh  # noqa: E402
import fake_tandem_remote as ftr  # noqa: E402
import test_fleet as tf  # noqa: E402
from onboard_rig import Rig, ok, pem_lines, rig  # noqa: E402
from proxy import enginecodes, fleethealth, hostkeys  # noqa: E402
from proxy_util import Proxy  # noqa: E402

ADMIN = "health-admin-token-0123456789abcdef0123"
LAN = {"X-Real-IP": "192.0.2.77"}


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t

    def tick(self, s: float = 20.0) -> None:
        self.t += s


def _setup(r, **state) -> tuple[str, Clock]:
    ftr.set_state(r.engine_dir, **state)
    hid = r.pair()["engine"]["id"]
    clk = Clock()
    r.fleet.health().clock = clk
    return hid, clk


def req(r, method: str, path: str, body=None, headers=None) -> tuple[int, dict]:
    st, _, out = r.px.request(method, path, body, headers=headers)
    r.texts.append(out.decode("utf-8", "replace"))
    doc = json.loads(out)
    if st != 200 and st != 401:
        ok(doc, "engine-error.schema.json")
    return st, doc


def health(r, tid: str, clk: Clock | None = None) -> dict:
    if clk is not None:
        clk.tick()
    st, doc = req(r, "GET", f"/v1/fleet/health/{tid}")
    assert st == 200, doc
    return ok(doc, "health-target.schema.json")


def fix(r, tid: str, action: str, **kw) -> tuple[int, dict]:
    st, doc = req(r, "POST", f"/v1/fleet/health/{tid}/fix", dict({"action": action}, **kw))
    if st == 200:
        ok(doc, "health-fix.schema.json")
    return st, doc


def codes(view: dict) -> list[str]:
    return [f["code"] for f in view["findings"]]


def one(view: dict, code: str) -> dict:
    got = [f for f in view["findings"] if f["code"] == code]
    assert len(got) == 1, (code, view["findings"])
    return got[0]


def checks(view: dict) -> dict:
    return {c["check"]: c["ok"] for c in view["checks"]}


# ---------------------------------------------------------------------------- 1 Gherkin: Start

def test_stopped_engine_start(tmp_path, monkeypatch):
    with rig(tmp_path, monkeypatch) as r:
        hid, clk = _setup(r)
        v = health(r, hid)
        assert v["state"] == "ok" and v["findings"] == [] and v["kind"] == "engine", v
        assert all(checks(v)[k] for k in ("ssh", "host_key", "engine", "version", "model", "http",
                                          "token")), v["checks"]
        ftr.set_state(r.engine_dir, engine="down")
        v = health(r, hid, clk)
        f = one(v, "engine_down")
        assert f["sentence"] == "The engine is not running." and f["severity"] == "error", f
        assert f["action"] == {"id": "start", "label": "Start", "changes": True, "confirm": False}
        assert f["facts"]["state"] == "down" and v["state"] == "error"
        n = len(r.calls())
        st, d = fix(r, hid, "start")
        assert st == 200 and d["cleared"] and d["finding"] is None and d["done"], d
        assert d["job"]["kind"] == "start" and d["target"]["state"] == "ok"
        assert [c["tokens"] for c in r.calls()[n:]] == [["start"], ["status"]], r.calls()[n:]
        assert ftr.state(r.engine_dir)["engine"] == "healthy"
        a = [x for x in r.cfg.audit(50) if x["action"] == "start" and x["object_id"] == hid]
        assert a, "audited"
        # the whole page: the controller, this engine, every action's button
        clk.tick()
        st, all_ = req(r, "GET", "/v1/fleet/health")
        assert st == 200, all_
        ok(all_, "health.schema.json")
        assert [e["id"] for e in all_["engines"]] == [hid] and all_["controller"]["id"] == "controller"
        assert set(all_["actions"]) == set(fleethealth.ACTIONS)
        assert all_["setup"]["engines"] == 1 and not all_["setup"]["needs_setup"]
    return "stopped engine: the sentence and Start; Start clears it, audited"


# ---------------------------------------------------------------------------- 2 Gherkin: host key

def test_changed_host_key(tmp_path, monkeypatch):
    with rig(tmp_path, monkeypatch) as r:
        hid, clk = _setup(r)
        health(r, hid)
        _pem, pub = hostkeys.generate()
        t, b64 = pub.split()[:2]
        fake_ssh.server(r.server, host_key=[t, b64])
        models_before = sum(1 for c in r.engine.calls if c[1].startswith("/v1/models"))
        v = health(r, hid, clk)
        f = one(v, "host_key_changed")
        assert f["facts"]["pinned"] == r.host_fp and f["facts"]["offered"] == fake_engine.fp_of(b64), f
        assert r.host_fp in f["sentence"] and fake_engine.fp_of(b64) in f["sentence"]
        assert f["action"]["id"] == "re_pair" and f["action"]["changes"] is False, f
        assert not any(x["action"] and x["action"]["changes"] for x in v["findings"]), \
            "no button connects anyway"
        assert checks(v)["host_key"] is False and checks(v)["token"] is None
        assert sum(1 for c in r.engine.calls if c[1].startswith("/v1/models")) == models_before, \
            "no token goes to an address whose host key changed"
        for action in ("start", "rotate_token", "upgrade"):
            st, e = fix(r, hid, action)
            assert st == 409 and e["error"]["code"] == "not_offered", (action, e)
        st, d = fix(r, hid, "re_pair")
        assert st == 200 and d["guide"]["pinned"] == r.host_fp, d
        assert d["guide"]["offered"] == fake_engine.fp_of(b64) and d["guide"]["open"] == "add_engine"
        assert "tandem pair" in d["guide"]["steps"][0] and not d["cleared"]
        assert d["finding"]["code"] == "host_key_changed"
        # nothing changed on the host or here: the pin is the old key
        assert [c["tokens"][:1] for c in r.calls()].count(["start"]) == 0
    return "both fingerprints, only the how-to, Start and Rotate refused, no token sent"


# ---------------------------------------------------------------------------- 3 every finding

def test_engine_findings(tmp_path, monkeypatch):
    with rig(tmp_path, monkeypatch) as r:
        hid, clk = _setup(r, model=None)
        # no model
        v = health(r, hid)
        f = one(v, "no_model")
        assert f["severity"] == "warning" and f["action"]["id"] == "pick_model", f
        st, d = fix(r, hid, "pick_model")
        assert st == 200 and d["guide"]["open"] == "model" and d["guide"]["engine"] == hid
        ftr.set_state(r.engine_dir, model="fake-model")
        # the token refused, then missing: Rotate clears both, the new one sealed
        new = ftr.state(r.engine_dir)["token"]
        r.engine.tokens.clear()
        r.engine.tokens.add(new)                     # the engine takes the token it rotates to
        v = health(r, hid, clk)
        f = one(v, "token_refused")
        assert f["action"]["id"] == "rotate_token" and f["severity"] == "error", f
        st, d = fix(r, hid, "rotate_token")
        assert st == 200 and d["cleared"] and d["result"]["stored"] is True, d
        assert d["result"]["hint"]["last4"] == new[-4:] and r.cfg.engine_token(hid) == new
        assert ["token", "rotate"] in [c["tokens"] for c in r.calls()]
        r.cfg.delete_engine_token(hid)
        v = health(r, hid, clk)
        assert one(v, "token_missing")["action"]["id"] == "rotate_token"
        clk.tick(61)
        st, d = fix(r, hid, "rotate_token")
        assert st == 200 and d["cleared"] and r.cfg.engine_token(hid) == new, d
        # too old: an older minor of the engine API and of the control; Upgrade plans first
        doc = {"ctl": "engine-ctl-v1", "version": "1.0", "verb": "status", "ok": True,
               "engine": {"state": "healthy", "pid": 1, "port": 8000, "health_code": 200,
                          "engine_api": "1.7", "version": "0.15.2", "model": "m",
                          "running_mode": "engine", "stopped_on_purpose": False},
               "mode": "engine", "profile": None, "engine_version": "0.15.2", "engine_api": "1.7",
               "install": True}
        ftr.set_state(r.engine_dir, script={"status": {"exit": 0, "stdout": json.dumps(doc)}})
        v = health(r, hid, clk)
        f = one(v, "engine_too_old")
        assert f["action"]["id"] == "upgrade" and f["action"]["confirm"] is True, f
        assert "engine API 1.7" in f["detail"] and "control 1.0" in f["detail"], f
        assert v["engine_api"] == "1.7" and v["engine_version"] == "0.15.2"
        st, e = fix(r, hid, "upgrade")
        assert st == 400 and "ref" in e["error"]["detail"], e
        st, d = fix(r, hid, "upgrade", ref="v0.16.0")
        assert st == 200 and d["needs_confirm"] and not d["done"] and d["job"] is None, d
        confirm = d["result"]["plan"]["confirm"]
        assert d["result"]["plan"]["to"]["ref"] == "v0.16.0"
        st, d = fix(r, hid, "upgrade", ref="v0.16.0", confirm=confirm)
        assert st == 200 and d["job"]["kind"] == "upgrade" and d["job"]["when"]["mode"] == "idle", d
        assert ["upgrade", "apply", "--ref", "v0.16.0", "--confirm", confirm, "--when", "idle"] in \
            [c["tokens"] for c in r.calls()]
        assert [x for x in r.cfg.audit(50) if x["action"] == "upgrade"], "audited"
        # another major: no button
        doc["engine_api"] = doc["engine"]["engine_api"] = "2.0"
        doc["version"] = "1.1"
        ftr.set_state(r.engine_dir, script={"status": {"exit": 0, "stdout": json.dumps(doc)}})
        clk.tick(61)
        v = health(r, hid)
        f = one(v, "engine_api_unsupported")
        assert f["action"] is None and f["facts"] == {"host": "127.0.0.1", "engine_api": "2.0",
                                                      "speaks": "1.9"}, f
        st, e = fix(r, hid, "upgrade", ref="v0.16.0")
        assert st == 409 and e["error"]["code"] == "not_offered"
        ftr.set_state(r.engine_dir, script={})
        # the engine's allow-from list leaves this machine out
        r.engine.allow = False
        v = health(r, hid, clk)
        assert one(v, "not_allowed")["action"] is None
        r.engine.allow = True
        # the key refused: re_pair only
        with open(r.ak) as fh:
            lines = fh.read()
        with open(r.ak, "w") as fh:
            fh.write("")
        v = health(r, hid, clk)
        f = one(v, "not_paired")
        assert f["action"]["id"] == "re_pair" and checks(v)["token"] is None, f
        with open(r.ak, "w") as fh:
            fh.write(lines)
        # SSH unreachable while HTTP serves with the token: a warning, retry
        fake_ssh.server(r.server, unreachable=True)
        v = health(r, hid, clk)
        f = one(v, "ssh_unreachable")
        assert f["severity"] == "warning" and f["action"]["id"] == "retry", f
        assert f["facts"]["port"] == 22 and f["facts"]["host"] == "127.0.0.1", f
        st, d = fix(r, hid, "retry")
        assert st == 200 and not d["cleared"] and d["finding"]["code"] == "ssh_unreachable"
        fake_ssh.server(r.server, unreachable=False)
        clk.tick(61)
        st, d = fix(r, hid, "retry")
        assert st == 200 and d["cleared"] and d["target"]["state"] == "ok", d
        # a pairing code the engine refused shows on the page until a pairing of that address
        code = r.engine.open()
        p = r.start()
        st, e = req(r, "POST", "/v1/fleet/engines/pair/code",
                    {"pairing": p["pairing"], "code": f"{(int(code) + 1) % 1000000:06d}"})
        assert st == 403 and e["error"]["code"] == "wrong_code"
        clk.tick()
        st, all_ = req(r, "GET", "/v1/fleet/health")
        ok(all_, "health.schema.json")
        pf = all_["pairings"][0]["finding"]
        assert pf["code"] == "wrong_code" and pf["action"]["id"] == "re_pair", pf
        assert all_["pairings"][0]["port"] == r.engine.port and all_["state"] in ("warning", "error")
        r.pair(code)
        st, all_ = req(r, "GET", "/v1/fleet/health")
        assert all_["pairings"] == [], "the pairing cleared it"
        # the port blocked from here while SSH says the engine runs (last: the fake stops)
        r.engine.stop()
        v = health(r, hid, clk)
        f = one(v, "http_blocked")
        assert f["action"]["id"] == "retry" and f["facts"]["port"] == r.engine.port, f
        assert str(r.engine.port) in f["fix"] and checks(v)["http"] is False
        # and both sides silent: one SSH finding that says the HTTP port is silent too
        fake_ssh.server(r.server, unreachable=True)
        v = health(r, hid, clk)
        assert codes(v) == ["ssh_unreachable"], v["findings"]
        f = one(v, "ssh_unreachable")
        assert f["severity"] == "error" and "does not answer either" in f["detail"], f
    return "no model, token refused/missing, too old with plan+confirm, major, allow-from, " \
           "key refused, SSH down, refused code, port blocked"


# ---------------------------------------------------------------------------- 4 the controller

class FakeChat:
    """appchat.AppChat's surface the health page reads."""

    def __init__(self, tmp: str):
        self.cfg: dict = {}
        self.data = os.path.join(tmp, "chat")
        os.makedirs(self.data, exist_ok=True)
        self.log_path = os.path.join(tmp, "logs", "chatd.log")
        self.running = self.answers = False
        self.hand_stop = False
        self.job = None
        self.acts: list = []

    def status(self) -> dict:
        return {"running": self.running, "pid": 7 if self.running else None,
                "answers": self.answers, "version": None}

    def view(self) -> dict:
        return {"watch": {"stopped_by_hand": self.hand_stop, "gave_up": False}}

    def act(self, action: str, who: str) -> dict:
        self.acts.append((action, who))
        self.job = {"id": f"chat-{len(self.acts)}", "action": action, "state": "running",
                    "who": who, "started_at": 1.0, "ended_at": None, "rc": None, "message": None}
        job = self.job

        def run():
            time.sleep(0.2)
            self.running = self.answers = True
            job.update(state="done", rc=0, ended_at=2.0, message="chatd started")
        threading.Thread(target=run, daemon=True).start()
        return dict(job)


class FakeRuntime:
    """A unix socket that answers as the Docker API: /_ping, /images/{name}/json."""

    def __init__(self, image_there: bool = False, silent: bool = False):
        self.dir = tempfile.mkdtemp(prefix="hs")
        self.path = os.path.join(self.dir, "r.sock")
        self.image_there, self.silent = image_there, silent
        self.seen: list = []
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.bind(self.path)
        self.s.listen(8)
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                c, _ = self.s.accept()
            except OSError:
                return
            with c:
                data = c.recv(4096).decode()
                url = data.split(" ")[1] if " " in data else ""
                self.seen.append(url)
                if self.silent:
                    continue
                if url == "/_ping":
                    c.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\nOK")
                elif url.startswith("/images/"):
                    st = b"200 OK" if self.image_there else b"404 Not Found"
                    c.sendall(b"HTTP/1.0 " + st + b"\r\nContent-Length: 2\r\n\r\n{}")
                else:
                    c.sendall(b"HTTP/1.0 404 Not Found\r\n\r\n")

    def close(self):
        self.s.close()


Usage = collections.namedtuple("Usage", "total used free")


def test_controller_findings(tmp_path, monkeypatch):
    with Proxy(env={"QSE_CHAT_URL": "http://127.0.0.1:9"}) as px:
        f = tf.fleet_on(px, tf.local_node(tf.fake_root()))
        r = Rig(px, f, str(tmp_path), monkeypatch)
        try:
            h = f.health()
            clk = Clock()
            h.clock = clk
            h.env = {"QSE_CHAT_URL": "http://127.0.0.1:9"}
            # nothing yet: no engine (the first run), chat elsewhere silent
            v = health(r, "controller")
            assert v["kind"] == "controller" and v["chat"] == "external", v
            f0 = one(v, "no_engine")
            assert f0["severity"] == "info" and f0["action"]["id"] == "add_engine", f0
            assert one(v, "chat_unreachable")["facts"]["address"] == "127.0.0.1:9"
            app = req(r, "GET", "/v1/fleet/app")[1]
            ok(app, "app.schema.json")
            assert app["setup"] == {"needs_setup": True, "engines": 0, "nodes": 0, "backends": 0,
                                    "first_page": "add_engine"}, app["setup"]
            st, d = fix(r, "controller", "add_engine")
            assert st == 200 and d["guide"]["open"] == "add_engine", d
            # own chatd: down, then Restart
            chat = FakeChat(str(tmp_path))
            f.chat = chat
            rt = FakeRuntime(image_there=True)
            chat.cfg["QSE_SANDBOX_SOCKET"] = rt.path
            h.platform = "linux"
            h.cgroup = lambda sock: (True, None)
            v = health(r, "controller", clk)
            fd = one(v, "chatd_down")
            assert fd["action"]["id"] == "restart_chatd" and chat.log_path in fd["fix"], fd
            assert v["chat"] == "own" and v["sandbox_socket"] == rt.path
            assert checks(v)["sandbox"] is True and checks(v)["memory_cgroup"] is True
            st, d = fix(r, "controller", "restart_chatd")
            assert st == 200 and d["cleared"] and d["job"]["state"] == "done", d
            assert [a for a, _ in chat.acts] == ["restart"], chat.acts
            chat.answers = False
            v = health(r, "controller", clk)
            assert one(v, "chatd_silent")["action"]["id"] == "restart_chatd"
            chat.answers = True
            # the sandbox: no image, silent, no socket
            rt.image_there = False
            v = health(r, "controller", clk)
            assert one(v, "sandbox_no_image")["facts"]["image"] == "tandem-sandbox:1"
            rt.silent = True
            v = health(r, "controller", clk)
            assert one(v, "sandbox_unreachable")["facts"]["path"] == rt.path
            rt.close()
            gone = os.path.join(rt.dir, "none.sock")
            chat.cfg["QSE_SANDBOX_SOCKET"] = gone
            v = health(r, "controller", clk)
            fo = one(v, "sandbox_off")
            assert fo["facts"]["path"] == gone and "--with-sandbox" in fo["fix"], fo
            assert fo["severity"] == "warning" and fo["action"] is None
            assert checks(v)["memory_cgroup"] is None, "no socket: nothing to cap"
            # the memory cgroup, with a socket there
            rt2 = FakeRuntime(image_there=True)
            chat.cfg["QSE_SANDBOX_SOCKET"] = rt2.path
            h.cgroup = lambda sock: (False, "the kernel's memory controller is off")
            v = health(r, "controller", clk)
            fm = one(v, "no_memory_cgroup")
            assert "cgroup_enable=memory" in fm["fix"] and fm["detail"].endswith("is off"), fm
            h.cgroup = lambda sock: (True, None)
            # disk
            h.disk = lambda p: Usage(100 << 30, 97 << 30, 3 << 30)
            v = health(r, "controller", clk)
            fdk = one(v, "disk_low")
            assert fdk["severity"] == "warning" and fdk["facts"]["free_gb"] == 3.0, fdk
            h.disk = lambda p: Usage(100 << 30, 100 << 30, 1 << 28)
            v = health(r, "controller", clk)
            assert one(v, "disk_low")["severity"] == "error" and v["state"] == "error"
            h.disk = lambda p: Usage(100 << 30, 1 << 30, 99 << 30)
            # a backend down: retry probes it now
            port = tf_free_port()
            px.svc.cfg.create_backend({"id": "gone", "kind": "generic", "label": "Gone box",
                                    "url": f"http://127.0.0.1:{port}/v1"})
            b = next(x for x in px.svc.cfg.backends() if x["id"] == "gone")
            px.svc.health.check(b)
            v = health(r, "controller", clk)
            fb = one(v, "backend_down")
            assert fb["facts"]["backend"] == "Gone box" and fb["action"]["id"] == "retry", fb
            assert "no_engine" not in codes(v), "a backend is there: no first run"
            st, d = fix(r, "controller", "retry")
            assert st == 200 and d["finding"]["code"] == "backend_down" and not d["cleared"]
            app = req(r, "GET", "/v1/fleet/app")[1]
            assert app["setup"]["needs_setup"] is False and app["setup"]["backends"] == 1
            rt2.close()
        finally:
            r.close()
    return "no engine, chat elsewhere, chatd down/silent + Restart, sandbox x3, cgroup, disk, " \
           "backend"


def tf_free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------------------- 5 cache and limits

def test_cache_and_limits(tmp_path, monkeypatch):
    with rig(tmp_path, monkeypatch) as r:
        hid, clk = _setup(r)
        n = len(r.calls())
        v1 = health(r, hid)
        v2 = health(r, hid)
        assert not v1["cached"] and v2["cached"] and v2["age_s"] == 0.0, (v1, v2)
        clk.tick(5)
        v3 = health(r, hid)
        assert v3["cached"] and v3["age_s"] == 5.0 and not v3["limited"]
        st, v4 = req(r, "GET", f"/v1/fleet/health/{hid}?fresh=1")
        assert st == 200 and not v4["cached"], "fresh after 3 s checks again"
        clk.tick(1)
        st, v5 = req(r, "GET", f"/v1/fleet/health/{hid}?fresh=1")
        assert v5["cached"] and v5["limited"], "fresh within 3 s is limited"
        status_calls = [c for c in r.calls()[n:] if c["tokens"] == ["status"]]
        assert len(status_calls) == 2, status_calls
        # actions not offered, unknown, bad bodies
        st, e = fix(r, hid, "start")
        assert st == 409 and e["error"]["code"] == "not_offered" and "retry" in e["error"]["detail"]
        for body in ({}, {"action": "shell"}, {"action": "retry", "x": 1},
                     {"action": "retry", "when": "later"}, {"action": 3}):
            st, e = req(r, "POST", f"/v1/fleet/health/{hid}/fix", body)
            assert st == 400 and e["error"]["code"] == "bad_request", (body, e)
        st, e = req(r, "GET", "/v1/fleet/health/eh-none")
        assert st == 404 and e["error"]["code"] == "not_found"
        r.cfg.add_node({"id": "n-lan", "alias": "a node", "endpoint": "https://192.0.2.9:7701",
                        "address": "192.0.2.9", "trust": {}})
        st, e = req(r, "GET", "/v1/fleet/health/n-lan")
        assert st == 409 and e["error"]["code"] == "not_ssh"
        # six fixes a minute
        for i in range(fleethealth.FIXES_PER_MIN):
            st, d = fix(r, hid, "retry")
            assert st == 200, (i, d)
        st, e = fix(r, hid, "retry")
        assert st == 429 and e["error"]["code"] == "too_many_fixes" and "a node" not in \
            e["error"]["message"], e
        clk.tick(61)
        assert fix(r, hid, "retry")[0] == 200
    return "cached with age, fresh limited, not offered, 400s, 404, not_ssh, 429"


# ---------------------------------------------------------------------------- 6 the rights

def test_auth_rules(tmp_path, monkeypatch):
    for admin in (None, ADMIN):
        env = {"TANDEM_ADMIN_TOKEN": admin} if admin else {}
        with Proxy(env=env) as px:
            f = tf.fleet_on(px, tf.local_node(tf.fake_root()))
            r = Rig(px, f, str(tmp_path / ("a" if admin else "n")), monkeypatch)
            try:
                hid, clk = _setup(r)
                bearer = {"Authorization": f"Bearer {admin}"} if admin else {}
                st, _ = req(r, "GET", f"/v1/fleet/health/{hid}", headers=LAN)
                assert st == 200, "a read follows the reads' rule (the login is off)"
                st, _ = req(r, "POST", f"/v1/fleet/health/{hid}/fix", {"action": "retry"},
                            headers=LAN)
                assert st == 200, "retry is a read"
                ftr.set_state(r.engine_dir, engine="down")
                clk.tick()
                st, d = req(r, "POST", f"/v1/fleet/health/{hid}/fix", {"action": "start"},
                            headers=LAN)
                if admin:
                    assert st == 401, d
                    st, d = req(r, "POST", f"/v1/fleet/health/{hid}/fix", {"action": "start"},
                                headers={**LAN, **bearer})
                assert st == 200 and d["cleared"], d
                r.engine.tokens.clear()
                r.engine.tokens.add(ftr.state(r.engine_dir)["token"])
                clk.tick()
                st, d = req(r, "POST", f"/v1/fleet/health/{hid}/fix", {"action": "rotate_token"},
                            headers=LAN)
                assert st == 401, ("Rotate is pairing's: from this machine or with the password",
                                   d)
                st, d = req(r, "POST", f"/v1/fleet/health/{hid}/fix", {"action": "rotate_token"},
                            headers={**LAN, **bearer} if admin else None)
                assert st == 200 and d["cleared"], d
            finally:
                r.close()
    return "reads and retry open as reads; Start the Setup Guide's; Rotate pairing's"


# ---------------------------------------------------------------------------- 7 no secret

def test_no_secret(tmp_path, monkeypatch):
    with rig(tmp_path, monkeypatch) as r:
        hid, clk = _setup(r, model=None)
        token = r.cfg.engine_token(hid)
        pem = hostkeys.HostKeys(r.cfg).private(hid)
        new = ftr.state(r.engine_dir)["token"]
        health(r, hid)
        r.engine.tokens.clear()
        r.engine.tokens.add(new)
        health(r, hid, clk)
        assert fix(r, hid, "rotate_token")[0] == 200
        ftr.set_state(r.engine_dir, engine="down")
        health(r, hid, clk)
        assert fix(r, hid, "start")[0] == 200
        clk.tick()
        req(r, "GET", "/v1/fleet/health")
        fake_ssh.server(r.server, unreachable=True)
        health(r, hid, clk)
        seen = r.seen() + "\n" + r.db_rows()
        files = r.files()
        for secret in (token, new):
            assert secret not in seen and secret.encode() not in files, "a token leaked"
        for line in pem_lines(pem):
            assert line not in seen and line.encode() not in files, "the private key leaked"
        assert any(c[2] for c in r.engine.calls if c[1] == "/v1/models"), "sent as the bearer"
    return "no token, rotated token or private key anywhere"


# ---------------------------------------------------------------------------- 8 the tables

def test_tables_and_cgroup(tmp_path):
    with open(os.path.join(ROOT, "contracts", "fleet-v1", "engine-codes.json")) as fh:
        doc = json.load(fh)
    assert {k: (v["severity"], v["action"]) for k, v in doc["findings"].items()} == \
        fleethealth.FINDINGS
    assert {k: (v["label"], v["changes"], v["confirm"], v["route"])
            for k, v in doc["actions"].items()} == fleethealth.ACTIONS
    for code, (sev, act) in fleethealth.FINDINGS.items():
        assert code in enginecodes.CATALOG and sev in fleethealth.SEVERITIES, code
        assert act is None or act in fleethealth.ACTIONS, code
        f = fleethealth.finding(code)
        ok(f, "health-finding.schema.json")
        assert "{" not in f["sentence"] + f["fix"] and "?" not in f["sentence"] + f["fix"], f
    assert fleethealth.FINDINGS["host_key_changed"][1] == "re_pair" and \
        not fleethealth.ACTIONS["re_pair"][1], "a changed host key never gets an action that acts"
    assert {a[3] for a in fleethealth.ACTIONS.values()} <= {"dashboard", "setup", "fleet_trust",
                                                             "conductor_write"}
    # memory_cgroup on a fake /sys
    root = str(tmp_path / "cg")
    os.makedirs(os.path.join(root, "user.slice", "user-995.slice", "user@995.service"))

    def put(rel, text):
        with open(os.path.join(root, rel), "w") as fh:
            fh.write(text)
    put("cgroup.controllers", "cpuset cpu io pids\n")
    assert fleethealth.memory_cgroup(None, root) == (False, "the kernel's memory controller is off")
    put("cgroup.controllers", "cpuset cpu io memory pids\n")
    put("user.slice/user-995.slice/user@995.service/cgroup.controllers", "cpu pids\n")
    sock = "/run/user/995/podman/podman.sock"
    assert fleethealth.memory_cgroup(sock, root) == \
        (False, "the memory controller is not delegated to user 995")
    put("user.slice/user-995.slice/user@995.service/cgroup.controllers", "cpu memory pids\n")
    assert fleethealth.memory_cgroup(sock, root) == (True, None)
    assert fleethealth.memory_cgroup("/var/run/docker.sock", root) == (True, None)
    proc = str(tmp_path / "cgroups")
    with open(proc, "w") as fh:
        fh.write("#subsys_name\thierarchy\tnum_cgroups\tenabled\nmemory\t0\t1\t0\n")
    assert fleethealth.memory_cgroup(None, str(tmp_path / "none"), proc)[0] is False
    with open(proc, "w") as fh:
        fh.write("#subsys_name\thierarchy\tnum_cgroups\tenabled\nmemory\t4\t90\t1\n")
    assert fleethealth.memory_cgroup(None, str(tmp_path / "none"), proc) == (True, None)
    # the rights by action
    assert fleethealth.route_for("POST", ["health", "x", "fix"], {"action": "upgrade"}) == "fleet_trust"
    assert fleethealth.route_for("POST", ["health", "x", "fix"], {"action": "start"}) == "setup"
    assert fleethealth.route_for("POST", ["health", "x", "fix"], {"action": "nope"}) == "dashboard"
    assert fleethealth.route_for("GET", ["health"], None) == "dashboard"
    return f"{len(fleethealth.FINDINGS)} findings, {len(fleethealth.ACTIONS)} actions, cgroup v1/v2"


if __name__ == "__main__":
    from scriptmode import run
    run(globals())
