"""Helpers of the hooks tests (SRV-849, SEC-16): a runner over a config database in a temp dir, the
contract's fake hook server, a fake clock.

    with Store() as (cfg, runner):          a ConfigDB and a Runner, no proxy
    srv, port = hook_server(secrets=[...])  contracts/hooks-v1/fake_server.py
    clock = Clock(); runner.clock = clock   the breakers' time (Breaker reads runner.clock)
"""

from __future__ import annotations

import base64
import importlib.util
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
CONTRACT = os.path.join(ROOT, "contracts", "hooks-v1")
MASTER = base64.b64encode(b"k" * 32).decode()

from proxy import config as config_mod  # noqa: E402
from proxy import hooks as hooks_mod  # noqa: E402

_spec = importlib.util.spec_from_file_location("hooks_fake_server",
                                               os.path.join(CONTRACT, "fake_server.py"))
fake_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fake_server)


def hook_server(secrets=(), **kw):
    return fake_server.start(secrets=secrets, **kw)


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class Store:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="hooks-test-")

    def __enter__(self):
        self.cfg = config_mod.ConfigDB(os.path.join(self.dir, "config.sqlite3"),
                                       env={"TANDEM_SECRETS_KEY": MASTER},
                                       use_keychain=False).open()
        self.logs: list[str] = []
        self.runner = hooks_mod.Runner(self.cfg, log=self.logs.append)
        return self.cfg, self.runner

    def __exit__(self, *exc):
        self.runner.bg.drain(5)
        self.runner.stop()
        self.cfg.close()
        shutil.rmtree(self.dir, ignore_errors=True)


def builtin(runner, name, fn):
    """Register a test builtin; returns the list of envelopes it saw."""
    seen: list = []

    def wrapped(env, options):
        seen.append(env)
        return fn(env, options)
    runner.builtin(name, wrapped)
    return seen


def make(runner, **doc):
    doc.setdefault("kind", "builtin")
    doc.setdefault("name", doc.get("id") or "h")
    h, _ = runner.store.create(doc, "test")
    return h


def sleeper(seconds: float, result=None):
    def fn(env, options):
        time.sleep(seconds)
        return result or {"action": "continue"}
    return fn


def ctx(**kw) -> dict:
    out = {"request_id": "r-1", "endpoint": "chat", "model": "m-a", "upstream_model": "m-a",
           "backend": "b1", "profile": None, "user": "owner", "session": "s-1",
           "client_kind": "unknown", "stream": False}
    out.update(kw)
    return out
