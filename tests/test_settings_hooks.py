"""SRV-845 (D1 x E1): the settings interface's hooks scope over the real hook registry
(proxy/hooksettings.py over proxy/hookstore.py and the runner of proxy/hooks.py), in a temporary
config database. Every answer is held to contracts/settings-v1 (tools/contract_check.py).

  Gherkin and the acceptance criteria:
  1  Given an http hook and a builtin hook in the registry, When the hooks scope's schema is read,
     Then each hook has its switches (enabled, mode, priority, scope, policy, timeout_ms,
     payload) with their types and defaults, and the http hook its secret, write-only and not
     writable here; the values carry the hook's name, a secret only as set with its hint, and a
     key that holds nothing (no scope, no policy, no timeout) is unset
                                                        test_keys_from_the_registry
  2  Given a hook, When its mode, priority, scope, policy, timeout and enabled are applied as one
     change set, Then the registry holds them (audited there with the change set's id), the runner
     sees them at once, a version is recorded before and after, and its restore brings the hook
     back; unsetting the policy goes back to the stage's default, unsetting a key that has no
     unset, a bad value and a key of no hook are refused on that key
                                                        test_apply_version_restore
  3  The full payload (SEC-16): Given a caller without pairing's rights, When payload full is
     planned, Then 403 trust_required on the key; Given pairing's rights and a destination on the
     LAN, Then the plan warns and applies with one confirm, the grant on the LAN; Given a
     destination outside the LAN, Then the plan lists the key in grant_keys with confirm_grant,
     apply without it is 428 grant_confirm_required, a wrong one 409, and with it the grant
     records who confirmed it; a hook already full plans no grant again; a hook changed after the
     plan makes the plan stale; a restore that brings full back asks again
                                                        test_full_payload_grant
  4  No hook secret in any answer, version or audit row of the settings interface
                                                        test_no_secret_anywhere

CPU only. Run: python tests/test_settings_hooks.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from proxy import config as config_mod  # noqa: E402
from proxy import hooks as hooks_mod  # noqa: E402
from proxy import hooksettings  # noqa: E402
from proxy import settings as settings_mod  # noqa: E402
from proxy import settingscopes  # noqa: E402
from tools import contract_check  # noqa: E402

CON = os.path.join(ROOT, "contracts", "settings-v1")
LAN_URL = "http://192.168.1.20:9300/hook"
AWAY_URL = "http://203.0.113.7:9300/hook"


def ok(doc, name: str):
    errs = contract_check.validate(doc, contract_check.load(name, CON), root=CON)
    assert not errs, (name, errs[:5], json.dumps(doc, default=str)[:600])
    return doc


class Rig:
    """A config database, the hook registry's runner and the settings interface's hooks scope."""

    def __init__(self, label: str):
        self.dir = tempfile.mkdtemp(prefix=f"settings-hooks-{label}-")
        self.cfg = config_mod.ConfigDB(os.path.join(self.dir, "config.sqlite3"), env={},
                                       use_keychain=False).open()
        self.runner = hooks_mod.Runner(self.cfg)
        self.store = self.runner.store
        self.logs: list[str] = []
        self.scope = settingscopes.HooksScope(self.cfg, hooksettings.RegistryHooks(self.runner))
        self.s = settings_mod.Settings(self.cfg, {"hooks": self.scope}, log=self.logs.append)
        self.answers: list = []

    def hook(self, **doc):
        base = {"role": "observer", "stages": ["request.finished"]}
        if doc.get("kind") != "builtin":
            base["endpoint"] = LAN_URL
        h, secret = self.store.create({**base, **doc}, who="t")
        return h, secret

    def call(self, method: str, path: str, body=None, q=None, trust: bool = False):
        parts = [p for p in path.strip("/").split("/") if p]
        st, out = self.s.handle(method, parts, q or {}, body, "admin", trust=trust)
        self.answers.append(out)
        return st, out

    def plan(self, sets=None, unsets=None, trust=False, status=200):
        body = {"changes": [{"scope": "hooks", **({"set": sets} if sets else {}),
                             **({"unset": unsets} if unsets else {})}]}
        st, p = self.call("POST", "plan", body, trust=trust)
        assert st == status, (st, p)
        return ok(p, "plan") if st == 200 else p

    def apply(self, sets=None, unsets=None, trust=False, plan=None, status=200, **kw):
        plan = plan or self.plan(sets, unsets, trust)
        ch = [{"scope": "hooks", **({"set": sets} if sets else {}),
               **({"unset": unsets} if unsets else {})}]
        st, r = self.call("POST", "apply", {"changes": ch, "confirm": plan["confirm"], **kw},
                          trust=trust)
        assert st == status, (st, r)
        return ok(r, "result") if st == 200 else r

    def audit_rows(self) -> list[dict]:
        con = sqlite3.connect(self.cfg.path)
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute("SELECT * FROM audit ORDER BY id")]
        con.close()
        return rows

    def close(self):
        self.runner.stop()
        self.cfg.close()


# ---------------------------------------------------------------------------- 1 the keys

def test_keys_from_the_registry():
    r = Rig("keys")
    try:
        st, sch = r.call("GET", "hooks/schema")
        assert st == 200 and ok(sch, "schema")["keys"] == [] and "no hook yet" in sch["why"], sch
        h, _ = r.hook(name="Audit log", id="audit")
        r.hook(name="No guests", id="guard", kind="builtin", target="deny", role="guard",
               stages=["request.received"], priority=5)
        st, sch = r.call("GET", "hooks/schema")
        keys = {k["key"]: k for k in ok(sch, "schema")["keys"]}
        fields = ("enabled", "mode", "payload", "policy", "priority", "scope", "timeout_ms")
        assert sorted(keys) == sorted([f"hooks.audit.{f}" for f in fields + ("secret",)]
                                      + [f"hooks.guard.{f}" for f in fields]), sorted(keys)
        assert sch["why"] is None and sch["writable"]
        assert keys["hooks.audit.mode"]["values"] == ["off", "shadow", "on"]
        assert keys["hooks.audit.mode"]["default"] == "on"
        assert keys["hooks.audit.payload"]["values"] == ["meta", "redacted", "full"]
        assert keys["hooks.audit.priority"]["type"] == "integer"
        assert keys["hooks.audit.scope"]["type"] == "object" and keys["hooks.audit.scope"]["nullable"]
        assert keys["hooks.audit.policy"]["nullable"] and keys["hooks.audit.policy"]["default"] is None
        sec = keys["hooks.audit.secret"]
        assert sec["secret"] and sec["write_only"] and not sec["writable"] and sec["why"], sec
        assert all(k["restart"] == "none" for k in keys.values())
        st, vals = r.call("GET", "hooks")
        rows = {v["key"]: v for v in ok(vals, "values")["values"]}
        assert rows["hooks.audit.mode"]["value"] == "on" and rows["hooks.audit.mode"]["set"]
        assert rows["hooks.audit.mode"]["label"] == "Audit log"
        assert rows["hooks.audit.mode"]["source"] == "hooks"
        assert rows["hooks.guard.priority"]["value"] == 5
        for k in ("scope", "policy", "timeout_ms"):
            assert rows[f"hooks.audit.{k}"]["set"] is False, rows[f"hooks.audit.{k}"]
        s = rows["hooks.audit.secret"]
        hint = r.store.secret_hint(h["id"], h["secret_ref"])
        assert s["value"] is None and s["set"] and s["hint"] == hint["sha256_6"], s
        # the secret is not written here
        st, err = r.call("POST", "hooks/plan", {"set": {"hooks.audit.secret": "x" * 40}})
        assert st == 422 and err["error"]["type"] == "locked", err
        st, err = r.call("POST", "hooks/plan", {"set": {"hooks.nope.mode": "on"}})
        assert st == 422 and err["error"]["type"] == "unknown_key", err
    finally:
        r.close()
    return "a hook's switches, the http hook's secret write-only; unset where nothing is held"


# ---------------------------------------------------------------------------- 2 apply and restore

def test_apply_version_restore():
    r = Rig("apply")
    try:
        r.hook(name="Audit log", id="audit")
        assert [h["id"] for h in r.runner.hooks()] == ["audit"]
        sets = {"hooks.audit.mode": "shadow", "hooks.audit.priority": -20,
                "hooks.audit.scope": {"models": ["big-*"], "users": ["guest*"]},
                "hooks.audit.policy": "closed", "hooks.audit.timeout_ms": 300}
        p = r.plan(sets)
        assert p["parts"][0]["restart"] == "none" and p["grant_keys"] == [] and \
            p["confirm_grant"] is None
        assert sorted(c["key"] for c in p["parts"][0]["changes"]) == sorted(sets)
        res = r.apply(sets, plan=p)
        assert res["state"] == "applied", res
        h = r.store.get("audit")
        assert (h["mode"], h["priority"], h["scope"], h["policy"], h["timeout_ms"]) == \
            ("shadow", -20, {"models": ["big-*"], "users": ["guest*"]}, "closed", 300), h
        seen = r.runner.hooks()
        assert seen and seen[0]["mode"] == "shadow", seen
        cs = p["change_set"]
        upd = [a for a in r.audit_rows() if a["object"] == "hook" and a["action"] == "update"]
        assert upd and upd[-1]["change_set"] == cs, upd
        part = res["parts"][0]
        assert part["version_before"] and part["version_after"]
        st, vers = r.call("GET", "hooks/versions")
        v = ok(vers, "versions")["versions"]
        assert vers["current"] == part["version_after"] and len(v) == 2, vers
        assert v[0]["values"]["hooks.audit.mode"] == "shadow"
        assert "hooks.audit.secret" not in v[0]["values"]
        # disabled: the runner leaves it out at once
        r.apply({"hooks.audit.enabled": False})
        assert r.runner.hooks() == []
        # unset goes back to the stage's default; a key that has no unset is refused on it
        r.apply(unsets=["hooks.audit.policy"])
        assert r.store.get("audit")["policy"] is None
        p2 = r.plan(unsets=["hooks.audit.policy"])
        assert not p2["changed"], p2                                # unset already: no change
        err = r.plan(unsets=["hooks.audit.mode"], status=422)["error"]
        assert err["type"] == "bad_value" and err["key"] == "hooks.audit.mode", err
        for k, v in (("hooks.audit.mode", "loud"), ("hooks.audit.priority", "high"),
                     ("hooks.audit.scope", {"colours": ["red"]}),
                     ("hooks.audit.timeout_ms", 999999), ("hooks.audit.enabled", "yes")):
            err = r.plan({k: v}, status=422)["error"]
            assert err["type"] == "bad_value" and err["key"] == k and err["scope"] == "hooks", \
                (k, err)
        # the restore of the version before the first change
        st, rp = r.call("POST", "hooks/restore", {"version": part["version_before"]})
        assert st == 200 and ok(rp, "plan")["changed"], rp
        st, rr = r.call("POST", "hooks/restore", {"version": part["version_before"],
                                                  "confirm": rp["confirm"]})
        assert st == 200 and ok(rr, "result")["state"] == "applied", rr
        h = r.store.get("audit")
        assert (h["mode"], h["priority"], h["scope"], h["policy"], h["timeout_ms"],
                h["enabled"]) == ("on", 100, {}, None, None, True), h
        assert [x["id"] for x in r.runner.hooks()] == ["audit"]
    finally:
        r.close()
    return "applied through the registry and seen by the runner; versions; restore; refusals"


# ---------------------------------------------------------------------------- 3 the full payload

def test_full_payload_grant():
    r = Rig("full")
    try:
        r.hook(name="Near", id="near")
        r.hook(name="Away", id="away", endpoint=AWAY_URL)
        r.hook(name="Here", id="here", kind="builtin", target="noop")
        # no pairing's rights: refused on the key
        err = r.plan({"hooks.near.payload": "full"}, status=403)["error"]
        assert err["type"] == "trust_required" and err["key"] == "hooks.near.payload", err
        assert "pairing" in err["message"]
        assert r.plan({"hooks.near.payload": "redacted"})["changed"]   # no grant: no rights asked
        # on the LAN: one confirm, the plan warns
        p = r.plan({"hooks.near.payload": "full"}, trust=True)
        assert p["grant_keys"] == [] and p["confirm_grant"] is None, p
        assert any("192.168.1.20" in w and "full payload" in w for w in p["parts"][0]["warnings"])
        res = r.apply({"hooks.near.payload": "full"}, trust=True, plan=p)
        g = r.store.get("near")["grant"]
        assert res["state"] == "applied" and g["lan"] and g["destination"] == "192.168.1.20" \
            and g["by"] == "admin" and g["confirmed_by"] is None, (res, g)
        # an in-process builtin is on this controller
        p = r.plan({"hooks.here.payload": "full"}, trust=True)
        assert p["grant_keys"] == [] and any("this controller" in w
                                             for w in p["parts"][0]["warnings"]), p
        # outside the LAN: the second confirm
        p = r.plan({"hooks.away.payload": "full"}, trust=True)
        assert p["grant_keys"] == ["hooks/controller:hooks.away.payload"], p
        assert p["parts"][0]["grant_keys"] == ["hooks.away.payload"] and p["confirm_grant"]
        assert any("outside the LAN" in w for w in p["parts"][0]["warnings"])
        err = r.apply({"hooks.away.payload": "full"}, trust=True, plan=p, status=428)["error"]
        assert err["type"] == "grant_confirm_required" and ok(err["plan"], "plan"), err
        err = r.apply({"hooks.away.payload": "full"}, trust=True, plan=p, status=409,
                      confirm_grant="0" * 32)["error"]
        assert err["type"] == "stale_plan", err
        # without pairing's rights at the apply: refused there too
        err = r.apply({"hooks.away.payload": "full"}, plan=p, status=403,
                      confirm_grant=p["confirm_grant"])["error"]
        assert err["type"] == "trust_required", err
        assert r.store.get("away")["payload"] == "meta"
        # the hook changed after the plan: the plan is stale
        r.store.update("away", {"name": "Away, renamed"}, who="t")
        err = r.apply({"hooks.away.payload": "full"}, trust=True, plan=p, status=409,
                      confirm_grant=p["confirm_grant"])["error"]
        assert err["type"] == "stale_plan", err
        p = r.plan({"hooks.away.payload": "full"}, trust=True)
        res = r.apply({"hooks.away.payload": "full"}, trust=True, plan=p,
                      confirm_grant=p["confirm_grant"])
        g = r.store.get("away")["grant"]
        assert res["state"] == "applied" and not g["lan"] and g["destination"] == "203.0.113.7" \
            and g["confirmed_by"] == "admin" and g["confirmed_at"], (res, g)
        grants = [a for a in r.audit_rows() if a["object"] == "hook" and a["object_id"] == "away"
                  and a["action"] == "update"]
        assert json.loads(grants[-1]["detail"])["confirmed"] is True, grants[-1]
        # full already: another key of the hook asks for no grant
        p = r.plan({"hooks.away.priority": 7, "hooks.away.payload": "full"})
        assert p["grant_keys"] == [] and [c["key"] for c in p["parts"][0]["changes"]] == \
            ["hooks.away.priority"], p
        # back to meta drops the grant; restoring the full version grants again, with both asks
        before = r.apply({"hooks.away.payload": "meta"})["parts"][0]["version_before"]
        assert r.store.get("away")["grant"] is None
        st, err = r.call("POST", "hooks/restore", {"version": before})
        assert st == 403 and err["error"]["type"] == "trust_required", err
        st, rp = r.call("POST", "hooks/restore", {"version": before}, trust=True)
        assert st == 200 and rp["grant_keys"] == ["hooks/controller:hooks.away.payload"], rp
        st, err = r.call("POST", "hooks/restore", {"version": before, "confirm": rp["confirm"]},
                         trust=True)
        assert st == 428 and err["error"]["type"] == "grant_confirm_required", err
        st, rr = r.call("POST", "hooks/restore", {"version": before, "confirm": rp["confirm"],
                                                  "confirm_grant": rp["confirm_grant"]},
                        trust=True)
        assert st == 200 and rr["state"] == "applied", rr
        assert r.store.get("away")["grant"]["confirmed_by"] == "admin"
    finally:
        r.close()
    return "pairing's rights for full; the LAN with one confirm, outside it with two"


# ---------------------------------------------------------------------------- 4 no secret

def test_no_secret_anywhere():
    r = Rig("secret")
    try:
        _, secret = r.hook(name="Audit log", id="audit")
        assert secret
        r.apply({"hooks.audit.mode": "shadow"})
        for path in ("hooks/schema", "hooks", "hooks/versions", "audit", ""):
            r.call("GET", path)
        rows = r.audit_rows()
        blob = json.dumps(r.answers, default=str) + json.dumps(rows, default=str) + \
            "\n".join(r.logs)
        assert secret not in blob
        vrows = r.cfg.settings_versions("hooks", "controller")
        assert secret not in json.dumps(vrows, default=str)
    finally:
        r.close()
    return f"{len(r.answers)} answers, the audit, the versions and the log: no hook secret"


if __name__ == "__main__":
    from scriptmode import run
    run(globals())
