"""OPS-292: the URL prefixes the controller owns, one list (docs/controller-routes.json), so a
reverse proxy in front of TandemApp sends all of them to it. A proxy that forwarded only some
controller paths sent the settings, hooks, providers, plugins and health pages' /v1/settings and
/v1/fleet/* calls to an engine, which answers 404.

  1  every /v1/... path the page calls (a string in dashboard/src/**/*.ts) falls under one entry
     of the list: a controller family the page calls that the list lacks fails here
                                                                  test_the_page_calls_only_listed_families
  2  the list agrees with the engine's contract: each app family of engine-api-v1 routes.json is
     the controller's here, each engine route is not the controller's alone
                                                                  test_the_list_agrees_with_the_engine_contract
  3  docs/install.md's section names every prefix a proxy sends to TandemApp or blocks
                                                                  test_the_install_doc_names_every_prefix
  4  a running controller answers each controller and both prefix itself (its probe is not the
     controller's own "no route" 404; one answered only in a state the test controller is not in
     is named in its code), the conductor's ones only while its conductor is on, and none of the
     engine's
                                                                  test_the_controller_answers_what_the_list_says

    python tests/test_route_list.py
"""

from __future__ import annotations

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LIST = os.path.join(ROOT, "docs", "controller-routes.json")
INSTALL = os.path.join(ROOT, "docs", "install.md")
SRC = os.path.join(ROOT, "dashboard", "src")
OWNERS = ("controller", "both", "engine", "internal")

# a path in a string: quotes or a template, up to its end, a ${...} part, a query or a fragment
LITERAL = re.compile(r"""(['"`])(/v1/[^'"`\s]*?)(?=\1|\$\{|\?|#)""")
COMMENT = re.compile(r"^\s*(//|\*|/\*)")


def routes() -> list[dict]:
    with open(LIST) as fh:
        doc = json.load(fh)
    return doc["routes"]


def owner_of(path: str, rows: list[dict]) -> dict | None:
    """The entry a path falls under: the longest prefix that holds it."""
    best = None
    for r in rows:
        p = r["prefix"]
        hit = path == p or (r["match"] == "prefix" and path.startswith(p.rstrip("/") + "/"))
        if hit and (best is None or len(p) > len(best["prefix"])):
            best = r
    return best


def page_paths() -> dict[str, str]:
    """Every /v1/... path in a string of the page's code (comments left out): path -> a file."""
    out: dict[str, str] = {}
    for d, _, files in os.walk(SRC):
        for f in files:
            if not f.endswith(".ts"):
                continue
            p = os.path.join(d, f)
            with open(p, encoding="utf-8") as fh:
                for line in fh:
                    if COMMENT.match(line):
                        continue
                    for m in LITERAL.finditer(line):
                        path = m.group(2).rstrip("/") or "/"
                        out.setdefault(path, os.path.relpath(p, ROOT))
    return out


# ---------------------------------------------------------------------------- 1
def test_the_page_calls_only_listed_families():
    rows = routes()
    assert {r["owner"] for r in rows} <= set(OWNERS), rows
    assert len({r["prefix"] for r in rows}) == len(rows), "a prefix listed twice"
    calls = page_paths()
    assert len(calls) > 50, f"the extraction found too little ({len(calls)}): its pattern is off"
    missing = {p: f for p, f in calls.items() if owner_of(p, rows) is None}
    assert not missing, ("the page calls these, and docs/controller-routes.json says nothing of "
                         f"them: add their family with its owner: {missing}")
    # the families of the bug: the page's settings, hooks, providers, plugins and health pages
    for path in ("/v1/settings", "/v1/fleet/health", "/v1/fleet/hooks", "/v1/fleet/plugins",
                 "/v1/fleet/decision-providers"):
        assert any(p == path or p.startswith(path + "/") for p in calls), path
        assert owner_of(path, rows)["owner"] == "controller", path
    used = {owner_of(p, rows)["prefix"] for p in calls}
    return f"{len(calls)} paths in {len(used)} families, all listed"


