"""The onboarding tests' machine (SRV-827..833): a controller (tests/proxy_util.py) with a fleet, an
engine host made of fakes, and the means to look for a secret everywhere.

    with rig(tmp_path, monkeypatch) as r:
        st, doc = r.post("/v1/fleet/engines/pair/start", {"address": "127.0.0.1", "port": r.engine.port})

THE ENGINE HOST is three fakes that share one home: tests/fake_engine.py (the engine's HTTP port:
pairing window, Setup Guide), tests/fake_ssh.py (sshd and OpenSSH: the pinned host key, the key's
forced command, a password login through SSH_ASKPASS) and tests/fake_tandem_remote.py (the forced
command, `tandem remote`). Pairing writes the key's line into the home's ~/.ssh/authorized_keys and
fake ssh logs in with it, so a paired host answers `status` over SSH as a real one does.

`seen()` is every text a secret could leak into: the controller's log lines, every answer and
error this rig read, the audit and event rows, the fake ssh's argv log, the fake engine's request
log and the call records of `tandem remote`; `files()` every byte of the controller's files.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3

import fake_engine
import fake_ssh
import fake_tandem_remote as ftr
import test_fleet as tf
from proxy import hostkeys
from proxy_util import Proxy
from tools import contract_check

CONTRACT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "contracts",
                        "fleet-v1")
PASSWORD = "pw-FIXTURE-correct-horse-7319"


def ok(doc, name: str):
    errs = contract_check.validate(doc, contract_check.load(name, CONTRACT), root=CONTRACT)
    assert not errs, (name, errs[:5], doc)
    return doc


class Rig:
    def __init__(self, px, fleet, tmp: str, monkeypatch, **engine_kw):
        self.px, self.fleet, self.tmp = px, fleet, tmp
        self.cfg = fleet.cfg
        self.engine_dir = ftr.install(os.path.join(tmp, "engine"))
        self.bin = fake_ssh.install(os.path.join(tmp, "bin"))
        monkeypatch.setenv("PATH", self.bin + os.pathsep + os.environ["PATH"])
        _pem, pub = hostkeys.generate()
        self.host_type, self.host_b64 = pub.split()[:2]
        self.host_key = f"{self.host_type} {self.host_b64}"
        self.host_fp = fake_engine.fp_of(self.host_b64)
        self.home = os.path.join(tmp, "home")
        os.makedirs(self.home, mode=0o755)
        self.ak = os.path.join(self.home, ".ssh", "authorized_keys")
        self.server = fake_ssh.server(os.path.join(tmp, "server.json"),
                                      host_key=[self.host_type, self.host_b64],
                                      authorized_keys=self.ak, home=self.home, password=PASSWORD)
        self.sshlog = os.path.join(tmp, "ssh.log")
        monkeypatch.setenv("FAKE_SSH_SERVER", self.server)
        monkeypatch.setenv("FAKE_SSH_LOG", self.sshlog)
        engine_kw.setdefault("ssh_user", "tandem")
        self.engine = fake_engine.FakeEngine(self.host_key, engine_dir=self.engine_dir,
                                             authorized_keys=self.ak, **engine_kw).start()
        self.texts: list[str] = []

    def close(self) -> None:
        self.engine.stop()

    # ------------------------------------------------------------------ the API
    def request(self, method: str, path: str, body=None) -> tuple[int, dict]:
        st, _, out = self.px.request(method, path, body)
        self.texts.append(out.decode("utf-8", "replace"))
        doc = json.loads(out)
        if st != 200:
            ok(doc, "engine-error.schema.json" if path.startswith("/v1/fleet/engines") else
               "error.schema.json")
        return st, doc

    def post(self, path: str, body) -> tuple[int, dict]:
        self.texts.append(json.dumps({k: v for k, v in body.items()
                                      if k not in ("code", "password")}))
        return self.request("POST", path, body)

    def get(self, path: str) -> tuple[int, dict]:
        return self.request("GET", path)

    def start(self, **kw) -> dict:
        st, doc = self.post("/v1/fleet/engines/pair/start",
                            dict({"address": "127.0.0.1", "port": self.engine.port}, **kw))
        assert st == 200, doc
        return ok(doc, "engine-pair-start.schema.json")

    def pair(self, code: str | None = None) -> dict:
        code = code or self.engine.open()
        p = self.start()
        st, doc = self.post("/v1/fleet/engines/pair/code", {"pairing": p["pairing"], "code": code})
        assert st == 200, doc
        return ok(doc, "engine-paired.schema.json")

    # ------------------------------------------------------------------ what the host holds
    def lines(self) -> list[str]:
        try:
            with open(self.ak) as f:
                return [x for x in f.read().splitlines() if x.strip()]
        except FileNotFoundError:
            return []

    def calls(self) -> list[dict]:
        return ftr.calls(self.engine_dir)

    def ssh_nodes(self) -> list[dict]:
        return [n for n in self.cfg.nodes() if n["transport"] == "ssh"]

    # ------------------------------------------------------------------ where a secret could be
    def seen(self) -> str:
        out = list(self.texts) + list(self.px.logs)
        for r in self.cfg.audit(1000):
            out.append(json.dumps(r, default=str))
        try:
            out += [json.dumps(e, default=str) for e in self.fleet.store.events(limit=1000)]
        except Exception:                                            # noqa: BLE001
            pass
        out += [json.dumps(x) for x in fake_ssh.log(self.sshlog)]
        out += [json.dumps(c) for c in self.calls()]
        out += [repr(c) for c in self.engine.calls]
        out += [json.dumps(n, default=str) for n in self.cfg.nodes()]
        return "\n".join(out)

    def files(self) -> bytes:
        raw = b""
        for d, _, fs in os.walk(self.px.dir):
            for name in fs:
                with open(os.path.join(d, name), "rb") as fh:
                    raw += fh.read()
        return raw

    def db_rows(self) -> str:
        """Every row of every table of the config database, as text (sealed columns as hex)."""
        con = sqlite3.connect(self.cfg.path)
        out = []
        for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
            for row in con.execute(f"SELECT * FROM {t}"):
                out.append(repr(row))
        con.close()
        return "\n".join(out)


def pem_lines(pem: str) -> list[str]:
    return [x for x in pem.splitlines() if x and "PRIVATE KEY" not in x]


@contextlib.contextmanager
def rig(tmp, monkeypatch, **engine_kw):
    with Proxy() as px:
        f = tf.fleet_on(px, tf.local_node(tf.fake_root()))
        r = Rig(px, f, str(tmp), monkeypatch, **engine_kw)
        try:
            yield r
        finally:
            r.close()
