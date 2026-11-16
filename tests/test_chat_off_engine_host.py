"""SRV-843: chatd off the engine host (the plan's C2). chatd runs on the controller's host; its
engine is the controller's gateway as a whole engine API (proxy/chatengine.py), the engine a
TandemLLM backend on another host that wants its HTTP token (engine API 1.9).

  1  the controller's engine for chatd: /health and the facts of the chat engine (chatd's run slots
     follow its lanes through it), the live rows as a snapshot and as a stream passed through, the
     cache routes (with a model: the member that holds it; without: the chat engine), a release
     only for an engine on this host, tokenize by the model's route, the model list, 404 for the
     rest; each call to the engine carries its HTTP token; the access rules for a caller from
     another machine; no token in a log line or a ledger row      test_the_chat_engine_routes
  2  a user's skill goes into the controller's conductor as an inline body (the library lists it,
     its text is the body), never by a path into skill_dirs      test_a_skill_goes_to_the_controllers_conductor
  3  the sandbox defaults by host kind: 2 GiB, 2 running, floor 1.5 on a host without the engine;
     4, 4, 4 beside it (unset); a variable wins                  test_sandbox_defaults_by_host_kind
  4  the memory guard on a host without the engine never asks the engine to release memory; beside
     the engine it still does                                    test_the_guard_never_asks_a_remote_engine
  5  the controller's chatd: its engine URL, its host kind (from where the chat engine runs, the env
     file's word first), its own key once the controller has users (made, kept 0600, reused, made
     again when its user is gone; never over the env file's)    test_appchat_host_kind_and_engine_key
  6  the chat data move between two sandbox HOMEs (`tandem-app chat-move`): a stopped chatd's data
     copied file by file, checked by sha256, the MCP secrets sealed again under the new host's key,
     the old folder untouched; the chatd here started on it: the chats are there, the run the old
     one left open is interrupted and Continue finishes it     test_chat_move_between_two_homes
  7  the move's plan and refusals: a source chatd that runs, a destination with chats (and
     --replace keeping it), sealed MCP secrets without the old key (and --without-mcp-secrets), not
     a data folder; --plan changes nothing                       test_chat_move_refusals
  8  the move over SSH: the look, the key and the tar run as one quoted command each through a
     stand-in ssh that runs it as sshd runs a command            test_chat_move_over_ssh

CPU only, standard library plus `cryptography`. Run: python tests/test_chat_off_engine_host.py
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fake_backend  # noqa: E402
from chatd_util import Chat, text_of  # noqa: E402
from ports import free_port  # noqa: E402
from proxy import appchat, chatmove, enginectl  # noqa: E402
from proxy import config as config_mod  # noqa: E402

TOKEN = "engine-http-token-" + "fedcba9876543210" * 3
MCP_SECRET = "mcp-fixture-secret-0123456789abcdef"
SKILL = ("---\nname: tea-timer\ndescription: Brew times for green, black and oolong tea.\n---\n"
         "Green: 2 minutes at 80 C.\n")


def _until(fn, s=20.0, step=0.05):
    end = time.time() + s
    while time.time() < end:
        got = fn()
        if got:
            return got
        time.sleep(step)
    return None


class Engine19:
    """A TandemLLM engine on API 1.9 that wants its HTTP token: /health is open (the full body only
    with the token), every other route answers 401 without it. It records what it was asked."""

    def __init__(self):
        self.seen: list[tuple[str, str, str | None]] = []
        eng = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, doc, ctype="application/json"):
                raw = doc if isinstance(doc, bytes) else json.dumps(doc).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _authed(self):
                return self.headers.get("Authorization") == f"Bearer {TOKEN}"

            def do_GET(self):
                p = self.path.split("?")[0]
                eng.seen.append(("GET", self.path, self.headers.get("Authorization")))
                if p == "/health":
                    if not self._authed():
                        return self._send(200, {"status": "ok"})
                    return self._send(200, {"status": "ok", "engine_api": "1.9", "max_len": 32768,
                                            "model": "fake-a", "parallel": {"n": 3}})
                if not self._authed():
                    return self._send(401, {"error": {"type": "unauthorized", "message": "token"}})
                if p == "/v1/models":
                    return self._send(200, {"object": "list", "data": [
                        {"id": "fake-a", "object": "model", "owned_by": "tandemllm",
                         "max_model_len": 32768}]})
                if p == "/v1/dashboard/system":
                    return self._send(200, {"engine": {"max_len": 32768, "weights": {"name": "w"}},
                                            "queue": {"parallel": {"n": 3}}, "memory": {}})
                if p == "/v1/dashboard/live":
                    if "follow=0" in self.path:
                        return self._send(200, {"requests": [{"conversation_id": "c1",
                                                              "phase": "decode"}], "engine": {}})
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    for i in range(3):
                        self.wfile.write(f"data: {json.dumps({'requests': [], 'n': i})}\n\n"
                                         .encode())
                        self.wfile.flush()
                    self.close_connection = True
                    return None
                return self._send(404, {"error": {"type": "not_found", "message": p}})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                eng.seen.append(("POST", self.path, self.headers.get("Authorization")))
                if not self._authed():
                    return self._send(401, {"error": {"type": "unauthorized", "message": "token"}})
                if self.path == "/v1/cache/peek":
                    return self._send(200, {"tier": "gpu", "model": body.get("model")})
                if self.path == "/v1/cache/release":
                    return self._send(200, {"released_bytes": int(body.get("bytes") or 0),
                                            "levels": {}})
                if self.path == "/v1/tokenize":
                    return self._send(200, {"tokens": 7, "model": body.get("model")})
                return self._send(404, {"error": {"type": "not_found", "message": self.path}})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def _req(port, method, path, body=None, headers=None, raw=False):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    h = {"Content-Type": "application/json", **(headers or {})}
    c.request(method, path, None if body is None else json.dumps(body).encode(), h)
    r = c.getresponse()
    data = r.read()
    c.close()
    if raw:
        return r.status, data
    try:
        return r.status, json.loads(data or b"null")
    except ValueError:
        return r.status, data


def _remote(cfg, host_id="eh-chat"):
    """An engine host over SSH at 127.0.0.1 (counted as another host here) with its token."""
    t = enginectl.ssh_trust(host="127.0.0.1", user="tandem", key_id="tandemapp-0123456789",
                            host_key_type="ssh-ed25519", host_key="SHA256:" + "A" * 43)
    cfg.add_node({"id": host_id, "alias": "engine-host", "transport": "ssh",
                  "endpoint": "ssh://tandem@127.0.0.1:22", "address": "127.0.0.1", "trust": t})
    cfg.put_engine_token(host_id, TOKEN)


# ---------------------------------------------------------------------------- 1 the routes

def test_the_chat_engine_routes(monkeypatch):
    from chatd.engine import Engine
    from chatd.service import engine_lanes
    from proxy import app as proxy_app
    from proxy import tokens as tokens_mod
    from proxy_util import Proxy, api_key, owner
    eng = Engine19()
    monkeypatch.setattr(config_mod, "is_loopback_host", lambda h: False)
    try:
        with Proxy(env={"TANDEM_BACKENDS": "eng", "TANDEM_BACKEND_ENG_KIND": "tandemllm",
                        "TANDEM_BACKEND_ENG_URL": f"http://127.0.0.1:{eng.port}/v1"}) as px:
            _remote(px.svc.cfg)
            px.sync()
            # SEC-28: /chat-engine is chatd's: its service identity, never this machine alone
            adm = owner(px)
            chatd = proxy_app.STATE["accounts"].tokens.mint_service(
                tokens_mod.CHATD_SERVICE, tokens_mod.CHATD_SCOPES,
                memory_user=tokens_mod.CHATD_LABEL)
            C = {"Authorization": f"Bearer {chatd}"}
            P = "/chat-engine"
            assert _req(px.port, "GET", P + "/v1/models")[0] == 401
            st, h = _req(px.port, "GET", P + "/health", headers=C)
            assert st == 200 and h["max_len"] == 32768 and h["parallel"] == {"n": 3}, h
            facts = Engine(f"http://127.0.0.1:{px.port}{P}", chatd).facts()
            assert engine_lanes(facts) == 3, facts           # chatd's run slots follow the lanes
            st, sysd = _req(px.port, "GET", P + "/v1/dashboard/system", headers=C)
            assert st == 200 and sysd["engine"]["weights"] == {"name": "w"}, sysd
            st, snap = _req(px.port, "GET", P + "/v1/dashboard/live?follow=0", headers=C)
            assert st == 200 and snap["requests"][0]["conversation_id"] == "c1", snap
            st, raw = _req(px.port, "GET", P + "/v1/dashboard/live", headers=C, raw=True)
            frames = [json.loads(x[6:]) for x in raw.decode().split("\n\n") if
                      x.startswith("data: ")]
            assert st == 200 and [f["n"] for f in frames] == [0, 1, 2], raw[:300]
            st, models = _req(px.port, "GET", P + "/v1/models", headers=C)
            assert st == 200 and "fake-a" in [m["id"] for m in models["data"]], models
            st, peek = _req(px.port, "POST", P + "/v1/cache/peek", {"model": "fake-a",
                                                                    "messages": []}, headers=C)
            assert st == 200 and peek["tier"] == "gpu" and peek["model"] == "fake-a", peek
            st, peek = _req(px.port, "POST", P + "/v1/cache/peek", {"messages": []}, headers=C)
            assert st == 200 and peek["tier"] == "gpu", peek          # no model: the chat engine
            n = len([s for s in eng.seen if s[1] == "/v1/cache/release"])
            st, rel = _req(px.port, "POST", P + "/v1/cache/release", {"bytes": 1 << 30}, headers=C)
            assert st == 200 and rel == {"released_bytes": 0, "levels": {},
                                         "skipped": "no_engine_here"}, rel
            assert len([s for s in eng.seen if s[1] == "/v1/cache/release"]) == n
            st, tok = _req(px.port, "POST", P + "/v1/tokenize", {"model": "fake-a",
                                                                 "messages": []}, headers=C)
            assert st == 200 and tok["tokens"] == 7, tok
            assert _req(px.port, "GET", P + "/v1/bogus", headers=C)[0] == 404
            assert _req(px.port, "POST", P + "/v1/bogus", {}, headers=C)[0] == 404
            # a body a refusal never read ends the connection: a request sent after it on the
            # same connection is never read as the rest of that body
            import socket
            sk = socket.create_connection(("127.0.0.1", px.port), timeout=10)
            auth = f"Authorization: Bearer {chatd}\r\n".encode()
            sk.sendall(b"POST /chat-engine/v1/bogus HTTP/1.1\r\nHost: x\r\nContent-Length: 5"
                       b"\r\nContent-Type: application/json\r\n" + auth + b"\r\nabcde"
                       b"GET /chat-engine/v1/models HTTP/1.1\r\nHost: x\r\n" + auth + b"\r\n")
            got = b""
            while True:
                chunk = sk.recv(65536)
                if not chunk:
                    break
                got += chunk
            sk.close()
            assert got.count(b"HTTP/1.1 ") == 1 and got.startswith(b"HTTP/1.1 404"), got[:200]
            # every call that reached the engine carried its token (the open /health aside)
            assert eng.seen and all(a == f"Bearer {TOKEN}" for m, p, a in eng.seen
                                    if not p.startswith("/health")), eng.seen
            # a caller from another machine: the facts need a sign-in (the dashboard's reads);
            # the rest is chatd's: a person's session or an inference token is refused
            far = {"X-Real-IP": "192.0.2.5"}
            assert _req(px.port, "GET", P + "/v1/dashboard/system", headers=far)[0] == 401
            assert _req(px.port, "GET", P + "/health", headers=far)[1] == {"status": "ok"}
            assert _req(px.port, "GET", P + "/v1/dashboard/system", headers={**far, **adm})[0] \
                == 200
            key = api_key(px, "laptop", network=True)
            for path in (P + "/v1/models", P + "/v1/dashboard/system"):
                assert _req(px.port, "GET", path, headers=far)[0] == 401, path
                assert _req(px.port, "GET", path, headers={
                    **far, "Authorization": f"Bearer {key}"})[0] == 403, path
            assert _req(px.port, "GET", P + "/v1/models", headers={**far, **adm})[0] == 403
            assert _req(px.port, "POST", P + "/v1/cache/release", {"bytes": 1},
                        headers=far)[0] == 401
            # an engine on this host: a sandbox here shares its memory, so the release goes on
            monkeypatch.setattr(config_mod, "is_loopback_host", lambda h: h == "127.0.0.1")
            px.svc.cfg.create_backend({"id": "eng-here", "kind": "tandemllm",
                                       "url": f"http://127.0.0.1:{eng.port}/v1",
                                       "key": TOKEN})
            monkeypatch.setenv("TANDEM_CHAT_ENGINE_BACKEND", "eng-here")
            st, rel = _req(px.port, "POST", P + "/v1/cache/release", {"bytes": 4096}, headers=C)
            assert st == 200 and rel["released_bytes"] == 4096, rel
            logs = "\n".join(px.logs)
            rows = json.dumps(px.rows(), default=str)
            assert TOKEN not in logs and TOKEN not in rows
    finally:
        eng.close()
    return "health, facts, live (snapshot and stream), cache, release, tokenize, models, " \
           "404, token on every call, access from another machine"


# ---------------------------------------------------------------------------- 2 skills

def _zip(files: dict) -> bytes:
    import io
    import zipfile
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return b.getvalue()


def _multipart(name: str, data: bytes) -> tuple[str, bytes]:
    bnd = "----tandemtest"
    body = (f"--{bnd}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
            f"Content-Type: application/zip\r\n\r\n").encode() + data + f"\r\n--{bnd}--\r\n".encode()
    return f"multipart/form-data; boundary={bnd}", body


def test_a_skill_goes_to_the_controllers_conductor():
    from proxy import app as proxy_app
    from proxy_util import Proxy
    fk = fake_backend.start(models=["fake-a"])
    try:
        with Proxy(env={"TANDEM_BACKENDS": "eng", "TANDEM_BACKEND_ENG_KIND": "tandemllm",
                        "TANDEM_BACKEND_ENG_URL": f"http://127.0.0.1:{fk[1]}/v1"},
                   conductor=True) as px:
            px.sync()
            from proxy import tokens as tokens_mod
            chatd = proxy_app.STATE["accounts"].tokens.mint_service(       # SEC-28: chatd's own
                tokens_mod.CHATD_SERVICE, tokens_mod.CHATD_SCOPES,
                memory_user=tokens_mod.CHATD_LABEL)
            C = {"Authorization": f"Bearer {chatd}"}
            c = Chat(engine_url=f"http://127.0.0.1:{px.port}/chat-engine", engine_key=chatd)
            try:
                ct, body = _multipart("tea.zip", _zip({"tea-timer/SKILL.md": SKILL,
                                                       "tea-timer/notes.md": "x"}))
                pkg = c.ok("POST", "/packages/upload", raw=body, headers={"Content-Type": ct})
                assert pkg["id"] == "tea-timer" and pkg["user"] is True, pkg
                st, doc = _req(px.port, "GET", "/v1/conductor/packages")
                assert st == 200, doc
                p = doc["doc"]["packages"]["tea-timer"]
                assert p["source"] == "inline" and p["library"] is True, p
                assert p["body"].strip() == "Green: 2 minutes at 80 C.", p["body"]
                assert c.svc.skills_dir not in (doc["doc"].get("skill_dirs") or [])
                assert not any(c.data in d for d in doc["doc"].get("skill_dirs") or [])
                st, lib = _req(px.port, "GET", "/chat-engine/v1/library", headers=C)
                names = [s.get("name") for s in (lib.get("skills") or lib.get("packages") or [])]
                assert st == 200 and "tea-timer" in names, lib
                st, one = _req(px.port, "GET", "/chat-engine/v1/library/tea-timer", headers=C)
                assert st == 200 and "2 minutes at 80 C" in one["body"], one
                assert c.call("DELETE", "/packages/tea-timer")[0] == 204
                st, doc = _req(px.port, "GET", "/v1/conductor/packages")
                assert "tea-timer" not in doc["doc"]["packages"]
            finally:
                c.close()
    finally:
        fk[0].shutdown()
    return "inline body in the controller's conductor, listed by its library, taken out again"


# ---------------------------------------------------------------------------- 3 sandbox defaults

def _sandbox_numbers(env: dict) -> list[str]:
    code = ("import chatd.sandbox as m; s = m.Sandboxes('/nonexistent-data', engine=object()); "
            "print(m.HOST_KIND, m.LIMIT_BYTES // m.GiB, m.FLOOR_GB, m.STOP_GB, m.KILL_GB, "
            "s.max_running, s.total_gb, s.release is None)")
    base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": ROOT,
            "HOME": tempfile.gettempdir()}
    r = subprocess.run([sys.executable, "-c", code], env={**base, **env}, capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.split()


def test_sandbox_defaults_by_host_kind():
    from chatd import sandbox as sb
    assert _sandbox_numbers({"QSE_CHAT_HOST": "app"}) == \
        ["app", "2", "1.5", "1.0", "0.5", "2", "4.0", "True"]
    assert _sandbox_numbers({}) == ["engine", "4", "4.0", "3.0", "2.0", "4", "16.0", "True"]
    assert _sandbox_numbers({"QSE_CHAT_HOST": "engine"})[:6] == ["engine", "4", "4.0", "3.0",
                                                                "2.0", "4"]
    got = _sandbox_numbers({"QSE_CHAT_HOST": "app", "QSE_SANDBOX_MEM_GB": "3",
                            "QSE_SANDBOX_MAX_RUNNING": "1", "QSE_SANDBOX_FLOOR_GB": "2"})
    assert got[:6] == ["app", "3", "2.0", "1.0", "0.5", "1"], got
    assert sb.host_kind({"QSE_CHAT_HOST": "APP"}) == "app" and sb.host_kind({}) == "engine"
    assert sb.host_kind({"QSE_CHAT_HOST": "other"}) == "engine"
    lim = sb.limits({"QSE_CHAT_HOST": "app"})
    assert (lim["mem_gb"], lim["max_running"], lim["floor_gb"]) == (2.0, 2, 1.5)
    return "app 2 GiB/2/1.5, engine 4/4/4, variables win"


# ---------------------------------------------------------------------------- 4 the guard

def test_the_guard_never_asks_a_remote_engine():
    import test_chatd_agent as tca
    from chatd import sandbox as sb_mod
    for host, want in (("app", []), ("engine", "asked")):
        mem, asked = {"v": 3.0}, []

        def release(n, asked=asked):
            asked.append(n)
            return 0
        clock = {"t": 1000.0}
        sb = sb_mod.Sandboxes(tempfile.mkdtemp(), engine=tca.FakeEngineAPI(), meminfo_fn=lambda: {
            "MemAvailable": int(mem["v"] * sb_mod.GiB)}, clock=lambda: clock["t"],
            release=release, host=host)
        sb._proxy = lambda b: None
        sb.run = lambda *a, **k: (0, False)
        sb.wait_s, sb.poll_s, sb.release_every_s = 0.2, 0.05, 0.0
        assert (sb.release is None) == (host == "app")
        why = sb.admit()
        assert why and ("this host" if host == "app" else "the engine") in why, why
        mem["v"] = 60
        sb.ensure("c1")
        mem["v"] = sb_mod.STOP_GB - 0.1
        assert sb.guard_once() == "stop c1", host
        assert sb.box("c1").stopped_reason == "memory_guard"
        assert (asked == []) if want == [] else asked, (host, asked)
    return "a host without the engine stops its own sandbox and asks nobody; beside it, it asks"


# ---------------------------------------------------------------------------- 5 the controller's chatd

def test_appchat_host_kind_and_engine_key():
    state = tempfile.mkdtemp(prefix="c2-appchat-")
    logs: list[str] = []
    chat = appchat.AppChat(state, {}, env={"PATH": "/usr/bin", "HOME": state}, log=logs.append)
    chat.gateway = "http://127.0.0.1:9123"
    assert chat.engine_url() == "http://127.0.0.1:9123/chat-engine"
    assert chat.host_kind() == "app"                         # no chat engine known: no engine here
    chat.engine_here = lambda: True
    assert chat.host_kind() == "engine"
    chat.engine_here = lambda: False
    env = chat.child_env()
    assert env["QSE_CHAT_HOST"] == "app" and env["QSE_CHAT_ENGINE"].endswith("/chat-engine")
    assert "QSE_CHAT_ENGINE_KEY" not in env
    chat.cfg["QSE_CHAT_HOST"] = "engine"
    assert chat.child_env()["QSE_CHAT_HOST"] == "engine", "the env file's word first"
    del chat.cfg["QSE_CHAT_HOST"]
    # its own key once the controller has users
    cfg = config_mod.ConfigDB(os.path.join(state, "config.sqlite3"), env={},
                              use_keychain=False).open()
    chat.users = cfg
    assert chat.engine_key() is None and not os.path.exists(os.path.join(chat.data, "engine.key"))
    cfg.create_user({"label": "laptop"})
    k1 = chat.child_env()["QSE_CHAT_ENGINE_KEY"]
    path = os.path.join(chat.data, "engine.key")
    assert os.stat(path).st_mode & 0o777 == 0o600 and cfg.user_for_key(k1) == "chatd"
    assert chat.engine_key() == k1, "kept"
    uid = next(u["id"] for u in cfg.users() if u["label"] == "chatd")
    cfg.update_user(uid, {"enabled": False})
    k2 = chat.engine_key()
    assert k2 != k1 and cfg.user_for_key(k2) == "chatd" and cfg.user_for_key(k1) is None
    assert [u["label"] for u in cfg.users()].count("chatd") == 1
    chat.cfg["QSE_CHAT_ENGINE_KEY"] = "env-file-key-0123456789"
    assert chat.engine_key() is None
    assert chat.child_env()["QSE_CHAT_ENGINE_KEY"] == "env-file-key-0123456789"
    assert chat.view()["host"] == "app"
    assert not any(k1 in ln or k2 in ln for ln in logs), logs
    cfg.close()
    shutil.rmtree(state, ignore_errors=True)
    return "engine URL, host kind, its own key made, kept, made again, env file first"


# ---------------------------------------------------------------------------- 6 the move

def _home(label: str) -> str:
    return tempfile.mkdtemp(prefix=f"c2-{label}-")


def _old_chatd(home: str, fk) -> tuple[str, str, str]:
    """A chatd in `home` (an engine host's layout): a finished chat, a chat whose run it left open
    at its stop, and an MCP server with a sealed secret. (data, done cid, open cid)."""
    data = os.path.join(home, ".qwen38-spark-engine", "chat")
    os.makedirs(data)
    c = Chat(data=data, fake=fk)
    try:
        done = c.new()
        c.send(done, "FAKE_SAY the first chat")
        c.wait_idle(done)
        c.ok("POST", "/mcp/servers", {"name": "local tool", "transport": "stdio",
                                      "command": "python3", "args": ["tool.py"],
                                      "env": {"TOOL_TOKEN": MCP_SECRET},
                                      "enabled_global": False})
        opened = c.new()
        c.send(opened, "FAKE_SLOW FAKE_LONG 80 long")
        c.wait(lambda: len(text_of(c.snap(opened)["messages"][-1]).split()) >= 4, 30)
    finally:
        c.close(keep_data=True)                       # SIGTERM: the open run ends interrupted
    return data, done, opened


def _new_state(home: str, fk_port: int) -> tuple[str, int]:
    state = os.path.join(home, ".tandemllm")
    os.makedirs(state, mode=0o700)
    port = free_port()
    with open(os.path.join(state, "chatd.env"), "w") as f:
        f.write(f"QSE_CHAT_URL=http://127.0.0.1:{port}\n"
                f"QSE_CHAT_ENGINE=http://127.0.0.1:{fk_port}\nQSE_CHAT_HOST=app\n"
                f"QSE_SANDBOX_SOCKET={os.path.join(home, 'no-sandbox.sock')}\n")
    return state, port


def _chat_api(port: int, data: str, method: str, path: str, body=None):
    with open(os.path.join(data, "forward.key")) as f:
        key = f.read().strip()
    return _req(port, method, "/v1/chat-app" + path, body, headers={"X-Tandem-Forward": key})


def _stop_here(state: str) -> None:
    try:
        appchat.AppChat.load(state, env={"HOME": os.path.dirname(state)}).stop()
    except Exception:                                              # noqa: BLE001
        pass


def test_chat_move_between_two_homes():
    from proxy import secrets as vault_mod
    fk = fake_backend.start(models=["fake-a"])
    a, b = _home("old"), _home("new")
    state = None
    try:
        src, done, opened = _old_chatd(a, fk)
        before = chatmove.Source(src).look()
        assert not before["running"] and before["mcp"] == [{"name": "local tool",
                                                            "transport": "stdio", "sealed": True}]
        state, port = _new_state(b, fk[1])
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": b, "PYTHONPATH": ROOT,
               "LANG": "C.UTF-8", "TMPDIR": tempfile.gettempdir()}
        r = subprocess.run([sys.executable, "-m", "proxy", "chat-move", "--from", src,
                            "--state-dir", state], env=env, capture_output=True, text=True,
                           timeout=180, cwd=ROOT)
        assert r.returncode == 0, (r.stdout[-2000:], r.stderr[-2000:])
        out = json.loads(r.stdout[r.stdout.index("{"):])
        dest = os.path.join(state, "chat")
        assert out["to"] == dest and out["started"] is True, out
        assert out["counts"]["conversations"] == 2 and out["counts"]["open_runs"] == 0, out
        assert out["mcp_secrets"] == {"servers": 1, "sealed_again": 1, "cleared": 0}, out
        assert "chatd.pid" not in os.listdir(dest) or appchat.is_chatd(
            int(open(os.path.join(dest, "chatd.pid")).read()))
        # the old folder is as it was
        assert chatmove.compare(before, chatmove.Source(src).look()) == []
        # the MCP secret opens with the new host's key, not only the old one's
        new_vault = vault_mod.Vault.load(state, env={}, use_keychain=False)
        old_vault = vault_mod.Vault.load(os.path.dirname(src), env={}, use_keychain=False)
        assert new_vault.key_id != old_vault.key_id
        con = sqlite3.connect(os.path.join(dest, "chat.sqlite3"))
        sid, blob = con.execute("SELECT id, env_enc FROM mcp_servers").fetchone()
        con.close()
        d = json.loads(blob)
        got = new_vault.decrypt(sid, base64.b64decode(d["ct"]), base64.b64decode(d["nonce"]),
                                d["kid"])
        assert json.loads(got) == {"TOOL_TOKEN": MCP_SECRET}
        # the chatd here serves the moved chats; the open run came back interrupted
        st, lst = _until(lambda: (lambda x: x if x[0] == 200 else None)(
            _chat_api(port, dest, "GET", "/conversations")), 30) or (0, None)
        assert st == 200, lst
        ids = {cv["id"] for cv in (lst.get("conversations") if isinstance(lst, dict) else lst)}
        assert {done, opened} <= ids, ids
        st, s = _chat_api(port, dest, "GET", f"/conversations/{opened}")
        assert st == 200 and s["messages"][-1]["status"] == "interrupted", s["messages"][-1]
        partial = text_of(s["messages"][-1])
        assert _chat_api(port, dest, "POST", f"/conversations/{opened}/continue")[0] in (200, 202)
        s = _until(lambda: (lambda x: x[1] if x[1]["run"] is None and x[1]["messages"][-1][
            "status"] == "done" else None)(_chat_api(port, dest, "GET",
                                                     f"/conversations/{opened}")), 60)
        assert s and text_of(s["messages"][-1]).startswith(partial), s
        # nothing of the secret in what the move printed or chatd's log
        assert MCP_SECRET not in r.stdout + r.stderr
        log = os.path.join(state, "logs", "chatd.log")
        assert MCP_SECRET not in open(log, errors="replace").read()
    finally:
        if state:
            _stop_here(state)
        fk[0].shutdown()
        shutil.rmtree(a, ignore_errors=True)
        shutil.rmtree(b, ignore_errors=True)
    return "2 chats moved, files checked, MCP secret sealed again, old folder untouched, the " \
           "open run interrupted and continued here"


def test_chat_move_refusals():
    fk = fake_backend.start(models=["fake-a"])
    a, b = _home("old2"), _home("new2")
    runner = None
    try:
        src, _done, _opened = _old_chatd(a, fk)
        state, _port = _new_state(b, fk[1])
        chat = appchat.AppChat.load(state, env={"HOME": b})
        lines: list[str] = []
        kw = dict(start=False, log=lines.append)
        # --plan: what would happen, nothing changed
        plan = chatmove.move(chatmove.Source(src), chat, plan=True, **kw)
        assert plan["plan"] and plan["problems"] == [] and plan["mcp_sealed"] == 1, plan
        assert not os.path.exists(chat.data)
        # a chatd that runs on the source
        runner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "-m",
                                   "chatd"])
        with open(os.path.join(src, "chatd.pid"), "w") as f:
            f.write(f"{runner.pid}\n")
        _until(lambda: appchat.is_chatd(runner.pid), 10)
        try:
            chatmove.move(chatmove.Source(src), chat, **kw)
            raise AssertionError("a running source must be refused")
        except chatmove.MoveError as exc:
            assert "stop it first" in exc.message, exc.message
        runner.kill()
        runner.wait()
        os.unlink(os.path.join(src, "chatd.pid"))
        # sealed MCP secrets without the old key: refused; --without-mcp-secrets clears them
        key = os.path.join(os.path.dirname(src), "secrets.key")
        os.rename(key, key + ".away")
        try:
            chatmove.move(chatmove.Source(src), chat, **kw)
            raise AssertionError("no old key must be refused")
        except chatmove.MoveError as exc:
            assert "--from-secrets" in exc.message, exc.message
        out = chatmove.move(chatmove.Source(src), chat, without_mcp_secrets=True, **kw)
        assert out["mcp_secrets"] == {"servers": 1, "sealed_again": 0, "cleared": 1}, out
        os.rename(key + ".away", key)
        # a destination with chats: refused; --replace keeps it aside
        try:
            chatmove.move(chatmove.Source(src), chat, from_secrets=key, **kw)
            raise AssertionError("a destination with chats must be refused")
        except chatmove.MoveError as exc:
            assert "--replace" in exc.message, exc.message
        out = chatmove.move(chatmove.Source(src), chat, replace=True, from_secrets=key, **kw)
        assert os.path.isfile(os.path.join(out["kept"], "chat.sqlite3")), out
        assert out["mcp_secrets"]["sealed_again"] == 1
        assert not [p for p in os.listdir(b + "/.tandemllm") if ".moving-" in p]
        # not a data folder; a bad source
        empty = tempfile.mkdtemp(dir=a)
        for spec, words in ((empty, "not a chatd data folder"), (src + "-gone", "no such folder")):
            try:
                chatmove.move(chatmove.Source(spec), chat, **kw)
                raise AssertionError(words)
            except chatmove.MoveError as exc:
                assert words in exc.message, exc.message
        for bad in ("relative/path", "-oProxyCommand=x:/p", "host:relative", "a b@h:/p"):
            try:
                chatmove.Source(bad)
                raise AssertionError(bad)
            except chatmove.MoveError as exc:
                assert exc.code == 2, bad
        assert MCP_SECRET not in "\n".join(lines)
    finally:
        if runner is not None and runner.poll() is None:
            runner.kill()
        fk[0].shutdown()
        shutil.rmtree(a, ignore_errors=True)
        shutil.rmtree(b, ignore_errors=True)
    return "plan, running source, no old key, without secrets, replace, not a folder, bad specs"


def test_chat_move_over_ssh():
    """The source over SSH: each step is one quoted command line a stand-in ssh hands to a shell, as
    sshd runs a command (no real host)."""
    fk = fake_backend.start(models=["fake-a"])
    a, b = _home("old3"), _home("new3")
    try:
        src, _done, _opened = _old_chatd(a, fk)
        state, _port = _new_state(b, fk[1])
        bindir = os.path.join(b, "bin")
        os.makedirs(bindir)
        stub = os.path.join(bindir, "ssh")
        seen = os.path.join(b, "ssh.log")
        with open(stub, "w") as f:
            f.write("#!/bin/sh\n"
                    f"printf '%s\\n' \"$*\" >> {seen}\n"
                    "while [ \"$#\" -gt 0 ]; do\n"
                    "  if [ \"$1\" = \"--\" ]; then shift; break; fi\n"
                    "  shift\n"
                    "done\n"
                    "shift\n"                                     # the host
                    "[ \"$#\" -eq 1 ] || { echo 'one command expected' >&2; exit 64; }\n"
                    "exec /bin/sh -c \"$1\"\n")
        os.chmod(stub, 0o755)
        chat = appchat.AppChat.load(state, env={"HOME": b, "PATH": os.environ.get("PATH", "")})
        source = chatmove.Source(f"tandem@engine-host.example:{src}", port=2222, ssh=stub)
        assert str(source) == f"tandem@engine-host.example:{src}"
        # a chatd runs here; a move whose copy fails starts it again on its own folder
        assert chat.start()[0] == 0 and chat.status()["running"]
        broken = chatmove.Source(f"tandem@engine-host.example:{src}", port=2222,
                                 ssh=os.path.join(bindir, "ssh-no-tar"))
        with open(broken.ssh_bin, "w") as f:
            f.write(open(stub).read().replace('exec /bin/sh -c "$1"',
                                              'case "$1" in tar*) exit 2;; esac\n'
                                              'exec /bin/sh -c "$1"'))
        os.chmod(broken.ssh_bin, 0o755)
        try:
            chatmove.move(broken, chat, replace=True, start=False, log=lambda m: None)
            raise AssertionError("a failed copy must be refused")
        except chatmove.MoveError as exc:
            assert "tar on the source exited 2" in exc.message, exc.message
        assert chat.status()["running"], "the chatd here runs again"
        assert not [p for p in os.listdir(state) if ".moving-" in p]
        out = chatmove.move(source, chat, replace=True, start=False, log=lambda m: None)
        assert not chat.status()["running"] and os.path.isdir(out["kept"]), out
        assert ".before-move-" in out["kept"]
        assert out["counts"]["conversations"] == 2 and out["mcp_secrets"]["sealed_again"] == 1
        runs = open(seen).read().splitlines()
        assert len(runs) >= 4 and all("-o BatchMode=yes" in r and "-p 2222" in r and
                                      " -- tandem@engine-host.example " in r for r in runs), runs
        assert any(" tar -C " in r for r in runs) and any(" cat -- " in r for r in runs)
        assert MCP_SECRET not in "\n".join(runs)
    finally:
        _stop_here(os.path.join(b, ".tandemllm"))
        fk[0].shutdown()
        shutil.rmtree(a, ignore_errors=True)
        shutil.rmtree(b, ignore_errors=True)
    return "look, key and tar over a stand-in ssh, each one quoted command"


if __name__ == "__main__":
    from scriptmode import run
    run(globals())
