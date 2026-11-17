"""SRV-841: TandemApp's controller on a host with no engine (the plan's C1): it serves there, takes
the fleet from another controller and gives it back, and runs chatd itself.

Each controller here is a real `python -m proxy serve` process started through
packaging/linux/tandem-app.sh in a sandbox HOME of its own: no engine checkout, no engine data
folder, no TandemNode agent (TANDEM_NODE_AGENT off is the launcher's default). Its engine is a fake
OpenAI-compatible server on a free loopback port, a fleet member reached over HTTP like a remote
one. The nodes are TandemNode agents in this process (tests/test_node_trust.py), paired by code.
chatd is the real one on a free port with a data folder in the sandbox.

  1  the launcher and the unit: the defaults for a host with no engine (only when neither the
     environment nor the env file sets them), the interpreter, the env file; the unit runs it
                                                                    test_launcher_and_unit
  2  a controller in a sandbox HOME with no engine: nothing of an engine imported, /health, the
     page, the models of a fake remote engine, a chat completion relayed to it with the stored
     key, nothing written outside its state dir, no engine data folder made
                                                                    test_serves_with_no_engine
  3  the taker's half across hosts, on fakes: a giver reached at a LAN name; its loopback
     backends point at that host, the users of its engine's database come along as hashes, an
     engine host over SSH comes with its key pair (under the taker's master key) and its pinned
     host key, a `moved_to` the taker kept is cleared; the giver's commit drops the carried key
                                                                    test_take_across_hosts
  4  take and take-back between two controllers in two sandbox HOMEs: B takes A's fleet (a
     paired node, a backend with its stored key, a user, the engine's users), serves it, A says
     moved_to B; A takes it back the same way and its old moved_to is gone
                                                                    test_take_and_take_back
  5  the controller's own chatd (`--chat own`): started at the controller's start with its own
     env file and the controller's gateway as its engine (the fake engine's models through it),
     never the controller's admin token; the forward goes to it with its own key; stop, start,
     a controller restart adopts it                                 test_controller_owned_chatd
  6  the watcher on a real chatd: a crash started again, 3 starts a clock hour then one line,
     the next hour again; a hand stop holds it until a hand start; a port another program holds
                                                                    test_chatd_watcher
  7  TandemNode's path stays: `--chat external` (the default) forwards to QSE_CHAT_URL, refuses
     the verbs with not_owned; nodechat.Chat still loads its engine-box defaults; the client
     names come from the controller's own clients.json first
                                                                    test_external_chat_stays

CPU only, standard library plus `cryptography`. Run: python tests/test_app_host.py
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fake_backend  # noqa: E402
from ports import free_port  # noqa: E402
from proxy import appchat, ctlmove, enginectl, hostkeys, nodechat  # noqa: E402
from proxy import config as config_mod  # noqa: E402
from proxy_util import Proxy, run  # noqa: E402

LAUNCHER = os.path.join(ROOT, "packaging", "linux", "tandem-app.sh")
UNIT = os.path.join(ROOT, "packaging", "linux", "tandem-app.service")
KEY = "sk-fixture-ABCDEFGH1234"
ADMIN_A = "host-a-admin-token-0123456789abcdef0123"
ADMIN_B = "host-b-admin-token-0123456789abcdef0123"
ENGINE_DIRS = (".qwen38-spark-engine", "TandemLLM")


def _until(fn, s=30.0, step=0.1):
    end = time.time() + s
    while time.time() < end:
        got = fn()
        if got:
            return got
        time.sleep(step)
    return None


def _page() -> str:
    d = tempfile.mkdtemp(prefix="apphost-page-")
    with open(os.path.join(d, "index.html"), "w") as f:
        f.write("<!doctype html><title>TandemApp</title><p>apphost-page</p>\n")
    return d


# a request as another machine sends it through a front (no trusted-local pass)
FAR = {"X-Real-IP": "192.0.2.5"}


def _http(url: str, method: str, path: str, body=None, token=None, timeout=30.0, headers=None):
    u = url.split("://", 1)[1]
    host, port = u.split(":")
    c = http.client.HTTPConnection(host, int(port), timeout=timeout)
    h = {"Content-Type": "application/json", **(headers or {})}
    if isinstance(token, dict):
        h.update(token)                     # SEC-28: a signed-in session's headers
    elif token:
        h["Authorization"] = f"Bearer {token}"
    c.request(method, path, None if body is None else json.dumps(body).encode(), h)
    r = c.getresponse()
    raw = r.read()
    c.close()
    try:
        return r.status, json.loads(raw)
    except ValueError:
        return r.status, raw


class Ctl:
    """A controller started through the launcher in a sandbox HOME, on a free loopback port."""

    def __init__(self, label: str, lines=(), env=None):
        self.home = tempfile.mkdtemp(prefix=f"apphost-{label}-")
        self.state = os.path.join(self.home, ".tandemllm")
        os.makedirs(self.state, mode=0o700)
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.log = os.path.join(self.home, "controller.log")
        self.env_file = os.path.join(self.state, "app.env")
        with open(self.env_file, "w") as f:
            f.write("\n".join([f"TANDEM_HOST=127.0.0.1", f"TANDEM_PORT={self.port}",
                               "QSE_TEST=1", *lines]) + "\n")
        os.chmod(self.env_file, 0o600)
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.home,
                    "LANG": "C.UTF-8", "TANDEM_APP_PYTHON": sys.executable,
                    "TMPDIR": tempfile.gettempdir(), **(env or {})}
        self.page = _page()
        self.proc = None

    def start(self) -> "Ctl":
        out = open(self.log, "ab")
        self.proc = subprocess.Popen(["bash", LAUNCHER, "--dashboard-dir", self.page,
                                      "--no-keychain"], env=self.env, cwd=self.home,
                                     stdout=out, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, start_new_session=True)
        out.close()
        ok = _until(lambda: self.proc.poll() is None and self.get("/health")[0] == 200, 60)
        assert ok, self.tail()
        return self

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(20)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait(5)
        self.proc = None

    def tail(self, n=40) -> str:
        try:
            with open(self.log, errors="replace") as f:
                return "".join(f.readlines()[-n:])
        except OSError:
            return ""

    def get(self, path, token=None):
        try:
            return _http(self.url, "GET", path, token=token, timeout=5.0)
        except OSError as exc:
            return 0, str(exc)

    def send(self, method, path, body=None, token=None, headers=None):
        return _http(self.url, method, path, body, token, headers=headers)

    def owner(self, code: str, name: str = "own",
              password: str = "the owner password of the test") -> dict:
        """SEC-28: the first owner, made with the setup code `code` (an admin token of before
        counts once as one), signed in: its session's headers (the cookie and the CSRF token a
        write needs)."""
        u = self.url.split("://", 1)[1]
        host, port = u.split(":")
        c = http.client.HTTPConnection(host, int(port), timeout=30)
        c.request("POST", "/v1/auth/setup", json.dumps({"code": code, "username": name,
                                                        "password": password}).encode(),
                  {"Content-Type": "application/json"})
        r = c.getresponse()
        raw = r.read()
        c.close()
        assert r.status == 201, (r.status, raw[:300])
        return {"Cookie": r.getheader("Set-Cookie").split(";", 1)[0],
                "X-Tandem-CSRF": json.loads(raw)["csrf"]}

    def files(self) -> list[str]:
        out = []
        for d, _, fs in os.walk(self.home):
            out += [os.path.relpath(os.path.join(d, f), self.home) for f in fs]
        return out


# ---------------------------------------------------------------------------- 1 launcher, unit

def test_launcher_and_unit():
    c = Ctl("print")
    env = dict(c.env, TANDEM_APP_STATE=c.state)
    r = subprocess.run(["bash", LAUNCHER, "--print", "--port", "1"], env=env, capture_output=True,
                       text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    line = r.stdout.strip()
    assert line.startswith("TANDEM_NODE_AGENT=off TANDEM_CONDUCTOR=off "), line
    assert f"--config-db {c.state}/config.sqlite3" in line and \
        f"--usage-ledger {c.state}/ledger.sqlite3" in line, line
    assert f"--env-file {c.env_file}" in line and line.endswith("--port 1"), line
    assert sys.executable in line, line
    # a default the env file sets, or the environment, stays theirs
    with open(c.env_file, "a") as f:
        f.write("export TANDEM_CONDUCTOR=~/.tandemllm/conductor\n")
    r = subprocess.run(["bash", LAUNCHER, "--print"], env=dict(env, TANDEM_NODE_AGENT="http://x"),
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and r.stdout.startswith("TANDEM_NODE_AGENT=http://x "), r.stdout
    assert "TANDEM_CONDUCTOR=off" not in r.stdout, r.stdout
    # no env file: none is passed
    os.unlink(c.env_file)
    r = subprocess.run(["bash", LAUNCHER, "--print"], env=env, capture_output=True, text=True,
                       timeout=30)
    assert r.returncode == 0 and "--env-file" not in r.stdout, r.stdout
    # an interpreter that is not 3.11 or newer is refused by name
    fake = os.path.join(c.home, "old-python")
    with open(fake, "w") as f:
        f.write("#!/bin/sh\nexit 1\n")
    os.chmod(fake, 0o755)
    r = subprocess.run(["bash", LAUNCHER, "--print"], env=dict(env, TANDEM_APP_PYTHON=fake),
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and "TANDEM_APP_PYTHON" in r.stderr, (r.returncode, r.stderr)
    # the unit: a user unit that runs the launcher with the state dir
    text = open(UNIT).read()
    body = [x for x in text.splitlines() if x and not x.startswith("#")]
    assert "[Service]" in body and "Type=exec" in body and "Restart=on-failure" in body
    assert "Environment=TANDEM_APP_STATE=%h/.tandemllm" in body
    assert "ExecStart=/bin/bash %h/tandemapp/packaging/linux/tandem-app.sh" in body
    assert "WantedBy=default.target" in body
    assert os.access(LAUNCHER, os.X_OK)
    return "defaults only when unset, env file, interpreter, the unit"


# ---------------------------------------------------------------------------- 2 no engine here

def test_serves_with_no_engine():
    srv, port = fake_backend.start(models=["fake-remote"], key=KEY)
    c = Ctl("serve", lines=["TANDEM_BACKENDS=eng", "TANDEM_BACKEND_ENG_KIND=generic",
                            f"TANDEM_BACKEND_ENG_URL=http://127.0.0.1:{port}/v1",
                            f"TANDEM_BACKEND_ENG_KEY={KEY}"])
    try:
        # nothing of an engine is imported by the controller, chatd or the take
        r = subprocess.run([sys.executable, "-I", "-c",
                            "import sys; sys.path.insert(0, sys.argv[1]); import proxy.app, "
                            "proxy.appchat, proxy.ctlmove, proxy.enginectl, chatd.service; "
                            "print(' '.join(sorted({m.split('.')[0] for m in sys.modules})))",
                            ROOT], env=c.env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        mods = set(r.stdout.split())
        assert not mods & {"torch", "numpy", "server", "engine", "triton"}, mods
        c.start()
        st, h = c.get("/health")
        assert st == 200 and h["status"] == "ok", h
        st, page = c.get("/dashboard/")
        assert st == 200 and b"apphost-page" in page, (st, page[:200])
        assert _until(lambda: any(m["id"] == "fake-remote" for m in
                                  (c.get("/v1/models")[1] or {}).get("data", [])), 30), \
            c.get("/v1/models")
        st, out = c.send("POST", "/v1/chat/completions", {
            "model": "fake-remote", "messages": [{"role": "user", "content": "FAKE_SAY hello"}]})
        assert st == 200 and out["choices"][0]["message"]["content"], out
        assert srv.fake.calls, "the fake engine saw no completion"
        st, ctl = c.get("/v1/fleet/controller")
        assert st == 200 and ctl["host"]["name"] and ctl["moved_to"] is None, ctl
        st, chat = c.get("/v1/fleet/chat")
        assert st == 200 and chat["owner"] == "external", chat
        c.stop()
        assert "Traceback" not in c.tail(400), c.tail()
        for d in ENGINE_DIRS:
            assert not os.path.exists(os.path.join(c.home, d)), d
        stray = [f for f in c.files() if not f.startswith(".tandemllm/") and
                 f != "controller.log"]
        assert not stray, stray
        assert KEY not in open(c.log, errors="replace").read()
    finally:
        c.stop()
        srv.shutdown()
    return "health, page, models and a completion of a remote engine; nothing of an engine"


# ---------------------------------------------------------------------------- 3 across hosts

def _cfg(d: str) -> config_mod.ConfigDB:
    return config_mod.ConfigDB(os.path.join(d, "config.sqlite3"), env={},
                               use_keychain=False).open()


def _ssh_host(cfg, root: str, hid: str) -> dict:
    """An engine host over SSH in `cfg`: its key pair and its pinned host key under `root`."""
    keys = hostkeys.HostKeys(cfg)
    k = keys.create(hid)
    _, host_pub = hostkeys.generate()
    kind, b64 = host_pub.split()[:2]
    fp = enginectl.pin_host_key(root, hid, kind, b64)
    t = enginectl.ssh_trust(host="engine-a.example", user="tandem", key_id=k["key_id"],
                            host_key_type=kind, host_key=fp)
    return cfg.add_node({"id": hid, "alias": "engine-one", "transport": "ssh",
                         "endpoint": "ssh://tandem@engine-a.example:22",
                         "address": "engine-a.example", "trust": t})


def test_take_across_hosts():
    import test_fleet as tf
    gd = tempfile.mkdtemp(prefix="apphost-giver-")
    giver = _cfg(gd)
    groot = os.path.join(gd, "controller")
    giver.create_backend({"id": "eng", "kind": "tandemllm", "url": "http://127.0.0.1:8000/v1",
                          "key": KEY, "host": {"mode": "collector",
                                               "url": "http://localhost:9108"}})
    giver.create_backend({"id": "lan", "kind": "generic", "url": "http://192.0.2.9:8000/v1"})
    giver.create_backend({"id": "ds", "kind": "deepseek", "key": "sk-fixture-ZYXWVUTS9876"})
    _, own_key = giver.create_user({"label": "laptop"})
    # the engine's own config database (TANDEM_API_USERS_FROM): a file on the giver's host
    ed = tempfile.mkdtemp(prefix="apphost-engine-db-")
    eng_db = _cfg(ed)
    _, eng_key = eng_db.create_user({"label": "phone"})
    eng_db.create_user({"label": "laptop"})                 # a label the giver has: it keeps its
    eng_db.close()
    giver.users_from = os.path.join(ed, "config.sqlite3")
    _ssh_host(giver, groot, "eh-1")
    private = giver.host_key_private("eh-1")
    with Proxy() as px:
        f = tf.fleet_on(px, tf.local_node(tf.fake_root(), h=tf.host(gpu=False)))
        tr = f.trust()
        px.svc.cfg.set_setting("controller.moved_to", {"api_url": "http://old"})
        # a backend the taker had before, on its own loopback: the take leaves it
        px.svc.cfg.create_backend({"id": "mine", "kind": "generic",
                                   "url": "http://127.0.0.1:1234/v1"})
        nodes = [{"id": "eh-1", "alias": "engine-one", "how": "carry", "why": None}]
        bundle = ctlmove.export(giver, host_keys=["eh-1"], ssh_root=groot)
        assert bundle["users_from"] == 1 and set(bundle["host_keys"]) == {"eh-1"}, bundle.keys()
        bundle["from"] = {"name": "TandemApp on engine-box", "fingerprint": "x",
                          "api_url": "http://engine-box.example:8091"}
        sealed = ctlmove.seal(bundle, tr.pem)
        assert private.splitlines()[1] not in json.dumps(sealed)
        asked = []

        def call(url, method, path, body=None, token=None, timeout=20.0, cookie=None):
            asked.append((url, method, path))
            assert url == "http://engine-box.example:8091", url
            # SEC-28: the password signs in once; no call carries it as a bearer
            assert token is None and cookie == {"cookie": "tandem_session=s", "csrf": "c"}, \
                (token, cookie)
            if path == "/v1/fleet/controller":
                return 200, {"host": {"name": "engine-box"}}
            if path == "/v1/fleet/controller/handover":
                assert body["certificate"] == tr.pem
                return 200, {"id": "h-1", "sealed": sealed, "nodes": nodes}
            if path == "/v1/fleet/controller/handover/h-1/commit":
                return 200, {"nodes": [dict(x, state="handed") for x in nodes]}
            return 404, {}
        def no_call(*a, **k):
            raise AssertionError("a pull whose password does not sign in calls nothing")
        m0 = ctlmove.Mover(f, call=no_call, sign_in=lambda u, p: None, spawn=lambda fn: fn(),
                           sleep=lambda s: None)
        m0.take({"url": "http://engine-box.example:8091", "password": "wrong-" + ADMIN_A},
                "admin", time.time())
        j0 = tf.ok(px.svc.cfg.job("move"), "move-job")
        assert j0["state"] == "failed" and j0["steps"][0]["state"] == "failed", j0
        assert "sign_in_failed" in json.dumps(j0), j0
        signed = []

        def sign_in(u, p):
            signed.append((u, p))
            return {"cookie": "tandem_session=s", "csrf": "c"}
        m = ctlmove.Mover(f, call=call, sign_in=sign_in, spawn=lambda fn: fn(),
                          sleep=lambda s: None)
        job = m.take({"url": "http://engine-box.example:8091", "password": ADMIN_A},
                     "admin", time.time())
        j = tf.ok(px.svc.cfg.job("move"), "move-job")
        assert j["id"] == job["id"] and j["state"] == "done", j
        assert signed == [("http://engine-box.example:8091", ADMIN_A)], signed
        cfg = px.svc.cfg
        # loopback backends point at the host the giver was reached at; the rest stay
        assert cfg.backend("eng")["url"] == "http://engine-box.example:8000/v1"
        assert cfg.backend("eng")["host"]["url"] == "http://engine-box.example:9108"
        assert cfg.backend("lan")["url"] == "http://192.0.2.9:8000/v1"
        assert cfg.backend("mine")["url"] == "http://127.0.0.1:1234/v1"
        assert cfg.backend("ds")["url"] == giver.backend("ds")["url"]
        assert {x["backend"] for x in j["rebased"]} == {"eng"}, j["rebased"]
        assert cfg.key_for("eng") == KEY
        # the engine's users came along as hashes; the giver's own label kept its key
        assert cfg.user_for_key(eng_key) == "phone" and cfg.user_for_key(own_key) == "laptop"
        assert cfg.users_from is None
        # the engine host: its key under this controller's master key, its host key pinned here
        assert cfg.host_key_private("eh-1") == private
        assert enginectl.pinned_key(tr.root, "eh-1") == enginectl.pinned_key(groot, "eh-1")
        assert cfg.node("eh-1")["transport"] == "ssh"
        assert [x.get("accepted") for x in j["nodes"]] == [True], j["nodes"]
        assert cfg.setting("controller.moved_to") is None
        assert [s["state"] for s in j["steps"]] == ["done"] * 5, j["steps"]
        assert any("loopback" in x["text"] for x in j["log"]), j["log"]
        for x in json.dumps(j), json.dumps(m.view(time.time())):
            assert KEY not in x and private.splitlines()[1] not in x
        # the same take from a giver on this machine changes no URL
        assert ctlmove.giver_host("http://127.0.0.1:8091") is None
        assert ctlmove.giver_host("http://localhost:8091") is None
        assert ctlmove.giver_host("http://[::1]:8091") is None
        assert ctlmove.giver_host("http://engine-box.example:8091") == "engine-box.example"
        assert ctlmove.rebase_loopback(cfg, None) == []
        assert ctlmove._rebase("http://[::1]:8000/v1", "fd00::5") == "http://[fd00::5]:8000/v1"
        for src in [s for _, s in f.agents.values()]:
            src.close()
    # the giver's half: the handover lists the host as carried, the commit drops its key here
    with Proxy() as px:
        f = tf.fleet_on(px, tf.local_node(tf.fake_root(), h=tf.host(gpu=False)))
        root = f.trust().root
        _ssh_host(px.svc.cfg, root, "eh-2")
        hd = tempfile.mkdtemp(prefix="apphost-taker-")
        from proxy import nodetrust
        cert, key = os.path.join(hd, "t.crt"), os.path.join(hd, "t.key")
        nodetrust.make_identity(cert, key, "taker")
        m = ctlmove.Mover(f, spawn=lambda fn: fn())
        h = tf.ok(m.handover({"certificate": open(cert).read(), "name": "taker",
                              "api_url": "http://pi.example:8080"}, "admin", time.time()),
                  "handover")
        assert [(x["id"], x["how"]) for x in h["nodes"] if x["id"].startswith("eh-")] == \
            [("eh-2", "carry")], h["nodes"]
        opened = ctlmove.unseal(h["sealed"], key)
        assert opened["host_keys"]["eh-2"]["private"] == px.svc.cfg.host_key_private("eh-2")
        got = tf.ok(m.commit(h["id"], "admin", time.time()), "handover-commit")
        assert got["nodes"][0]["state"] == "handed", got
        assert px.svc.cfg.host_key("eh-2") is None and enginectl.pinned_key(root, "eh-2") is None
        assert px.svc.cfg.node("eh-2") is None
        assert px.svc.cfg.setting("controller.moved_to")["api_url"] == "http://pi.example:8080"
        # a host with no key pair here stays, with the words why
        px.svc.cfg.delete_setting("controller.moved_to")
        _ssh_host(px.svc.cfg, root, "eh-3")
        hostkeys.HostKeys(px.svc.cfg).delete("eh-3")
        h = m.handover({"certificate": open(cert).read(), "name": "taker",
                        "api_url": "http://pi.example:8080"}, "admin", time.time())
        x = next(x for x in h["nodes"] if x["id"] == "eh-3")
        assert x["how"] == "stays" and "pair it" in x["why"], x
        assert "eh-3" not in (ctlmove.unseal(h["sealed"], key).get("host_keys") or {})
        for src in [s for _, s in f.agents.values()]:
            src.close()
    giver.close()
    return "loopback rebased, engine users, SSH host carried sealed, moved_to cleared"


# ---------------------------------------------------------------------------- 4 take, take back

def _node_up(ctl: Ctl, token: str, ids) -> bool:
    st, doc = ctl.get("/v1/fleet/nodes", token=token)
    if st != 200:
        return False
    got = {n["id"]: n["status"] for n in doc.get("nodes") or []}
    return set(ids) <= set(got) and all(got[i] in ("up", "alert", "draining") for i in ids)


def _job_done(ctl: Ctl, token: str, jid: str):
    st, v = ctl.get("/v1/fleet/controller", token=token)
    j = (v or {}).get("job") if st == 200 else None
    return j if j and j["id"] == jid and j["state"] != "running" else None


def _token(ctl: Ctl, who: dict, label: str, scopes: list) -> str:
    body = {"label": label, "scopes": scopes, **({"days": 1} if "trust" in scopes else {})}
    st, doc = ctl.send("POST", "/v1/auth/tokens", body, token=who)
    assert st == 201, (st, doc)
    return doc["token"]


def test_take_and_take_back():
    """SEC-28: each controller's first owner comes from its admin token of before (the setup
    code, once); a take carries a trust token made on the giver. The API tokens stay with the
    controller that made them (a move carries no account or token): clients get new ones."""
    import test_node_trust as tnt
    srv, port = fake_backend.start(models=["fake-remote"], key=KEY)
    # the engine's own users (TANDEM_API_USERS_FROM): a file on A's host only, imported at A's
    # first start as tokens with the same keys
    ed = tempfile.mkdtemp(prefix="apphost-engine-db-")
    eng_db = _cfg(ed)
    _, eng_key = eng_db.create_user({"label": "phone"})
    eng_db.close()
    a = Ctl("a", lines=[f"TANDEM_ADMIN_TOKEN={ADMIN_A}",
                        f"TANDEM_API_USERS_FROM={os.path.join(ed, 'config.sqlite3')}"])
    b = Ctl("b", lines=[f"TANDEM_ADMIN_TOKEN={ADMIN_B}"])
    node = tnt.Node("APPHOST-0001")
    try:
        a.start()
        b.start()
        wa, wb = a.owner(ADMIN_A), b.owner(ADMIN_B)
        st, made = a.send("POST", "/v1/config/backends", {
            "id": "eng", "kind": "generic", "url": f"http://127.0.0.1:{port}/v1", "key": KEY},
            token=wa)
        assert st in (200, 201), made
        own_key = _token(a, wa, "laptop", ["inference:openai"])
        w = node.window()
        st, n = a.send("POST", "/v1/fleet/pair", {"address": "127.0.0.1", "code": w["code"],
                                                  "port": w["port"]}, token=wa)
        assert st == 200, n
        nid = n["id"]
        assert _until(lambda: _node_up(a, wa, [nid]), 30)

        def chat(ctl, key):
            return ctl.send("POST", "/v1/chat/completions", {
                "model": "fake-remote", "messages": [{"role": "user", "content": "hi"}]},
                token=key, headers=FAR)
        assert _until(lambda: chat(a, own_key)[0] == 200, 30), chat(a, own_key)
        assert chat(a, eng_key)[0] == 200
        # B takes the fleet from A, with a trust token made on A
        st, job = b.send("POST", "/v1/fleet/controller/take",
                         {"url": a.url, "token": _token(a, wa, "move-to-b", ["trust"])},
                         token=wb)
        assert st == 200 and job["direction"] == "pull", job
        j = _until(lambda: _job_done(b, wb, job["id"]), 90)
        assert j and j["state"] == "done", (j, b.tail())
        assert _until(lambda: _node_up(b, wb, [nid]), 30), b.get("/v1/fleet/nodes", wb)
        # the tokens stayed on A: a new one made on B answers there
        assert chat(b, own_key)[0] == 401
        b_key = _token(b, wb, "laptop", ["inference:openai"])
        # B never had keys: its API is this machine's until an owner opens it
        assert chat(b, b_key)[0] == 403
        mine = {"model": "fake-remote", "messages": [{"role": "user", "content": "hi"}]}
        assert _until(lambda: b.send("POST", "/v1/chat/completions", mine,
                                     token=b_key)[0] == 200, 30)
        st, bk = b.get("/v1/config/backends/eng", token=wb)
        assert st == 200 and bk["key"]["key_set"] and bk["url"].startswith("http://127.0.0.1:"), bk
        st, va = a.get("/v1/fleet/controller", token=wa)
        assert va["moved_to"]["api_url"] == b.url, va
        assert nid not in [x["id"] for x in a.get("/v1/fleet/nodes", token=wa)[1]["nodes"]]
        # A takes it back the same way: its old moved_to goes
        st, job = a.send("POST", "/v1/fleet/controller/take",
                         {"url": b.url, "token": _token(b, wb, "move-to-a", ["trust"])},
                         token=wa)
        assert st == 200, job
        j = _until(lambda: _job_done(a, wa, job["id"]), 90)
        assert j and j["state"] == "done", (j, a.tail())
        assert _until(lambda: _node_up(a, wa, [nid]), 30)
        st, va = a.get("/v1/fleet/controller", token=wa)
        assert va["moved_to"] is None, va
        assert b.get("/v1/fleet/controller", token=wb)[1]["moved_to"]["api_url"] == a.url
        assert chat(a, own_key)[0] == 200 and chat(a, eng_key)[0] == 200
        a.stop()
        b.stop()
        for c in (a, b):
            assert "Traceback" not in c.tail(1000), c.tail()
            for d in ENGINE_DIRS:
                assert not os.path.exists(os.path.join(c.home, d)), d
            assert KEY not in open(c.log, errors="replace").read()
            # the stored key is ciphertext in each controller's database
            con = sqlite3.connect(os.path.join(c.state, "config.sqlite3"))
            assert con.execute("SELECT COUNT(*) FROM backend_secrets").fetchone()[0] == 1
            con.close()
    finally:
        a.stop()
        b.stop()
        node.close()
        srv.shutdown()
    return "B took A's fleet with a trust token, A took it back; keys sealed, tokens stay"


# ---------------------------------------------------------------------------- 5 own chatd

def _environ(pid: int) -> dict | None:
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            raw = f.read()
    except OSError:
        return None                                  # no /proc here (macOS)
    return dict(x.split("=", 1) for x in raw.decode(errors="replace").split("\0") if "=" in x)


def _kill_chatd(data: str) -> None:
    try:
        pid = int(open(os.path.join(data, "chatd.pid")).read().strip())
        os.kill(pid, signal.SIGKILL)
    except (OSError, ValueError):
        pass


def test_controller_owned_chatd():
    srv, port = fake_backend.start(models=["fake-remote"])
    chat_port = free_port()
    c = Ctl("chat", lines=["TANDEM_CHAT=own", f"TANDEM_ADMIN_TOKEN={ADMIN_A}",
                           "TANDEM_BACKENDS=eng", "TANDEM_BACKEND_ENG_KIND=generic",
                           f"TANDEM_BACKEND_ENG_URL=http://127.0.0.1:{port}/v1"],
            env={"QSE_CHAT_FORWARD_KEY": "decoy-forward-key-of-another-chatd-000000"})
    data = os.path.join(c.state, "chat")
    with open(os.path.join(c.state, "chatd.env"), "w") as f:
        f.write(f"QSE_CHAT_URL=http://127.0.0.1:{chat_port}\nQSE_CHAT_MAX_RUNS=2\n"
                f"export QSE_CHAT_RETRY_S='30'\n")
    try:
        c.start()
        who = c.owner(ADMIN_A)              # SEC-28: the admin token of before, once
        v = _until(lambda: (lambda d: d if d.get("state") == "running" and d.get("answers")
                            else None)(c.get("/v1/fleet/chat", who)[1]), 40)
        assert v, (c.get("/v1/fleet/chat", who), c.tail())
        # SRV-843: chatd's engine is the gateway as a whole engine API; no engine here
        assert v["owner"] == "controller" and v["engine"] == c.url + "/chat-engine", v
        assert v["host"] == "app", v
        assert v["url"] == f"http://127.0.0.1:{chat_port}", v
        assert os.path.realpath(v["data"]) == os.path.realpath(data), v
        pid = v["pid"]
        # the forward reaches this chatd with its own key; chatd lists the remote engine's
        # models through this controller's gateway
        caps = _until(lambda: (lambda r: r[1] if r[0] == 200 and "fake-remote" in json.dumps(
            r[1]) else None)(c.get("/v1/chat-app/capabilities", who)), 30)
        assert caps, c.get("/v1/chat-app/capabilities", who)
        assert os.path.isfile(os.path.join(data, "forward.key"))
        env = _environ(pid)
        if env is not None:
            assert env["QSE_CHAT_ENGINE"] == c.url + "/chat-engine" and env["QSE_CHAT_HOST"] == "app"
            assert env["QSE_CHAT_MAX_RUNS"] == "2"
            assert env["QSE_CHAT_RETRY_S"] == "30" and env["HOME"] == c.home
            for k in ("TANDEM_ADMIN_TOKEN", "TANDEM_BACKEND_ENG_URL", "QSE_CHAT_FORWARD_KEY",
                      "TANDEM_CHAT", "QSE_TEST"):
                assert k not in env, k
        # stop: the chat answers 502; the watcher holds it; start again
        st, job = c.send("POST", "/v1/fleet/chat/stop", token=who)
        assert st == 200 and job["action"] == "stop", job
        v = _until(lambda: (lambda d: d if (d.get("job") or {}).get("state") == "done" else None)(
            c.get("/v1/fleet/chat", who)[1]), 40)
        assert v and v["state"] == "stopped" and v["watch"]["stopped_by_hand"], v
        assert c.get("/v1/chat-app/capabilities", who)[0] == 502
        st, job = c.send("POST", "/v1/fleet/chat/start", token=who)
        assert st == 200, job
        v = _until(lambda: (lambda d: d if (d.get("job") or {}).get("state") == "done" else None)(
            c.get("/v1/fleet/chat", who)[1]), 40)
        assert v and v["state"] == "running" and not v["watch"]["stopped_by_hand"], v
        pid = v["pid"]
        assert c.send("POST", "/v1/fleet/chat/jump", token=who)[0] == 404
        # a controller restart leaves chatd running; the new one adopts it
        c.stop()
        assert appchat.is_chatd(pid), "the controller's stop ended chatd"
        c.start()
        v = _until(lambda: (lambda d: d if d.get("state") == "running" else None)(
            c.get("/v1/fleet/chat", who)[1]), 30)
        assert v and v["pid"] == pid, (v, pid)
        assert c.get("/v1/chat-app/capabilities", who)[0] == 200
        st, job = c.send("POST", "/v1/fleet/chat/stop", token=who)
        assert _until(lambda: not appchat.is_chatd(pid), 40)
        c.stop()
        assert "Traceback" not in c.tail(1000), c.tail()
        log = open(os.path.join(c.state, "logs", "chatd.log"), errors="replace").read()
        assert "[chatd]" in log and "decoy-forward" not in log
        assert ADMIN_A not in log
    finally:
        c.stop()
        _kill_chatd(data)
        srv.shutdown()
    return "started with its own env, engine = the gateway, forward, stop/start, adopted"


# ---------------------------------------------------------------------------- 6 the watcher

def test_chatd_watcher():
    srv, port = fake_backend.start(models=["fake-remote"])
    state = tempfile.mkdtemp(prefix="apphost-watch-")
    chat_port = free_port()
    now = [1_800_000_000.0]
    lines: list[str] = []
    env = {"PATH": os.environ.get("PATH", ""), "HOME": state, "TANDEM_ADMIN_TOKEN": ADMIN_A}
    ch = appchat.AppChat(state, {"QSE_CHAT_URL": f"http://127.0.0.1:{chat_port}",
                                 "QSE_CHAT_ENGINE": f"http://127.0.0.1:{port}"},
                         env=env, log=lines.append, clock=lambda: now[0], which=lambda n: None)
    try:
        assert "systemd-run" not in ch.argv()
        assert ch.child_env()["QSE_CHAT_ENGINE"] == f"http://127.0.0.1:{port}"
        assert "TANDEM_ADMIN_TOKEN" not in ch.child_env()
        scoped = appchat.AppChat(state, {}, env=dict(env, INVOCATION_ID="x",
                                                     XDG_RUNTIME_DIR="/run/user/1"),
                                 which=lambda n: "/usr/bin/" + n)
        assert scoped.argv()[:4] == ["systemd-run", "--user", "--scope", "--quiet"], scoped.argv()
        assert ch.tick() == "started", lines
        pid = ch.status()["pid"]
        assert ch.tick() == "running"
        # a crash: started again, the count goes up
        os.kill(pid, signal.SIGKILL)
        assert _until(lambda: not appchat.is_chatd(pid), 10)
        assert ch.tick() == "started", lines
        assert ch.tick() == "running"            # a running chatd clears the count
        assert ch._watch().get("n") is None
        # three starts an hour that do not keep it: then one line, nothing more this hour
        for i in range(3):
            _kill_chatd(ch.data)
            _until(lambda: not ch.running(), 10)
            ch._update(hour=ch.hour(), n=i)
            assert ch.tick() == "started"
        _kill_chatd(ch.data)
        _until(lambda: not ch.running(), 10)
        assert ch.tick() == "gave_up" and ch.tick() == "gave_up"
        assert sum("giving up" in x for x in lines) == 1, lines
        now[0] += 3600
        assert ch.tick() == "started"
        # a hand stop holds the watcher until a hand start
        assert ch.run_action("stop")[0] == 0 and not ch.running()
        assert ch.tick() == "stopped_by_hand"
        rc, msg = ch.run_action("restart")
        assert rc == 0 and ch.running() and ch.tick() == "running", msg
        # a port another program holds: refused with the words
        ch.run_action("stop")
        other = fake_backend.start(port=chat_port)[0]
        try:
            rc, msg = ch.start()
            assert rc == 1 and "taken by another program" in msg, msg
        finally:
            other.shutdown()
            other.server_close()
        bad = os.path.join(state, "bad.env")
        with open(bad, "w") as f:
            f.write("QSE_CHAT_URL http://x\n")
        try:
            appchat.AppChat.load(state, env={"TANDEM_CHAT_ENV": bad})
            raise AssertionError("a bad line was read")
        except ValueError as exc:
            assert "bad.env:1" in str(exc) and "http://x" not in str(exc)
        try:
            appchat.AppChat(state, {"QSE_CHAT_URL": "http://127.0.0.1"})
            raise AssertionError("a URL with no port")
        except ValueError:
            pass
    finally:
        ch.stop()
        srv.shutdown()
    return "crash restarted, 3 an hour, hand stop holds, port taken refused"


# ---------------------------------------------------------------------------- 7 TandemNode's path

def test_external_chat_stays():
    from tandemapp import chat_forward
    with Proxy(env={"QSE_CHAT_URL": "http://127.0.0.1:18016"}) as px:
        st, _, out = px.get("/v1/fleet/chat")
        assert st == 200 and json.loads(out)["owner"] == "external", out
        st, _, out = px.request("POST", "/v1/fleet/chat/start", {})
        assert st == 409 and json.loads(out)["error"]["type"] == "not_owned", out
        assert chat_forward._OWN == {}
        # the client names: the controller's own clients.json first, else the engine's here
        from proxy import app as proxy_app
        from tandemapp import dashboard_api
        assert proxy_app.clients_path() == dashboard_api.CLIENTS_JSON
        own = os.path.join(os.path.dirname(px.svc.cfg.path), "clients.json")
        with open(own, "w") as f:
            json.dump({"abc123": "laptop"}, f)
        assert proxy_app.clients_path() == own
        old = os.environ.get("QSE_CHAT_URL")
        os.environ["QSE_CHAT_URL"] = "http://127.0.0.1:18016"
        try:
            assert chat_forward.target() == ("127.0.0.1", 18016)
        finally:
            if old is None:
                os.environ.pop("QSE_CHAT_URL")
            else:
                os.environ["QSE_CHAT_URL"] = old
    # an own chatd's settings win over the process environment, and clear again
    chat_forward.configure(url="http://127.0.0.1:18999", data="/nonexistent-data",
                           key=lambda: "own-key")
    try:
        assert chat_forward.target() == ("127.0.0.1", 18999)
        assert chat_forward.forward_key() == "own-key"
        assert chat_forward.data_dir() == "/nonexistent-data"
    finally:
        chat_forward.configure()
    assert chat_forward._OWN == {}
    # TandemNode's chatd on an engine box: its defaults beside the engine, as before
    d = tempfile.mkdtemp(prefix="apphost-node-")
    ch = nodechat.Chat.load(d, tandem_dir="~/eng", own=True)
    assert ch.serve_env.endswith("/eng/run/ops/serve.env") and ch.own
    assert ch.chat_env.endswith("/eng/run/chatd.env")
    return "external forwards to QSE_CHAT_URL and refuses the verbs; TandemNode's launcher stays"


if __name__ == "__main__":
    run(globals())
