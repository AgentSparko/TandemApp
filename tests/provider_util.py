"""Helpers of the decision providers' tests (SRV-851, QA SRV-852): a config database with the
hooks' runner and the providers, a controller-style conductor wired as the harness wires it,
the example systemone server, fixture prompts.

    with Rig() as rig:                       rig.cfg, rig.runner, rig.ps (Providers), rig.dir
        cond = rig.conductor(decider=url)    a Conductor whose decider role is `url` (None: the
                                             controller's default, `self`, which answers nothing
                                             here), the Qwen stage off, providers attached
        rig.add(**doc)                       a provider through the store
    srv, port = example(key=..., model=...)  tools/systemone_example.py on a thread
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from proxy import config as config_mod  # noqa: E402
from proxy import hooks as hooks_mod  # noqa: E402
from proxy import providers as providers_mod  # noqa: E402
from tools import systemone_example  # noqa: E402

MASTER = base64.b64encode(b"p" * 32).decode()
KEYS = ("dpk-fixture-DEFAULT-0001", "dpk-fixture-CLASSIFY-0002", "dpk-fixture-ROTATED-0003")
SCHEMA_DIR = os.path.join(ROOT, "contracts", "engine-api-v1", "decision")

#: made-up prompts across the classes; the example decider scores the words they share
PROMPTS = [
    "Rename the variable cnt to count in utils.py and run the tests.",
    "Why does this test fail with a KeyError when the config is empty? Fix the bug.",
    "hello, how are you today?",
    "Find sources on battery recycling and summarize what they say.",
    "Draw a small logo for the project, a cat on a rocket.",
    "Plan the next release: list the steps and who does what.",
    "Change the button colour on the settings page and fix the layout on phones.",
    "Debug the crash in the parser: a traceback points at line 42.",
]


def example(key=None, model="example-decider", answer=None):
    srv, port = systemone_example.start(key=key, model=model)
    if answer is not None:
        srv.answer = answer
    return srv, port


def last_option(state, question):
    """A provider that disagrees with the example's scorer: the last option, sure."""
    opts = list(question.get("criteria") or {})
    return opts[-1], {o: (0.9 if o == opts[-1] else round(0.1 / max(1, len(opts) - 1), 4))
                      for o in opts}


class Rig:
    def __init__(self, journal: bool = False):
        self.dir = tempfile.mkdtemp(prefix="providers-test-")
        self.want_journal = journal
        self.conductors = []

    def __enter__(self) -> "Rig":
        self.cfg = config_mod.ConfigDB(os.path.join(self.dir, "config.sqlite3"),
                                       env={"TANDEM_SECRETS_KEY": MASTER},
                                       use_keychain=False).open()
        self.logs: list[str] = []
        self.runner = hooks_mod.Runner(self.cfg, log=self.logs.append)
        self.ps = providers_mod.Providers(self.cfg, runner=self.runner, log=self.logs.append)
        return self

    def __exit__(self, *exc):
        self.ps.bg.drain(10)
        self.runner.bg.drain(5)
        for c in self.conductors:
            try:
                c.journal.close()
            except Exception:                                      # noqa: BLE001
                pass
        self.ps.stop()
        self.runner.stop()
        self.cfg.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def add(self, **doc) -> dict:
        doc.setdefault("name", doc.get("id") or "p")
        return self.ps.store.create(doc, "test")

    def conductor(self, decider: str | None = None, tester: str | None = None,
                  providers: bool = True, journal=None):
        """A controller's conductor: shipped-at-version-0 documents, the decider role at
        `decider` (http) or `self`, the tester at `tester` or `self`, the Qwen stage off; its
        router and judge ask through `self.ps` (as proxy/harness.py wires them) unless
        `providers` is False."""
        from tandemapp.conductor.decisions import Journal
        from tandemapp.conductor.hook import Conductor
        root = tempfile.mkdtemp(prefix="cond-", dir=self.dir)
        if journal is None and self.want_journal:
            journal = Journal(os.path.join(self.dir, f"journal-{len(self.conductors)}.sqlite3"))
        cond = Conductor(root, state={"model": "TandemLLM"},
                         journal=journal if journal is not None else False)
        self.conductors.append(cond)
        v, roles = cond.store.get("roles")
        roles = json.loads(json.dumps(roles))
        roles["decider"] = dict(roles["decider"], mode="on", endpoint=decider or "self")
        roles["tester"] = dict(roles["tester"], mode="on", endpoint=tester or "self")
        roles["classifier"] = dict(roles["classifier"], mode="off")
        cond.store.put("roles", None, roles, note="test", base=v, author="test")
        v, router = cond.store.get("router")
        router = json.loads(json.dumps(router))
        for st in router["classifier"]["stages"]:
            st["enabled"] = st["kind"] == "decider"
        cond.store.put("router", None, router, note="test", base=v, author="test")
        if providers:
            self.ps.attach(cond)
            cond.router.providers = self.ps
            cond.judge.providers = self.ps
        return cond


def plan(cond, text: str, sid: str = "ses-1", user: str = "owner", rid: str = "r-1"):
    return cond.plan({"model": "TandemLLM", "messages": [
        {"role": "system", "content": "You are a coding agent. Working directory: /tmp/proj"},
        {"role": "user", "content": text}]}, {"x-session-id": sid}, client_kind="opencode",
        request_id=rid, chat=True, user=user)


def decision(p) -> dict:
    """What a plan decided, without its times: the class, its source and confidence, every
    stage's answer, the effort, the decisions' modes, the notes' texts."""
    row = p.row or {}
    stages = [{k: v for k, v in s.items() if k not in ("ms", "eta_ms")}
              for s in row.get("stages") or []]
    return {"class": row.get("class_id"), "confidence": row.get("confidence"),
            "source": row.get("source"), "stages": stages, "effort": row.get("effort"),
            "decisions": row.get("decisions"),
            "injected": [n.get("text") for n in row.get("injected") or []]}


def schema_errors(answer: dict) -> list:
    from tools import contract_check
    return contract_check.validate(answer, contract_check.load("systemone.schema.json",
                                                               SCHEMA_DIR), root=SCHEMA_DIR)
