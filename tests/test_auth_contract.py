"""contracts/auth-v1 and docs/auth.md are what the controller does (SRV-883, D1).

  Gherkin "Docs":
  1  every example validates against its schema; the README names every file
                                                                   test_examples_validate
                                                                   test_readme_names_every_file
  2  the running server's answers validate: the session with and without a sign-in, the sign-in,
     a token made, read and listed, and the refusals of the gate in its three shapes (a session's,
     an OpenAI route's, a Messages API route's), the wrong-token limit among them
                                                                   test_answers_validate
  3  nothing points to the old way: the old sign-in's variables are named only in
     docs/auth.md's migration section (docs, README, contracts)
                                                                   test_the_old_way_only_in_the_migration
  4  docs/auth.md has the role table, the token scopes, both client setups and the recovery
     steps; the threat model's first item names sessions and tokens
                                                                   test_the_auth_doc_has_its_parts

    python tests/test_auth_contract.py
"""

from __future__ import annotations

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from proxy import authz  # noqa: E402
from proxy import tokens as tokens_mod  # noqa: E402
from proxy_util import run  # noqa: E402
from tools import contract_check  # noqa: E402

CONTRACT = os.path.join(ROOT, "contracts", "auth-v1")
AUTH_DOC = os.path.join(ROOT, "docs", "auth.md")
OLD = re.compile(r"TANDEM_ADMIN_TOKEN|TANDEM_API_KEYS|TANDEM_API_USERS_FROM|TANDEM_DASHBOARD_LOGIN")
MIGRATION = "## Moving from the old sign-in"


def _load(name: str):
    with open(os.path.join(CONTRACT, name), encoding="utf-8") as fh:
        return json.load(fh)


def _check(value, schema: str) -> None:
    errs = contract_check.validate(value, _load(f"{schema}.schema.json"), root=CONTRACT)
    assert not errs, (schema, errs, value)


# ---------------------------------------------------------------------------- 1
def test_examples_validate():
    names = sorted(os.listdir(os.path.join(CONTRACT, "examples")))
    schemas = {f[:-12] for f in os.listdir(CONTRACT) if f.endswith(".schema.json")}
    for f in names:
        n = f[:-5]
        _check(_load(os.path.join("examples", f)), "session" if n == "session-anonymous" else n)
    assert schemas - {"common"} <= {f[:-5] for f in names}, "a schema without an example"
    # the token example has the token's own format
    assert tokens_mod.TOKEN_RE.match(_load("examples/token-created.json")["token"])
    return f"{len(names)} examples"


def test_readme_names_every_file():
    with open(os.path.join(CONTRACT, "README.md"), encoding="utf-8") as fh:
        text = fh.read()
    for f in os.listdir(CONTRACT):
        if f != "README.md":
            assert f"`{f}" in text, f"the README does not name {f}"
    return "every file named"


