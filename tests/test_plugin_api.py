"""SRV-857 (QA SRV-858): the plugins' admin API end to end through a running controller
(proxy/pluginapi.py, /v1/fleet/plugins) and the plugins scope over HTTP (/v1/settings/plugins).
Every answer validates against contracts/plugin-v1 (and settings-v1).

  1  Valid plugin over HTTP: inspect a folder (manifest, checks, requested capabilities, the
     settings' form; nothing installed), install it (201, sha256 recorded, disabled), list and get
     it, enable, verify, disable, remove; a git source pinned to a commit installs, a branch name
     is refused (422 commit_required); a wheel installs        test_lifecycle_over_http
  2  Refusals over HTTP: a missing annotation (422 plugin_refused naming the tool and the
     annotation, every error listed), extra properties, a newer TandemApp (the needed version), a
     second install of the name (409 exists), removal while a hook names it (409 referenced), an
     unknown field in the body, an unknown plugin (404)        test_refusals_over_http
  3  The rights: from another machine without the admin's token every route is 401; with it,
     reads and installs work; with no admin password set, install, upload, enable, trust,
     remove and the policy are this machine's only (403 trust_required from another, SEC-16); a
     write from another origin is 403
                                                               test_rights
  4  The settings, secrets and grants over /v1/settings/plugins; no secret in any answer, the log,
     a file under the controller's state dir in plain text, or the move's export
                                                               test_settings_and_secrets_over_http
  5  A wheel uploaded as bytes is kept for an hour and answers the source to inspect and install it
     with; no name, another content type, bytes that are no zip and another machine without
     pairing's rights are refused; past its time it is gone. The list carries each plugin's run
     state, and every plugin's view names the grants that take the second confirm, as inspect
                                                               test_upload_run_and_confirm

Run: python tests/test_plugin_api.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from plugin_util import example, example_files, folder, repo, wheel  # noqa: E402
from proxy_util import Proxy, run  # noqa: E402

from proxy import app as proxy_app  # noqa: E402
from proxy import settings as settings_mod  # noqa: E402
from tools import contract_check  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PV = os.path.join(ROOT, "contracts", "plugin-v1")
SV = os.path.join(ROOT, "contracts", "settings-v1")
BASE = "/v1/fleet/plugins"
AWAY = {"X-Forwarded-For": "203.0.113.50"}
KEY = "sk-" + "Hx7" * 12


def _ok(doc, name, root=PV):
    errs = contract_check.validate(doc, contract_check.load(f"{name}.schema.json", root),
                                   root=root)
    assert not errs, (name, errs[:5], json.dumps(doc)[:500])
    return doc


def _j(px, method, path, body=None, want=200, headers=None):
    st, _, out = px.request(method, path, body, headers)
    doc = json.loads(out or b"{}")
    assert st == want, (method, path, st, doc)
    if st >= 400 and path.startswith(BASE):
        _ok(doc, "error")
        api = json.load(open(os.path.join(PV, "api.json")))
        assert api["errors"].get(doc["error"]["type"]) == st, (doc["error"]["type"], st)
    return doc


def _versions():
    from proxy import _version
    return _version.release()


# ---------------------------------------------------------------------------- 1
def test_lifecycle_over_http():
    src = folder()
    d = tempfile.mkdtemp(prefix="plugin-api-")
    try:
        with Proxy({}) as px:
            listed = _ok(_j(px, "GET", BASE), "plugins")
            assert listed["plugins"] == [] and listed["plugin_api"] == "1.0"
            assert listed["hooks_api"] == "1.0" and listed["tandemapp"] == _versions()
            ins = _ok(_j(px, "POST", BASE + "/inspect", {"source": {"type": "folder",
                                                                    "path": src}}), "inspect")
            assert ins["ok"] and ins["manifest"]["name"] == "hello-tool" and ins["installed"] is None
            assert ins["settings_form"]["fields"][0]["key"] == "greeting"
            assert _j(px, "GET", BASE)["plugins"] == [], "inspect installs nothing"
            made = _ok(_j(px, "POST", BASE, {"source": {"type": "folder", "path": src}}, 201),
                       "install")
            p = made["plugin"]
            assert made["action"] == "installed" and p["enabled"] is False
            assert len(p["sha256"]) == 64 and p["integrity"] == "ok"
            assert p["contributes"] == {"tools": 1} and p["settings"]["greeting"] == "Hello"
            assert os.path.isdir(os.path.join(px.dir, "plugins", "hello-tool", "0.1.0"))
            again = _ok(_j(px, "POST", BASE + "/inspect", {"source": {"type": "folder",
                                                                      "path": src}}), "inspect")
            assert again["installed"] == {"version": "0.1.0", "sha256": p["sha256"]}
            one = _ok(_j(px, "GET", BASE + "/hello-tool"), "plugin-one")
            assert one["plugin"]["manifest"]["contributes"]["tools"][0]["name"] == "greet"
            on = _ok(_j(px, "POST", BASE + "/hello-tool/enable", {}), "plugin-one")
            assert on["plugin"]["enabled"] is True
            v = _ok(_j(px, "POST", BASE + "/hello-tool/verify", {}), "verify")
            assert v["integrity"] == "ok" and v["sha256"] == p["sha256"]
            off = _ok(_j(px, "POST", BASE + "/hello-tool/disable", {}), "plugin-one")
            assert off["plugin"]["enabled"] is False
            gone = _ok(_j(px, "DELETE", BASE + "/hello-tool"), "removed")
            assert gone == {"contract_version": "1.0", "removed": "hello-tool",
                            "version": "0.1.0"}
            # git at a commit; a branch name refused
            g, c = repo(example_files("hello-tool"))
            err = _j(px, "POST", BASE, {"source": {"type": "git", "url": g, "commit": "main"}},
                     422)
            assert err["error"]["type"] == "commit_required"
            made = _ok(_j(px, "POST", BASE, {"source": {"type": "git", "url": g, "commit": c}},
                          201), "install")
            assert made["plugin"]["source"] == {"type": "git", "url": g, "commit": c}
            _j(px, "DELETE", BASE + "/hello-tool")
            shutil.rmtree(g)
            # a wheel
            w = wheel(os.path.join(d, "hello_tool-0.1.0-py3-none-any.whl"),
                      example_files("hello-tool", "hello_tool"))
            made = _ok(_j(px, "POST", BASE, {"source": {"type": "wheel", "path": w}}, 201),
                       "install")
            assert made["plugin"]["manifest_dir"] == "hello_tool"
            rows = [r for r in proxy_app.STATE["svc"].cfg.audit(limit=50)
                    if r.get("object") == "plugin"]
            assert {r["action"] for r in rows} >= {"install", "enable", "disable", "remove"}
    finally:
        shutil.rmtree(src)
        shutil.rmtree(d)
    return "inspect, install (folder, git, wheel), get, enable, verify, disable, remove"


# ---------------------------------------------------------------------------- 2
def test_refusals_over_http():
    srcs = []
    try:
        with Proxy({}) as px:
            m = example("hello-tool")
            m["contributes"]["tools"][0]["annotations"].pop("idempotentHint")
            m["contributes"]["tools"][0]["inputSchema"]["additionalProperties"] = True
            srcs.append(folder(manifest=m))
            err = _j(px, "POST", BASE, {"source": {"type": "folder", "path": srcs[-1]}}, 422)
            e = err["error"]
            assert e["type"] == "plugin_refused" and "greet" in e["message"]
            assert {(x["code"], x.get("tool"), x.get("annotation")) for x in e["errors"]} == {
                ("annotation_missing", "greet", "idempotentHint"),
                ("extra_properties", "greet", None)}, e["errors"]
            ins = _ok(_j(px, "POST", BASE + "/inspect", {"source": {"type": "folder",
                                                                    "path": srcs[-1]}}),
                      "inspect")
            assert ins["ok"] is False and len(ins["errors"]) == 2 and ins["settings_form"] is None
            m = example("hello-tool")
            m["tandemapp"] = ">=42.0"
            srcs.append(folder(manifest=m))
            err = _j(px, "POST", BASE, {"source": {"type": "folder", "path": srcs[-1]}}, 422)
            x = err["error"]["errors"][0]
            assert x["code"] == "incompatible_tandemapp" and x["needed"] == ">=42.0"
            assert ">=42.0" in err["error"]["message"]
            srcs.append(folder())
            _j(px, "POST", BASE, {"source": {"type": "folder", "path": srcs[-1]}}, 201)
            m = example("hello-tool")
            m["version"] = "0.3.0"
            srcs.append(folder(manifest=m))
            err = _j(px, "POST", BASE, {"source": {"type": "folder", "path": srcs[-1]}}, 409)
            assert err["error"]["type"] == "exists" and err["error"]["installed"] == "0.1.0"
            up = _ok(_j(px, "POST", BASE, {"source": {"type": "folder", "path": srcs[-1]},
                                           "upgrade": True}), "install")
            assert up["action"] == "upgraded" and up["from"] == "0.1.0"
            svc = proxy_app.STATE["svc"]
            with svc.cfg._lock:
                svc.hooks.store.ready()
                svc.cfg._conn.execute(
                    "INSERT INTO hooks(id, name, kind, stages, role, target, enabled, "
                    "created_at, updated_at) VALUES ('h1', 'h1', 'plugin', "
                    "'[\"request.finished\"]', 'observer', 'hello-tool/x', 1, 0, 0)")
                svc.cfg._conn.commit()
            err = _j(px, "DELETE", BASE + "/hello-tool", want=409)
            assert err["error"]["type"] == "referenced" and err["error"]["hooks"][0]["id"] == "h1"
            got = _j(px, "GET", BASE + "/hello-tool")["plugin"]["references"]
            assert [r["id"] for r in got] == ["h1"]
            _j(px, "POST", BASE, {"source": {"type": "folder", "path": "/"}, "force": True}, 400)
            _j(px, "POST", BASE, {"source": {"type": "ftp", "path": "/x"}}, 422)
            _j(px, "POST", BASE, {"source": {"type": "folder", "path": "/no/such/folder"}}, 404)
            _j(px, "GET", BASE + "/nope", want=404)
            _j(px, "POST", BASE + "/hello-tool/explode", {}, 404)
            _j(px, "POST", BASE, ["not", "an", "object"], 400)
            # SRV-859: a plugin hook names a hook the plugin declares; hello-tool has none
            st, _, out = px.request("POST", "/v1/fleet/hooks", {
                "name": "p", "kind": "plugin", "role": "observer",
                "stages": ["request.finished"], "target": "hello-tool/x"})
            err = json.loads(out)["error"]
            assert st == 422 and (err["type"], err["field"]) == ("invalid", "target"), err
    finally:
        for s in srcs:
            shutil.rmtree(s)
    return "annotation, extra properties, version, exists, referenced, bodies"


# ---------------------------------------------------------------------------- 3
def test_rights():
    src = folder()
    token = "adm-" + "p" * 40
    try:
        body = {"source": {"type": "folder", "path": src}}
        with Proxy({"TANDEM_ADMIN_TOKEN": token}) as px:
            for method, path, b in (("GET", BASE, None), ("POST", BASE + "/inspect", body),
                                    ("POST", BASE, body), ("GET", BASE + "/hello-tool", None),
                                    ("POST", BASE + "/hello-tool/enable", {}),
                                    ("DELETE", BASE + "/hello-tool", None)):
                st, _, _ = px.request(method, path, b, AWAY)
                assert st == 401, (method, path, st)
            h = dict(AWAY, Authorization=f"Bearer {token}")
            _j(px, "POST", BASE, body, 201, h)
            _j(px, "POST", BASE + "/hello-tool/enable", {}, 200, h)
            _j(px, "GET", BASE, headers=h)
            st, _, _ = px.request("POST", BASE + "/hello-tool/disable", {},
                                  {"Origin": "http://evil.example", "Host": "127.0.0.1"})
            assert st == 403
            _j(px, "DELETE", BASE + "/hello-tool", headers=h)
        with Proxy({}) as px:                                     # no admin password
            # SEC-16: the configuration's writes but not pairing's rights: 403 trust_required,
            # as the hooks and the settings interface answer (not 401: nothing to sign in to)
            for method, path, b in (("POST", BASE, body),):
                err = _j(px, method, path, b, 403, AWAY)
                assert err["error"]["type"] == "trust_required", err
                assert "pairing" in err["error"]["message"]
            _j(px, "POST", BASE, body, 201)                       # from this machine
            _j(px, "GET", BASE, headers=AWAY)                     # reads: the configuration's
            for method, path, b in (("POST", BASE + "/hello-tool/enable", {}),
                                    ("POST", BASE + "/hello-tool/trust", {"level": "host"}),
                                    ("DELETE", BASE + "/hello-tool", None),
                                    ("POST", "/v1/fleet/plugin-trust/policy",
                                     {"require_signature_sandboxed": True})):
                st, _, out = px.request(method, path, b, AWAY)
                assert st == 403 and json.loads(out)["error"]["type"] == "trust_required", \
                    (method, path, st, out)
            _j(px, "POST", BASE + "/hello-tool/disable", {}, headers=AWAY)
            # the plugins scope: enabling and granting take pairing's rights there too
            st, _, out = px.request("POST", "/v1/settings/plugins/plan", {
                "target": "hello-tool", "set": {"enabled": True}}, AWAY)
            assert st == 403 and json.loads(out)["error"]["type"] == "trust_required"
    finally:
        shutil.rmtree(src)
    return "401 away without the token; pairing's rights for install, enable, remove"


# ---------------------------------------------------------------------------- 4
def test_settings_and_secrets_over_http():
    src = folder("classify-provider")
    try:
        with Proxy({}) as px:
            _j(px, "POST", BASE, {"source": {"type": "folder", "path": src}}, 201)
            answers = []

            def s(method, path, body=None, want=200):
                doc = _j(px, method, "/v1/settings" + path, body, want)
                answers.append(doc)
                return doc
            ov = _ok(s("GET", ""), "overview", SV)
            assert ov["order"][-2:] == ["hooks", "plugins"] and \
                ov["contract_version"] == settings_mod.CONTRACT
            sch = _ok(s("GET", "/plugins/schema?target=classify-provider"), "schema", SV)
            assert "secrets.API_KEY" in {k["key"] for k in sch["keys"]}
            sets = {"secrets.API_KEY": KEY, "settings.fallback": "code",
                    "capabilities.prompts:redacted": True, "enabled": True}
            p = _ok(s("POST", "/plugins/plan", {"target": "classify-provider", "set": sets}),
                    "plan", SV)
            res = _ok(s("POST", "/plugins/apply", {"target": "classify-provider", "set": sets,
                                                   "confirm": p["confirm"]}), "result", SV)
            assert res["state"] == "applied"
            one = _ok(_j(px, "GET", BASE + "/classify-provider"), "plugin-one")
            answers.append(one)
            pl = one["plugin"]
            assert pl["enabled"] and pl["capabilities"]["granted"] == ["prompts:redacted"]
            assert pl["settings"]["fallback"] == "code"
            assert pl["secrets"]["API_KEY"]["set"] and pl["secrets"]["API_KEY"]["last4"] == KEY[-4:]
            for path in ("/plugins?target=classify-provider",
                         "/plugins/versions?target=classify-provider", "/audit"):
                s("GET", path)
            answers.append(_j(px, "GET", BASE))
            st, _, out = px.request("GET", "/v1/fleet/app/export")
            answers.append(out.decode())
            blob = json.dumps(answers, default=str) + "\n".join(px.logs)
            assert KEY not in blob, "a plugin secret in an answer or the log"
            for base, _, names in os.walk(px.dir):
                for n in names:
                    with open(os.path.join(base, n), "rb") as f:
                        assert KEY.encode() not in f.read(), os.path.join(base, n)
            assert proxy_app.STATE["svc"].plugins.secret_for("classify-provider",
                                                             "API_KEY") == KEY
    finally:
        shutil.rmtree(src)
    return "settings, a secret and a grant over HTTP; no secret anywhere"


# ---------------------------------------------------------------------------- 5
def test_upload_run_and_confirm():
    from proxy import pluginapi
    d = tempfile.mkdtemp(prefix="plugin-upload-")
    whl = wheel(os.path.join(d, "hello_tool-0.1.0-py3-none-any.whl"),
                example_files("hello-tool"))
    with open(whl, "rb") as f:
        data = f.read()
    octet = {"Content-Type": "application/octet-stream"}
    name = "?name=hello_tool-0.1.0-py3-none-any.whl"
    m = example("classify-provider")
    m["capabilities"] = m["capabilities"] + ["prompts:full", "network:203.0.113.9:443"]
    src = folder("classify-provider", manifest=m)
    try:
        with Proxy({}) as px:
            up = _ok(_j(px, "POST", BASE + "/upload" + name, data, 201, octet), "upload")
            p = up["source"]["path"]
            assert os.path.isfile(p) and p.startswith(os.path.join(px.dir, "plugins",
                                                                   ".uploads"))
            assert up["bytes"] == len(data) and len(up["sha256"]) == 64
            assert stat_mode(p) == 0o600
            assert up["expires_at"] > time.time() * 1000
            ins = _ok(_j(px, "POST", BASE + "/inspect", {"source": up["source"]}), "inspect")
            assert ins["ok"] and ins["manifest"]["name"] == "hello-tool", ins
            _j(px, "POST", BASE + "/upload", data, 400, octet)              # no name
            _j(px, "POST", BASE + "/upload?name=../x.whl", data, 400, octet)
            _j(px, "POST", BASE + "/upload" + name, data, 415, {"Content-Type": "text/plain"})
            _j(px, "POST", BASE + "/upload" + name, b"not a zip", 422, octet)
            _j(px, "POST", BASE + "/upload" + name, b"", 400, octet)
            err = _j(px, "POST", BASE + "/upload" + name, data, 403, dict(octet, **AWAY))
            assert err["error"]["type"] == "trust_required", err
            made = _ok(_j(px, "POST", BASE, {"source": up["source"]}, 201), "install")
            assert made["plugin"]["source"]["type"] == "wheel"
            # past its time it is gone, and so is what it answered
            up2 = _j(px, "POST", BASE + "/upload" + name, data, 201, octet)
            pluginapi.sweep_uploads(proxy_app.STATE["svc"].plugins,
                                    now=time.time() + pluginapi.UPLOAD_TTL_S + 1)
            assert not os.path.exists(up2["source"]["path"])
            _j(px, "POST", BASE + "/inspect", {"source": up2["source"]}, 404)
            # the list: each plugin's run state; the view: the grants that take the confirm
            _j(px, "POST", BASE, {"source": {"type": "folder", "path": src}}, 201)
            listed = _ok(_j(px, "GET", BASE), "plugins")
            by = {x["id"]: x for x in listed["plugins"]}
            assert by["hello-tool"]["run"]["state"] == "disabled", by["hello-tool"]["run"]
            assert by["hello-tool"]["run"]["gave_up"] is None
            want = ["prompts:full", "network:203.0.113.9:443"]
            assert by["classify-provider"]["capabilities"]["confirm"] == want, by
            one = _ok(_j(px, "GET", BASE + "/classify-provider"), "plugin-one")
            assert one["plugin"]["capabilities"]["confirm"] == want
            assert by["hello-tool"]["capabilities"]["confirm"] == []
    finally:
        shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(src, ignore_errors=True)
    return "upload, inspect, install; refusals; expiry; run state; confirm list"


def stat_mode(p: str) -> int:
    return os.stat(p).st_mode & 0o777


if __name__ == "__main__":
    run(globals())
