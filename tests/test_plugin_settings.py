"""SRV-857 (QA SRV-858): the settings interface's plugins scope (proxy/pluginsettings.py) over the
plugin registry, in a temporary config database. Every answer is held to contracts/settings-v1.

  1  Given two installed plugins, When the overview and a plugin's schema are read, Then the
     plugins scope comes after hooks with one target a plugin; its keys come from the manifest:
     enabled, settings.<property> with the schema's type, values and default, secrets.<NAME>
     write-only, capabilities.<capability> false by default; values carry the plugin's title
                                                               test_keys_from_the_manifest
  2  Settings apply after the property's schema checks them (a bad value is refused on its key);
     each plugin keeps its own versions, and restoring one leaves the other as it is; unset goes
     back to the schema's default; a required property without a default has no unset
                                                               test_settings_versions_restore
  3  Capabilities requested vs granted, deny by default: a grant and enabling take pairing's
     rights (403 trust_required); a LAN host or prompts:redacted applies with one confirm;
     prompts:full, engine:control and a host outside the LAN take confirm_grant (428 without it,
     409 with a wrong one) and the grant records who confirmed; unset revokes; a restore that
     brings a grant back asks again; the registry never holds a capability that is not requested
                                                               test_capability_grants
  4  Enabling through the settings checks the tree: a changed tree is refused on `enabled`; the
     plan writes nothing                                       test_enable_checks_the_tree
  5  Secrets never in an answer, a version, an audit row, a log line or the controller move's
     export; a secret set through the scope reaches only the registry, sealed
                                                               test_secrets_never_leave
  6  One change set over the hooks scope and the plugins scope applies hooks first, then plugins
                                                               test_change_set_order

Run: python tests/test_plugin_settings.py
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from plugin_util import Reg, example, folder  # noqa: E402

from proxy import ctlmove  # noqa: E402
from proxy import hooksettings  # noqa: E402
from proxy import pluginsettings  # noqa: E402
from proxy import settings as settings_mod  # noqa: E402
from proxy import settingscopes  # noqa: E402
from tools import contract_check  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CON = os.path.join(ROOT, "contracts", "settings-v1")
KEY = "sk-" + "Pz4" * 12
KEY2 = "sk-" + "Qy5" * 12


def ok(doc, name: str):
    errs = contract_check.validate(doc, contract_check.load(name, CON), root=CON)
    assert not errs, (name, errs[:5], json.dumps(doc, default=str)[:600])
    return doc


class Rig(Reg):
    def __init__(self, label: str):
        super().__init__(label)
        self.srcs = []
        self.scope = pluginsettings.PluginsScope(self.cfg, self.store)
        self.s = settings_mod.Settings(self.cfg, {
            "hooks": settingscopes.HooksScope(self.cfg, hooksettings.RegistryHooks(self.runner)),
            "plugins": self.scope}, log=self.logs.append)
        self.answers: list = []

    def add(self, name: str, manifest=None):
        self.srcs.append(folder(name, manifest))
        return self.install(self.srcs[-1])[0]

    def call(self, method, path, body=None, q=None, trust=False):
        parts = [p for p in path.strip("/").split("/") if p]
        st, out = self.s.handle(method, parts, q or {}, body, "admin", trust=trust)
        self.answers.append(out)
        return st, out

    def plan(self, target, sets=None, unsets=None, trust=True, status=200):
        st, p = self.call("POST", "plugins/plan", {"target": target,
                                                    **({"set": sets} if sets else {}),
                                                    **({"unset": unsets} if unsets else {})},
                          trust=trust)
        assert st == status, (st, p)
        return ok(p, "plan") if st == 200 else p

    def apply(self, target, sets=None, unsets=None, trust=True, status=200, plan=None, **kw):
        plan = plan or self.plan(target, sets, unsets, trust)
        body = {"target": target, **({"set": sets} if sets else {}),
                **({"unset": unsets} if unsets else {}), "confirm": plan["confirm"], **kw}
        st, res = self.call("POST", "plugins/apply", body, trust=trust)
        assert st == status, (st, res)
        return ok(res, "result") if st == 200 else res

    def close(self):
        super().close()
        for s in self.srcs:
            shutil.rmtree(s)


# ---------------------------------------------------------------------------- 1
def test_keys_from_the_manifest():
    r = Rig("keys")
    try:
        st, ov = r.call("GET", "")
        assert ok(ov, "overview")["order"] == ["hooks", "plugins"]
        assert [s["targets"] for s in ov["scopes"] if s["scope"] == "plugins"] == [[]]
        st, err = r.call("GET", "plugins/schema")
        assert st == 404 and "install one" in err["error"]["message"], err
        r.add("hello-tool")
        r.add("classify-provider")
        st, ov = r.call("GET", "")
        ts = [s for s in ok(ov, "overview")["scopes"] if s["scope"] == "plugins"][0]["targets"]
        assert [(t["id"], t["label"]) for t in ts] == [("classify-provider", "Keyword classifier"),
                                                       ("hello-tool", "Hello tool")]
        st, err = r.call("GET", "plugins/schema")
        assert st == 400 and err["error"]["type"] == "target_required"
        st, sch = r.call("GET", "plugins/schema", q={"target": "classify-provider"})
        keys = {k["key"]: k for k in ok(sch, "schema")["keys"]}
        assert sorted(keys) == ["capabilities.network:192.168.1.30:8720",
                                "capabilities.prompts:redacted", "capabilities.secrets:API_KEY",
                                "enabled", "secrets.API_KEY", "settings.classes",
                                "settings.endpoint", "settings.fallback"], sorted(keys)
        assert keys["enabled"]["type"] == "boolean" and keys["enabled"]["default"] is False
        f = keys["settings.fallback"]
        assert f["type"] == "enum" and f["values"] == ["code", "chat", "unclear"]
        assert f["default"] == "unclear" and not f["nullable"]
        assert keys["settings.classes"]["type"] == "object"
        assert keys["settings.endpoint"]["type"] == "string" and keys["settings.endpoint"]["nullable"]
        sec = keys["secrets.API_KEY"]
        assert sec["secret"] and sec["write_only"] and sec["type"] == "secret"
        for c in ("prompts:redacted", "secrets:API_KEY", "network:192.168.1.30:8720"):
            k = keys[f"capabilities.{c}"]
            assert k["type"] == "boolean" and k["default"] is False
        st, vals = r.call("GET", "plugins", q={"target": "classify-provider"})
        rows = {v["key"]: v for v in ok(vals, "values")["values"]}
        assert rows["settings.fallback"]["value"] == "unclear" and not rows["settings.fallback"]["set"]
        assert rows["settings.fallback"]["label"] == "Keyword classifier"
        assert rows["secrets.API_KEY"]["value"] is None and not rows["secrets.API_KEY"]["set"]
        assert rows["capabilities.prompts:redacted"]["value"] is False
        assert rows["enabled"]["value"] is False and rows["enabled"]["source"] == "plugin"
        st, sch = r.call("GET", "plugins/schema", q={"target": "hello-tool"})
        assert sorted(k["key"] for k in sch["keys"]) == ["enabled", "settings.excited",
                                                          "settings.greeting"]
        st, err = r.call("POST", "plugins/plan", {"target": "hello-tool",
                                                  "set": {"capabilities.prompts:full": True}},
                         trust=True)
        assert st == 422 and err["error"]["type"] == "unknown_key", "not requested: no key"
    finally:
        r.close()
    return "keys from the manifest; capabilities false by default"


# ---------------------------------------------------------------------------- 2
def test_settings_versions_restore():
    r = Rig("versions")
    try:
        r.add("hello-tool")
        r.add("classify-provider")
        res = r.apply("hello-tool", {"settings.greeting": "Hi", "settings.excited": True})
        assert res["state"] == "applied"
        assert r.store.get("hello-tool")["settings"] == {"greeting": "Hi", "excited": True}
        before = res["parts"][0]["restore"][0]["version"]
        st, err = r.call("POST", "plugins/plan", {"target": "hello-tool",
                                                  "set": {"settings.greeting": "x" * 41}})
        assert st == 422 and err["error"]["type"] == "bad_value" and \
            err["error"]["key"] == "settings.greeting", err
        st, err = r.call("POST", "plugins/plan", {"target": "hello-tool",
                                                  "set": {"settings.excited": "yes"}})
        assert st == 422 and err["error"]["key"] == "settings.excited"
        st, err = r.call("POST", "plugins/plan", {"target": "classify-provider",
                                                  "set": {"settings.classes": {"x": ["a"]}}})
        assert st == 422 and "not a known field" in err["error"]["message"], err
        st, err = r.call("POST", "plugins/plan", {"target": "classify-provider",
                                                  "unset": ["settings.fallback"]})
        assert st == 200, "fallback has a default: unset goes back to it"
        r.apply("classify-provider", {"settings.fallback": "chat"})
        r.apply("hello-tool", {"settings.greeting": "Hey"})
        st, vers = r.call("GET", "plugins/versions", q={"target": "hello-tool"})
        assert len(ok(vers, "versions")["versions"]) >= 3
        st, rp = r.call("POST", "plugins/restore", {"target": "hello-tool", "version": before})
        assert st == 200 and ok(rp, "plan")["changed"], rp
        st, rr = r.call("POST", "plugins/restore", {"target": "hello-tool", "version": before,
                                                    "confirm": rp["confirm"]})
        assert st == 200 and rr["state"] == "applied", rr
        assert r.store.get("hello-tool")["settings"].get("greeting") is None or \
            r.store.effective_settings(r.store.get("hello-tool"))["greeting"] == "Hello"
        assert r.store.get("classify-provider")["settings"] == {"fallback": "chat"}, \
            "the other plugin is left as it is"
        r.apply("hello-tool", unsets=["settings.excited"])
        assert "excited" not in r.store.get("hello-tool")["settings"]
        m = example("hello-tool")
        m["name"], m["title"] = "needs-name", "Needs a name"
        m["settings"]["properties"]["who"] = {"type": "string"}
        m["settings"]["required"] = ["who"]
        r.add("hello-tool", m)
        r.apply("needs-name", {"settings.who": "me"})
        st, err = r.call("POST", "plugins/plan", {"target": "needs-name",
                                                  "unset": ["settings.who"]})
        assert st == 422 and "required" in err["error"]["message"], err
        rows = [a for a in r.audit() if a["object"] == "plugin" and a["action"] == "settings"]
        assert rows and all(a["change_set"] for a in rows), rows
    finally:
        r.close()
    return "checked by the schema; one plugin's restore leaves the other"


# ---------------------------------------------------------------------------- 3
def test_capability_grants():
    r = Rig("grants")
    try:
        m = example("classify-provider")
        m["capabilities"] += ["prompts:full", "engine:control", "network:203.0.113.7:443"]
        r.add("classify-provider", m)
        t = "classify-provider"
        for key in ("capabilities.prompts:redacted", "enabled"):
            st, err = r.call("POST", "plugins/plan", {"target": t, "set": {key: True}})
            assert st == 403 and err["error"]["type"] == "trust_required", err
            assert err["error"]["key"] == key
        p = r.plan(t, {"capabilities.prompts:redacted": True,
                       "capabilities.network:192.168.1.30:8720": True})
        assert p["grant_keys"] == [] and p["confirm_grant"] is None
        assert any("gets prompts:redacted" in w for w in p["parts"][0]["warnings"])
        r.apply(t, {"capabilities.prompts:redacted": True,
                    "capabilities.network:192.168.1.30:8720": True})
        g = r.store.get(t)["granted"]
        assert sorted(g) == ["network:192.168.1.30:8720", "prompts:redacted"]
        assert g["prompts:redacted"]["by"] == "admin" and g["prompts:redacted"]["confirmed_by"] is None
        for cap in ("prompts:full", "engine:control", "network:203.0.113.7:443"):
            key = f"capabilities.{cap}"
            p = r.plan(t, {key: True})
            assert p["grant_keys"] == [f"plugins/{t}:{key}"] and p["confirm_grant"], p
            err = r.apply(t, {key: True}, status=428, plan=p)
            assert err["error"]["type"] == "grant_confirm_required"
            err = r.apply(t, {key: True}, status=409, plan=p, confirm_grant="0" * 32)
            assert err["error"]["type"] == "stale_plan"
            assert cap not in r.store.get(t)["granted"]
            r.apply(t, {key: True}, plan=p, confirm_grant=p["confirm_grant"])
            got = r.store.get(t)["granted"][cap]
            assert got["confirmed_by"] == "admin" and got["confirmed_at"], got
        before = r.store.get(t)
        st, vers = r.call("GET", "plugins/versions", q={"target": t})
        v_full = vers["versions"][0]["id"]
        r.apply(t, unsets=["capabilities.prompts:full"])
        assert "prompts:full" not in r.store.get(t)["granted"]
        st, rp = r.call("POST", "plugins/restore", {"target": t, "version": v_full}, trust=True)
        assert st == 200 and rp["confirm_grant"] and rp["grant_keys"], rp
        st, rr = r.call("POST", "plugins/restore", {"target": t, "version": v_full,
                                                    "confirm": rp["confirm"]}, trust=True)
        assert st == 428 and rr["error"]["type"] == "grant_confirm_required", rr
        st, rr = r.call("POST", "plugins/restore", {"target": t, "version": v_full,
                                                    "confirm": rp["confirm"],
                                                    "confirm_grant": rp["confirm_grant"]},
                        trust=True)
        assert st == 200 and "prompts:full" in r.store.get(t)["granted"], rr
        st, rp = r.call("POST", "plugins/restore", {"target": t, "version": v_full})
        assert st == 200 and not rp["changed"]
        assert set(r.store.get(t)["granted"]) <= set(r.store.requested(before))
        audit = [a for a in r.audit() if a["object"] == "plugin" and a["detail"]]
        details = [json.loads(a["detail"]) for a in audit]
        assert any("prompts:full" in d.get("confirmed", []) for d in details), details
        assert any("prompts:full" in d.get("revoked", []) for d in details), details
    finally:
        r.close()
    return "deny by default; trust for every grant; second confirm for full, control, away"


# ---------------------------------------------------------------------------- 4
def test_enable_checks_the_tree():
    r = Rig("enable")
    try:
        p = r.add("hello-tool")
        with open(os.path.join(p["path"], "server.py"), "a") as f:
            f.write("# changed\n")
        st, err = r.call("POST", "plugins/plan", {"target": "hello-tool",
                                                  "set": {"enabled": True}}, trust=True)
        assert st == 422 and err["error"]["key"] == "enabled" and "mismatch" in \
            err["error"]["message"], err
        assert r.store.get("hello-tool")["integrity"] == "ok", "a plan writes nothing"
        with open(os.path.join(p["path"], "server.py")) as f:
            body = f.read()
        with open(os.path.join(p["path"], "server.py"), "w") as f:
            f.write(body.replace("# changed\n", ""))
        res = r.apply("hello-tool", {"enabled": True})
        assert res["state"] == "applied" and r.store.get("hello-tool")["enabled"]
        r.apply("hello-tool", unsets=["enabled"], trust=False)
        assert r.store.get("hello-tool")["enabled"] is False, "disabling needs no trust"
    finally:
        r.close()
    return "a changed tree is refused on enabled; a plan writes nothing"


# ---------------------------------------------------------------------------- 5
def test_secrets_never_leave():
    r = Rig("secrets")
    try:
        r.add("classify-provider")
        t = "classify-provider"
        p = r.plan(t, {"secrets.API_KEY": KEY})
        assert p["parts"][0]["changes"][0]["secret"] and p["parts"][0]["changes"][0]["to"] is None
        r.apply(t, {"secrets.API_KEY": KEY})
        assert r.store.secret_for(t, "API_KEY") == KEY
        r.apply(t, {"secrets.API_KEY": KEY2, "settings.fallback": "code"})
        st, vals = r.call("GET", "plugins", q={"target": t})
        row = [v for v in vals["values"] if v["key"] == "secrets.API_KEY"][0]
        assert row["set"] and row["value"] is None and row["hint"], row
        for path in ("plugins/schema", "plugins", "plugins/versions", "audit", ""):
            r.call("GET", path, q={"target": t} if path.startswith("plugins") else None)
        con = sqlite3.connect(r.cfg.path)
        dump = "\n".join(con.iterdump())
        con.close()
        exp = ctlmove.export(r.cfg, keys=False)
        d = tempfile.mkdtemp(prefix="plugin-export-")
        try:
            db = os.path.join(d, "x.sqlite3")
            with open(db, "wb") as f:
                f.write(base64.b64decode(exp["db"]))
            c = sqlite3.connect(db)
            assert c.execute("SELECT COUNT(*) FROM plugin_secrets").fetchone()[0] == 0
            assert c.execute("SELECT COUNT(*) FROM plugins").fetchone()[0] == 1
            c.close()
        finally:
            shutil.rmtree(d)
        blob = json.dumps(r.answers, default=str) + json.dumps(r.audit(), default=str) + \
            json.dumps(r.cfg.settings_versions("plugins", t), default=str) + \
            "\n".join(r.logs) + json.dumps(exp, default=str)
        for k in (KEY, KEY2):
            assert k not in blob, "a secret left the registry"
            assert k not in dump, "a secret stored in plain text"
        r.apply(t, unsets=["secrets.API_KEY"])
        assert r.store.secret_for(t, "API_KEY") is None
    finally:
        r.close()
    return f"{len(r.answers)} answers, the audit, the versions, the log, the export: no secret"


# ---------------------------------------------------------------------------- 6
def test_change_set_order():
    r = Rig("order")
    try:
        r.add("hello-tool")
        r.runner.store.create({"name": "Audit", "id": "audit", "role": "observer",
                               "stages": ["request.finished"],
                               "endpoint": "http://192.168.1.20:9300/h"}, who="t")
        body = {"changes": [{"scope": "plugins", "target": "hello-tool",
                             "set": {"settings.greeting": "Yo"}},
                            {"scope": "hooks", "set": {"hooks.audit.mode": "shadow"}}]}
        st, p = r.call("POST", "plan", body, trust=True)
        assert st == 200 and [x["scope"] for x in ok(p, "plan")["parts"]] == ["hooks", "plugins"]
        st, res = r.call("POST", "apply", dict(body, confirm=p["confirm"]), trust=True)
        assert st == 200 and [x["scope"] for x in res["parts"]] == ["hooks", "plugins"], res
        assert r.store.get("hello-tool")["settings"]["greeting"] == "Yo"
        assert r.runner.store.get("audit")["mode"] == "shadow"
    finally:
        r.close()
    return "hooks, then plugins"


if __name__ == "__main__":
    from scriptmode import run
    run(globals())