# ---------------------------------------------------------------------------- 2
def test_the_list_agrees_with_the_engine_contract():
    rows = routes()
    with open(os.path.join(ROOT, "contracts", "engine-api-v1", "routes.json")) as fh:
        eng = json.load(fh)
    for fam in eng["app_families"]:
        path = fam["path"].rstrip("*").rstrip("/") or "/"
        r = owner_of(path, rows)
        assert r is not None and r["owner"] == "controller", (fam["path"], r)
    for rt in eng["routes"]:
        path = re.sub(r"\{[^}]+\}", "x", rt["path"])
        r = owner_of(path, rows)
        assert r is not None, f"the engine serves {rt['path']}: list its family"
        assert r["owner"] in ("both", "engine"), (rt["path"], r)
    return f"{len(eng['app_families'])} app families, {len(eng['routes'])} engine routes"


# ---------------------------------------------------------------------------- 3
def test_the_install_doc_names_every_prefix():
    with open(INSTALL, encoding="utf-8") as fh:
        text = fh.read()
    at = text.find("## Behind a reverse proxy")
    assert at >= 0, "docs/install.md has no section 'Behind a reverse proxy'"
    end = text.find("\n## ", at + 5)
    sec = text[at:end if end > 0 else None]
    assert "controller-routes.json" in sec
    for r in routes():
        assert f"`{r['prefix']}`" in sec, f"the install doc's list lacks {r['prefix']}"
    return f"{len(routes())} prefixes in docs/install.md"


# ---------------------------------------------------------------------------- 4
def test_the_controller_answers_what_the_list_says():
    from proxy_util import Proxy
    rows = routes()
    seen = {}
    app_src = ""
    for pkg in ("proxy", "tandemapp"):
        for f in sorted(os.listdir(os.path.join(ROOT, pkg))):
            if f.endswith(".py"):
                with open(os.path.join(ROOT, pkg, f), encoding="utf-8") as fh:
                    app_src += fh.read()
    def ask_on(px, probe: str) -> tuple[int, str]:
        method, path = probe.split(" ", 1)
        st, _, out = px.request(method, path, {} if method == "POST" else None)
        return st, out.decode("utf-8", "replace")

    # what needs the conductor answers 404 while it is off, as a new install has it
    with Proxy({}) as px:
        for r in rows:
            if r.get("needs") == "conductor":
                st, text = ask_on(px, r["probe"])
                # SEC-28: a session address the controller never gave is no credential: the
                # sign-in gate's 401 comes before the conductor's 404
                if r["prefix"] == "/s" and st == 401 and "sign_in_required" in text:
                    continue
                assert st == 404, (r["prefix"], "answered with the conductor off", st, text[:200])
    with Proxy({}, conductor=True) as px:
        def ask(probe: str) -> tuple[int, str]:
            return ask_on(px, probe)

        def no_route(st: int, text: str) -> bool:
            # the controller's own answer for a path it does not dispatch (proxy/app.py)
            return st == 404 and '"no route' in text
        for r in rows:
            if r["owner"] in ("controller", "both") and r.get("when"):
                # answered only in a state this controller is not in: dispatched by name
                assert f'"{r["prefix"]}' in app_src, (r["prefix"], "named nowhere in the controller")
            elif r["owner"] in ("controller", "both"):
                assert r.get("probe"), f"{r['prefix']}: a controller prefix needs a probe"
                st, text = ask(r["probe"])
                assert not no_route(st, text), (r["prefix"], r["probe"], st, text[:200])
                seen[r["prefix"]] = st
            elif r["owner"] == "engine":
                path = r["prefix"] + ("/x" if r["match"] == "prefix" else "")
                st, text = ask(f"GET {path}")
                assert st == 404, (r["prefix"], st, text[:200])
    return f"{len(seen)} prefixes answered by the controller"


if __name__ == "__main__":
    from scriptmode import run
    run(globals())
