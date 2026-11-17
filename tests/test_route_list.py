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
  5  OPS-300: docs/proxy.md's fronts leave no way around the controller: the bare-engine front
     sends every listed path to TandemApp, the front of an engine with its own page sends every
     controller prefix; both block the internal ones
                                                                  test_the_front_snippets_send_every_controller_prefix
  6  the front of an engine with its own page sends the engine nothing without a sign-in of
     TandemApp: every engine location asks the controller first, at a route that needs a
     credential; /health alone stays open and says the accounts are on
                                                                  test_the_mixed_front_signs_in_before_the_engine

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


# ---------------------------------------------------------------------------- 5
PROXY_DOC = os.path.join(ROOT, "docs", "proxy.md")
LOCATION = re.compile(r"^\s{4}location\s+(=|\^~)?\s*(\S+)\s*\{\s*(.*?)\s*\}\s*$")


def doc_blocks(section: str) -> list[list[tuple[str, str, str]]]:
    """The nginx blocks (indented `location` lines) of one section of docs/proxy.md:
    [[(modifier, path, body), ...], ...] in the order they appear."""
    with open(PROXY_DOC, encoding="utf-8") as fh:
        text = fh.read()
    at = text.find(section)
    assert at >= 0, f"docs/proxy.md has no {section!r}"
    end = text.find("\n### ", at + 5)
    blocks, cur = [], []
    for line in text[at:end if end > 0 else None].splitlines():
        m = LOCATION.match(line)
        if m:
            cur.append((m.group(1) or "", m.group(2), m.group(3)))
        elif cur and line.strip() == "":
            blocks.append(cur)
            cur = []
        elif cur and not line.startswith("    "):
            blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)
    return blocks


def nginx_pick(locations, path: str, fallback: str) -> str:
    """Where nginx sends `path` (no regex locations here): an exact match first, else the longest
    prefix; 'controller' for the data-plane snippet, 'blocked' for a return, else `fallback`."""
    best = None
    for mod, loc, body in locations:
        if mod == "=" and path == loc:
            best = (10 ** 6, body)
            break
        if mod in ("^~", "") and path.startswith(loc) and (best is None or len(loc) > best[0]):
            best = (len(loc), body)
    if best is None:
        return fallback
    body = best[1]
    if "tandemapp-data-plane.conf" in body:
        return "controller"
    if body.startswith("return"):
        return "blocked"
    return f"? {body}"


def probes(r: dict) -> list[str]:
    p = r["prefix"]
    if r["match"] == "exact":
        return [p]
    return [p, p.rstrip("/") + "/x/y"]


def test_the_front_snippets_send_every_controller_prefix():
    """OPS-300: no way around the controller. The bare-engine front sends every listed path to
    TandemApp and blocks the internal ones. The front of an engine that still serves its own page
    sends TandemApp every prefix only TandemApp answers (with the data plane of 'Through a reverse
    proxy'), blocks the internal ones, and leaves only engine and both prefixes to the engine."""
    rows = routes()
    data_plane = doc_blocks("### Through a reverse proxy")[0]
    bare, mixed = doc_blocks("### No way around the controller")[:2]
    assert len(data_plane) == 8, data_plane
    for r in rows:
        for path in probes(r):
            want = "blocked" if r["owner"] == "internal" else "controller"
            got = nginx_pick(bare, path, "nothing")
            assert got == want, ("bare", path, r["owner"], got)
            got = nginx_pick(data_plane + mixed, path, "engine")
            if r["owner"] in ("controller", "internal"):
                assert got == want, ("mixed", path, r["owner"], got)
            else:
                assert got in ("engine", "controller"), ("mixed", path, got)
    # the data plane and the new routes are the controller's in both
    for path in ("/v1/messages", "/v1/messages/count_tokens", "/v1/auth/session",
                 "/v1/chat/completions", "/v1/models"):
        assert nginx_pick(data_plane + mixed, path, "engine") == "controller", path
    return f"{len(rows)} prefixes, {len(bare)} bare and {len(mixed)} mixed locations"



def test_the_mixed_front_signs_in_before_the_engine():
    """The engine has no sign-in for its dashboard, conductor and library reads: behind the mixed
    front a page showed the engine's live view to a browser that never signed in, because the
    page found no X-Tandem-Auth on the engine's /health and every engine read answered."""
    from proxy import authz
    gate = doc_blocks("### No way around the controller")[2]
    by = {(mod, loc): body for mod, loc, body in gate}
    fall = by[("", "/")]
    assert "auth_request /_tandemapp_signin;" in fall and ":8000" in fall, fall
    check = by[("=", "/_tandemapp_signin")]
    assert "internal;" in check, check
    target = re.search(r"proxy_pass http://[^/;]+(/[^;\s]*);", check).group(1)
    perm = authz.permission_for("GET", target)
    assert perm not in (None, authz.OPEN), (target, perm)
    health = by[("=", "/health")]
    assert "auth_request" not in health and "X-Tandem-Auth accounts" in health, health
    # every path the mixed front leaves to the engine lands in the gated location
    mixed = doc_blocks("### No way around the controller")[1]
    data_plane = doc_blocks("### Through a reverse proxy")[0]
    left = [p for r in routes() for p in probes(r) if r["owner"] in ("engine", "both")
            and nginx_pick(data_plane + mixed, p, "engine") == "engine"]
    assert left, "the mixed front leaves nothing to the engine"
    for p in left:
        if p == "/health":
            continue
        assert nginx_pick(gate, p, "nothing").startswith("? auth_request"), p
    return f"{len(left)} engine paths behind {target} ({perm}), /health open"

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
                # SEC-28: the sign-in gate refuses a path the controller does not dispatch
                # before any handler (403 unmapped_route): the controller does not answer it
                assert st == 404 or (st == 403 and "unmapped_route" in text), \
                    (r["prefix"], st, text[:200])
    return f"{len(seen)} prefixes answered by the controller"


if __name__ == "__main__":
    from scriptmode import run
    run(globals())