# ---------------------------------------------------------------------------- 2
def test_answers_validate():
    import test_auth_tokens as T
    srv, port = T.engine()
    try:
        with T.Proxy(env={**T.ON, **T.env_for(port)}) as px:
            px.sync()
            _check(json.loads(px.get("/v1/auth/session")[2]), "session")
            T.make("ops", "operator")
            st, h, out = px.post("/v1/auth/session", {"username": "ops",
                                                     "password": T.PW["ops"]})
            assert st == 200, out
            _check(json.loads(out), "signed-in")
            ops = T.sign_in(px, "ops")
            _check(json.loads(px.get("/v1/auth/session", ops)[2]), "session")
            body = {"label": "laptop", "scopes": ["inference:openai"], "models": ["small-*"],
                    "days": 90}
            _check(body, "token-new")
            st, h, out = px.post("/v1/auth/tokens", body, ops)
            made = json.loads(out)
            assert st == 201 and h.get("Cache-Control") == "no-store", (st, h)
            _check(made, "token-created")
            _check(json.loads(px.get(f"/v1/auth/tokens/{made['id']}", ops)[2]), "token")
            _check(json.loads(px.get("/v1/auth/tokens", ops)[2]), "tokens")
            # the refusals
            st, _, out = px.post("/v1/auth/session", {"username": "ops", "password": "x" * 12})
            assert st == 401
            _check(json.loads(out), "error")
            st, _, out = px.get("/v1/settings")
            assert st == 401
            _check(json.loads(out), "error")
            wrong = "tdm-00000000-" + "x" * 43
            st, _, out = px.post("/v1/chat/completions", {"model": "small-1", **T.CHAT},
                                 T.bearer(wrong))
            assert st == 401
            _check(json.loads(out), "openai-error")
            st, h, out = px.post("/v1/messages", {"model": "small-1"}, {"x-api-key": wrong})
            assert st == 401 and h.get("request-id") == json.loads(out)["request_id"], h
            _check(json.loads(out), "messages-error")
            for _ in range(20):
                px.post("/v1/chat/completions", {"model": "small-1", **T.CHAT}, T.bearer(wrong))
            st, h, out = px.post("/v1/chat/completions", {"model": "small-1", **T.CHAT},
                                 T.bearer(wrong))
            assert st == 429 and h.get("Retry-After"), (st, h)
            _check(json.loads(out), "openai-error")
            st, _, out = px.post("/v1/messages", {"model": "small-1"}, {"x-api-key": wrong})
            assert st == 429
            _check(json.loads(out), "messages-error")
    finally:
        srv.shutdown()
    return "every answer validates"


# ---------------------------------------------------------------------------- 3
def _doc_files():
    out = [os.path.join(ROOT, "README.md")]
    for top, ext in (("docs", ".md"), ("contracts", ".md")):
        for d, _, files in os.walk(os.path.join(ROOT, top)):
            out += [os.path.join(d, f) for f in files if f.endswith(ext)]
    return [p for p in out if os.path.isfile(p)]


def test_the_old_way_only_in_the_migration():
    with open(AUTH_DOC, encoding="utf-8") as fh:
        auth = fh.read()
    at = auth.find(MIGRATION)
    assert at > 0, f"docs/auth.md has no {MIGRATION!r}"
    end = auth.find("\n## ", at + 5)
    hits = []
    for p in _doc_files():
        with open(p, encoding="utf-8") as fh:
            text = fh.read()
        for m in OLD.finditer(text):
            if p == AUTH_DOC and at <= m.start() and (end < 0 or m.start() < end):
                continue
            line = text.count("\n", 0, m.start()) + 1
            hits.append(f"{os.path.relpath(p, ROOT)}:{line} {m.group(0)}")
    assert not hits, "the old sign-in outside docs/auth.md's migration section: " + \
        ", ".join(hits)
    assert OLD.search(auth[at:end]), "the migration section names the old variables"
    return f"{len(_doc_files())} files"


# ---------------------------------------------------------------------------- 4
def test_the_auth_doc_has_its_parts():
    with open(AUTH_DOC, encoding="utf-8") as fh:
        doc = fh.read()
    for heading in ("## Roles", "## The first owner", "## Sessions", "## API tokens",
                    "## Who may use the API from other devices", "## Clients", "## Recovery",
                    "## Routes", MIGRATION):
        assert heading in doc, heading
    for role in authz.ROLES:
        assert f"| {role} |" in doc, role
    for scope in tokens_mod.SCOPES:
        if scope in ("collect:write", "chat-engine", "plugin-backend"):
            continue                                 # service identities' own
        assert f"`{scope}`" in doc, scope
    for client in ("OPENAI_BASE_URL", "ANTHROPIC_BASE_URL", "/v1/messages", "x-api-key"):
        assert client in doc, client
    for step in ("users reset-password", "users setup-code", "tokens revoke"):
        assert step in doc, step
    with open(os.path.join(ROOT, "docs", "threat-model.md"), encoding="utf-8") as fh:
        tm = fh.read()
    first = tm[tm.index("\n1. "):].split("\n2. ", 1)[0]
    assert "Sessions" in first and "token" in first, first[:200]
    return "every part"


if __name__ == "__main__":
    run(globals())
