"""SEC-18 (QA SEC-19): plugin signing and trust levels: the trusted publisher keys
(proxy/plugintrust.py), the registry's trust decisions (proxy/pluginstore.py), the plugin host's
refusals (proxy/pluginhost.py), the routes (proxy/pluginapi.py) and tools/plugin_sign.py.

  Gherkin and the acceptance criteria:
  1  Tampered package (the F3 gate): a plugin whose files changed after install is refused at
     enable and load, and by the plugin host at its start, with a finding that names the change
     (plugin_tampered); so is a plugin.sig.json changed, added or removed after the install; a
     source changed after signing is refused at install; each refusal is audited
                                                               test_tampered_package_refused
  2  A signature by an untrusted key is not trusted (it counts as none); a trusted one shows as
     signed with its key's name; a key limited to other plugins or levels does not trust it
                                                               test_signature_trusted_or_not
  3  Trusted keys: added and removed only with the fingerprint typed back (428 with the
     fingerprint, 422 a different one); a key trusted twice, a bad key or limit refused; audited
                                                               test_trusted_keys_fingerprint_confirm
  4  Host level needs a grant: an unsigned host plugin does not run until the admin types its
     id; a plugin signed by a key trusted for host is granted on that signature without it; a
     grant on a signature ends when the key is no longer trusted (the running plugin stops); the
     settings' trust key grants host only on a trusted signature
                                                               test_host_level_needs_grant
  5  Engine level: a grant per engine host takes a trusted signature AND the typed confirmation;
     recorded, still refused at runtime ('not available yet'); revoked per engine
                                                               test_engine_level_grant
  6  Update asks for more: an upgrade to a higher level waits for a new confirm and the old
     version keeps running; with the plugin's id typed it goes ahead and is granted; a host grant
     on a signature does not carry to an unsigned version without it; new capabilities start
     ungranted and are listed                                  test_update_asks_for_more
  7  Downgrade to an older version of a signed plugin takes allow_downgrade
                                                               test_downgrade_needs_confirm
  8  The policy "require signatures for sandboxed" (off by default): unsigned sandboxed plugins
     refused at install, enable and load while it is on; a trusted signature passes
                                                               test_policy_require_signature
  9  The routes end to end through a running controller: /v1/fleet/plugin-trust and
     /v1/fleet/plugins/{id}/trust, every answer and refusal against contracts/plugin-v1, pairing's
     rights for every write (403 trust_required from another machine with no admin password)
                                                               test_trust_over_http
 10  tools/plugin_sign.py: keygen (0600, never overwritten), pubkey, sign, verify (and the
     signer's fingerprint); the private key never in its output
                                                               test_sign_tool

Run: python tests/test_plugin_trust.py
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from plugin_host_util import Rig, kitchen, wait  # noqa: E402
from plugin_util import Reg, bumped, example, folder  # noqa: E402

from proxy import pluginapi  # noqa: E402
from proxy import pluginmanifest as PM  # noqa: E402
from proxy.pluginstore import PluginError  # noqa: E402
from tools import plugin_sign  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tools", "plugin_sign.py")


def keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    k = Ed25519PrivateKey.generate()
    pub, fp = plugin_sign._public(k)
    return k, pub, fp


def signed(d: str, key) -> str:
    plugin_sign.sign_with(key, d)
    return d


def refused(fn, status: int, code: str) -> PluginError:
    try:
        fn()
    except PluginError as exc:
        assert (exc.status, exc.code) == (status, code), (exc.status, exc.code, exc.message)
        return exc
    raise AssertionError(f"not refused: wanted {status} {code}")


def audits(reg, action: str, pid: str | None = None) -> list[dict]:
    out = []
    for a in reg.audit():
        if a["action"] == action and (pid is None or a["object_id"] == pid):
            a = dict(a, detail=json.loads(a["detail"]) if a["detail"] else None)
            out.append(a)
    return out


def refusals(reg, pid: str | None = None) -> list[dict]:
    """The registry's trust and integrity refusals (the host's capability refusals aside)."""
    return [a for a in audits(reg, "refused", pid) if a["detail"] and "at" in a["detail"]]


def trust_key(store, pub: str, fp: str, name: str = "Acme plugins", **limits) -> dict:
    return store.keys.add(pub, name, limits.get("plugins"), limits.get("levels"),
                          confirm=fp, who="admin")


# ---------------------------------------------------------------------------- 1
def test_tampered_package_refused():
    key, pub, fp = keypair()
    r = Rig("tamper")
    src = signed(kitchen(), key)
    plain = folder()
    try:
        trust_key(r.store, pub, fp)
        pid = r.add(src, grant=["prompts:meta"])
        st = r.running(pid)
        assert st["state"] == "running"
        p = r.store.get(pid)
        assert p["sig_file"] and p["signature"]["verified"], p["signature"]
        # a file of the installed tree changes: verify, enable and load refuse, the host too
        with open(os.path.join(p["path"], "server.py"), "a") as f:
            f.write("\n# changed under it\n")
        v = r.store.verify(pid, record=False)
        assert v["integrity"] == "mismatch" and "changed" in v["reason"], v
        refused(lambda: r.store.load(pid), 409, "integrity")
        r.host.restart_plugin(pid)
        assert wait(lambda: r.host.status(pid)["state"] == "refused", 15), r.host.status(pid)
        probs = [x for x in r.host.problems() if x["plugin"] == pid]
        assert probs and probs[0]["code"] == "plugin_tampered", probs
        assert "changed" in probs[0]["detail"] and "install it again" in probs[0]["detail"]
        r.store.set_enabled(pid, False, "admin")
        refused(lambda: r.store.set_enabled(pid, True, "admin"), 409, "integrity")
        got = refusals(r.reg, pid)
        assert {a["detail"]["at"] for a in got} >= {"load", "enable"}, got
        assert all(a["detail"]["code"] == "integrity" for a in got), got
        assert any(a["detail"] and a["detail"].get("integrity") == "mismatch"
                   for a in audits(r.reg, "verify", pid))
        r.host.forget(pid)
        r.store.remove(pid, "admin")
        # only plugin.sig.json changes after the install: the tree's digest leaves it out
        pid = r.add(src, enable=False)
        p = r.store.get(pid)
        sig_path = os.path.join(p["path"], PM.SIGNATURE)
        with open(sig_path) as f:
            doc = json.load(f)
        with open(sig_path, "w") as f:
            json.dump(doc, f)                                     # same content, other bytes
        v = r.store.verify(pid)
        assert v["integrity"] == "mismatch" and PM.SIGNATURE in v["reason"], v
        refused(lambda: r.store.set_enabled(pid, True, "admin"), 409, "integrity")
        os.remove(sig_path)
        assert PM.SIGNATURE in r.store.verify(pid)["reason"], "a signature removed"
        r.store.remove(pid, "admin")
        # a signature added to an unsigned plugin after its install
        qid = r.reg.install(plain)[0]["id"]
        shutil.copyfile(os.path.join(src, PM.SIGNATURE),
                        os.path.join(r.store.get(qid)["path"], PM.SIGNATURE))
        assert r.store.verify(qid)["integrity"] == "mismatch"
        r.store.remove(qid, "admin")
        # a source changed after signing is refused at install, and audited
        with open(os.path.join(src, "server.py"), "a") as f:
            f.write("# after signing\n")
        exc = refused(lambda: r.reg.install(src), 422, "plugin_refused")
        assert exc.extra["errors"][0]["code"] == "signature_invalid", exc.extra
        assert any(a["detail"]["at"] == "install" and a["detail"]["code"] == "signature_invalid"
                   for a in refusals(r.reg))
    finally:
        r.close()
        shutil.rmtree(src, ignore_errors=True)
        shutil.rmtree(plain, ignore_errors=True)
    return "a changed tree or signature refused at enable, load and start, with a finding"


# ---------------------------------------------------------------------------- 2
def test_signature_trusted_or_not():
    key, pub, fp = keypair()
    reg = Reg("trusted")
    src = signed(folder(), key)
    try:
        store = reg.store
        ins = store.inspect({"type": "folder", "path": src})
        sig = ins["signature"]
        assert ins["ok"] and sig["verified"] and sig["trusted"] is False, sig
        assert sig["key_name"] is None and sig["fingerprint"] == fp
        p, _ = reg.install(src)
        tv = store.trust_view(p["id"])
        assert tv["signature"]["trusted"] is False and "does not trust" in tv["signed"], tv
        assert tv["trusted_for"] == [] and tv["runs"] is True, "sandboxed runs unsigned"
        assert pluginapi.view(store, store.get(p["id"]))["signature"]["trusted"] is False
        trust_key(store, pub, fp)
        sig = store.inspect({"type": "folder", "path": src})["signature"]
        assert sig["trusted"] is True and sig["key_name"] == "Acme plugins", sig
        tv = store.trust_view(p["id"])
        assert tv["trusted_for"] == ["sandboxed", "host", "engine"], tv
        assert tv["signed"].startswith("signed by Acme plugins"), tv
        one = pluginapi.view(store, store.get(p["id"]), full=True)["signature"]
        assert one["trusted"] is True and one["key_name"] == "Acme plugins"
        # a key limited to another plugin, or to other levels, does not trust it
        store.keys.remove(fp[:16], confirm=fp, who="admin")
        trust_key(store, pub, fp, plugins=["other-tool"])
        tv = store.trust_view(p["id"])
        assert tv["trusted_for"] == [] and "other-tool only" in tv["signed"], tv
        store.keys.remove(fp[:16], confirm=fp, who="admin")
        trust_key(store, pub, fp, levels=["host"])
        tv = store.trust_view(p["id"])
        assert tv["trusted_for"] == ["host"] and "not sandboxed" in tv["signed"], tv
        # unsigned: no signature, trusted null
        u = folder("audit-hook")
        try:
            sig = store.inspect({"type": "folder", "path": u})["signature"]
            assert sig["present"] is False and sig["trusted"] is None, sig
        finally:
            shutil.rmtree(u)
    finally:
        reg.close()
        shutil.rmtree(src)
    return "unknown key: not trusted; trusted key: signed by its name; limits hold"


# ---------------------------------------------------------------------------- 3
def test_trusted_keys_fingerprint_confirm():
    _, pub, fp = keypair()
    reg = Reg("keys")
    try:
        keys = reg.store.keys
        exc = refused(lambda: keys.add(pub, "Acme", who="admin"), 428,
                      "fingerprint_confirm_required")
        assert exc.extra["fingerprint"] == fp and fp in exc.message
        other = keypair()[2]
        refused(lambda: keys.add(pub, "Acme", confirm=other, who="admin"), 422,
                "fingerprint_mismatch")
        assert keys.list() == [], "nothing trusted without the fingerprint"
        typed = "SHA256:" + ":".join(fp[i:i + 2] for i in range(0, 64, 2)).upper()
        k = keys.add(pub, " Acme ", ["hello-tool"], ["host", "sandboxed"], confirm=typed,
                     who="admin")
        assert (k["id"], k["fingerprint"], k["name"]) == (fp[:16], fp, "Acme")
        assert k["plugins"] == ["hello-tool"] and k["levels"] == ["sandboxed", "host"], k
        refused(lambda: keys.add(pub, "Again", confirm=fp, who="admin"), 409, "key_exists")
        refused(lambda: keys.add("not a key", "X", confirm=fp, who="admin"), 422, "bad_value")
        _, pub2, fp2 = keypair()
        refused(lambda: keys.add(pub2, "X", levels=["root"], confirm=fp2, who="admin"), 422,
                "bad_value")
        refused(lambda: keys.add(pub2, "X", plugins=["Bad Name"], confirm=fp2, who="admin"),
                422, "bad_value")
        refused(lambda: keys.add(pub2, "", confirm=fp2, who="admin"), 422, "bad_value")
        refused(lambda: keys.remove(fp[:16], who="admin"), 428, "fingerprint_confirm_required")
        refused(lambda: keys.remove(fp[:16], confirm=fp2, who="admin"), 422,
                "fingerprint_mismatch")
        refused(lambda: keys.remove("0" * 16, confirm=fp, who="admin"), 404, "not_found")
        gen = keys.gen
        assert keys.remove(fp[:16], confirm=fp, who="admin")["removed"] == fp[:16]
        assert keys.list() == [] and keys.gen == gen + 1
        added = audits(reg, "trust_key")
        gone = audits(reg, "distrust_key")
        assert len(added) == 1 and added[0]["detail"]["fingerprint"] == fp, added
        assert added[0]["who"] == "admin" and len(gone) == 1, gone
        assert keys.policy() == {"require_signature_sandboxed": False}, "off by default"
    finally:
        reg.close()
    return "fingerprint typed back to add and remove; limits checked; audited"


# ---------------------------------------------------------------------------- 4
def test_host_level_needs_grant():
    from proxy import pluginrt
    key, pub, fp = keypair()
    r = Rig("host-grant")
    unsigned = kitchen(name="host-plain", trust="host")
    signed_src = signed(kitchen(name="host-signed", trust="host"), key)
    try:
        r.host.set_runtime("host", pluginrt.HostRuntime())
        hid = r.add(unsigned)
        r.host.sync()
        assert wait(lambda: r.host.status(hid)["state"] == "refused", 10)
        assert "trust host" in r.host.status(hid)["last_error"]
        assert {"code": "plugin_untrusted", "plugin": hid} in [
            {k: x[k] for k in ("code", "plugin")} for x in r.host.problems()]
        tv = r.store.trust_view(hid)
        assert tv["grant_needs"] == "typed" and tv["runs"] is False, tv
        refused(lambda: r.store.grant_trust(hid, "host", who="admin"), 428,
                "typed_confirm_required")
        refused(lambda: r.store.grant_trust(hid, "host", confirm="host-plai", who="admin"), 422,
                "typed_confirm_mismatch")
        refused(lambda: r.store.write(hid, trust="host", confirmed={"trust"}, who="admin"), 409,
                "typed_confirm_required")
        assert r.store.get(hid)["trust_granted"] == "sandboxed", "nothing granted yet"
        tv = r.store.grant_trust(hid, "host", confirm="host-plain", who="admin")
        assert tv["granted"] == "host" and tv["grant"]["basis"] == "typed" and tv["runs"], tv
        st = r.running(hid)
        assert st["runtime"] == "host"
        codes = [a["detail"]["code"] for a in refusals(r.reg, hid)]
        assert "typed_confirm_required" in codes and "typed_confirm_mismatch" in codes, codes
        grant = audits(r.reg, "trust", hid)[-1]
        assert grant["detail"] == {"level": "host", "basis": "typed", "key_id": None}, grant
        # signed by a key trusted for host: granted on the signature, no typed confirmation
        trust_key(r.store, pub, fp, levels=["host"])
        sid = r.add(signed_src)
        tv = r.store.trust_view(sid)
        assert tv["grant_needs"] == "rights" and tv["trusted_for"] == ["host"], tv
        tv = r.store.grant_trust(sid, "host", who="admin")
        assert tv["grant"]["basis"] == "signature" and tv["grant"]["key_id"] == fp[:16], tv
        r.running(sid)
        # the key is no longer trusted: a grant on its signature ends, the plugin stops
        r.store.keys.remove(fp[:16], confirm=fp, who="admin")
        r.host.sync()
        assert wait(lambda: r.host.status(sid)["state"] == "refused", 15), r.host.status(sid)
        assert "rested on a trusted signature" in r.host.status(sid)["last_error"]
        assert r.host.status(hid)["state"] == "running", "a typed grant does not rest on a key"
        # trusted again: looked at again and it runs
        trust_key(r.store, pub, fp, levels=["host"])
        r.host.sync()
        assert wait(lambda: r.host.status(sid)["state"] == "running", 20), r.host.status(sid)
        # the settings' trust key: revoke works; a grant there only on a trusted signature
        r.store.write(sid, trust="sandboxed", who="admin")
        assert r.store.get(sid)["trust_grant"] is None
        refused(lambda: r.store.write(sid, trust="host", who="admin"), 409, "confirm_needed")
        r.store.write(sid, trust="host", confirmed={"trust"}, who="admin")
        assert r.store.get(sid)["trust_grant"]["basis"] == "signature"
        # revoke through the trust route
        tv = r.store.grant_trust(hid, "sandboxed", who="admin")
        assert tv["granted"] == "sandboxed" and tv["grant"] is None and not tv["runs"]
        refused(lambda: r.store.grant_trust(hid, "engine", engine="e1", confirm=hid,
                                            who="admin"), 422, "not_requested")
    finally:
        r.close()
        shutil.rmtree(unsigned)
        shutil.rmtree(signed_src)
    return "unsigned: typed id; signed: the signature; a removed key ends its grants"


# ---------------------------------------------------------------------------- 5
def test_engine_level_grant():
    key, pub, fp = keypair()
    r = Rig("engine-grant")
    m = dict(example("hello-tool"), name="engine-tool", trust="engine")
    src = folder(manifest=m)
    try:
        eid = r.add(src, enable=False)
        refused(lambda: r.store.grant_trust(eid, "engine", engine="engine-a", confirm=eid,
                                            who="admin"), 409, "signature_required")
        r.store.remove(eid, "admin")
        signed(src, key)
        eid = r.add(src)
        trust_key(r.store, pub, fp, levels=["host"])
        exc = refused(lambda: r.store.grant_trust(eid, "engine", engine="engine-a",
                                                  confirm=eid, who="admin"), 409,
                      "signature_required")
        assert "host only" in exc.message, exc.message
        r.store.keys.remove(fp[:16], confirm=fp, who="admin")
        trust_key(r.store, pub, fp, levels=["engine"])
        assert r.store.trust_view(eid)["grant_needs"] == "typed"
        refused(lambda: r.store.grant_trust(eid, "engine", engine="engine-a", who="admin"), 428,
                "typed_confirm_required")
        refused(lambda: r.store.grant_trust(eid, "engine", confirm=eid, who="admin"), 400,
                "bad_request")
        refused(lambda: r.store.grant_trust(eid, "engine", engine="bad host!", confirm=eid,
                                            who="admin"), 400, "bad_request")
        r.store.grant_trust(eid, "engine", engine="engine-a", confirm=eid, who="admin")
        tv = r.store.grant_trust(eid, "engine", engine="engine-b", confirm=eid, who="admin")
        assert sorted(tv["engines"]) == ["engine-a", "engine-b"], tv
        assert tv["engines"]["engine-a"]["basis"] == "signature+typed"
        assert tv["runs"] is False and "not available yet" in tv["why"], tv
        assert "engine-a, engine-b" in tv["why"]
        r.host.sync()
        assert wait(lambda: r.host.status(eid)["state"] == "refused", 10)
        assert "not available yet" in r.host.status(eid)["last_error"]
        tv = r.store.grant_trust(eid, "sandboxed", engine="engine-a", who="admin")
        assert sorted(tv["engines"]) == ["engine-b"], tv
        tv = r.store.grant_trust(eid, "sandboxed", who="admin")
        assert tv["engines"] == {}
        trail = [a["detail"] for a in audits(r.reg, "trust", eid)]
        assert trail[0]["engine"] == "engine-a" and trail[-1]["revoked"] == ["engine-b"], trail
    finally:
        r.close()
        shutil.rmtree(src)
    return "engine: a trusted signature and the typed id, per host; still not run"


# ---------------------------------------------------------------------------- 6
def test_update_asks_for_more():
    from proxy import pluginrt
    key, pub, fp = keypair()
    r = Rig("update")
    v1 = kitchen(name="grower")
    v2 = kitchen(name="grower", trust="host", version="0.2.0")
    try:
        r.host.set_runtime("host", pluginrt.HostRuntime())
        pid = r.add(v1)
        st = r.running(pid)
        pid_before = st["where"]
        exc = refused(lambda: r.reg.install(v2, upgrade=True), 409, "trust_confirm_needed")
        assert "keeps running" in exc.message and exc.extra["level"] == "host", exc.extra
        p = r.store.get(pid)
        assert p["version"] == "0.1.0" and os.path.isdir(p["path"]), "the old version stays"
        r.host.sync()
        st = r.host.status(pid)
        assert st["state"] == "running" and st["where"] == pid_before, st
        assert any(a["detail"]["code"] == "trust_confirm_needed"
                   for a in refusals(r.reg, pid))
        refused(lambda: r.reg.install(v2, upgrade=True, confirm_trust="grow"), 422,
                "typed_confirm_mismatch")
        p, note = r.reg.install(v2, upgrade=True, confirm_trust="grower")
        assert note["action"] == "upgraded" and "trust" in note["new"], note
        assert p["trust_granted"] == "host" and p["trust_grant"]["basis"] == "typed", p
        st = r.running(pid)
        assert st["version"] == "0.2.0" and st["runtime"] == "host", st
        # a new capability starts ungranted and is listed
        m3 = json.load(open(os.path.join(v2, "plugin.json")))
        m3["version"] = "0.3.0"
        m3["capabilities"].append("network:example.com")
        v3 = folder(manifest=m3)
        shutil.copyfile(os.path.join(v2, "server.py"), os.path.join(v3, "server.py"))
        for sub in ("panel", "skills"):
            shutil.copytree(os.path.join(v2, sub), os.path.join(v3, sub))
        p, note = r.reg.install(v3, upgrade=True)
        assert note["new"] == ["capabilities.network:example.com"], note
        assert "network:example.com" not in p["granted"] and "trust" in note["kept"]
        # a host grant on a signature does not carry to a version without it
        trust_key(r.store, pub, fp)
        signed(v3, key)
        m3["version"] = "0.3.1"
        with open(os.path.join(v3, "plugin.json"), "w") as f:
            json.dump(m3, f)
        signed(v3, key)
        r.reg.install(v3, upgrade=True)
        r.store.grant_trust(pid, "sandboxed", who="admin")
        r.store.grant_trust(pid, "host", who="admin")
        assert r.store.get(pid)["trust_grant"]["basis"] == "signature"
        m3["version"] = "0.4.0"
        with open(os.path.join(v3, "plugin.json"), "w") as f:
            json.dump(m3, f)
        os.remove(os.path.join(v3, PM.SIGNATURE))
        exc = refused(lambda: r.reg.install(v3, upgrade=True), 409, "trust_confirm_needed")
        assert "rested on a trusted signature" in exc.message, exc.message
        assert r.store.get(pid)["version"] == "0.3.1"
        p, note = r.reg.install(v3, upgrade=True, confirm_trust="grower")
        assert p["trust_grant"]["basis"] == "typed" and p["version"] == "0.4.0", p
        shutil.rmtree(v3)
    finally:
        r.close()
        shutil.rmtree(v1)
        shutil.rmtree(v2)
    return "more trust waits for the typed id, the old version running; new capabilities ungranted"


# ---------------------------------------------------------------------------- 7
def test_downgrade_needs_confirm():
    key, pub, fp = keypair()
    reg = Reg("downgrade")
    old = signed(folder(manifest=bumped("hello-tool", "0.1.0")), key)
    new = signed(folder(manifest=bumped("hello-tool", "0.2.0")), key)
    try:
        trust_key(reg.store, pub, fp)
        reg.install(new)
        exc = refused(lambda: reg.install(old, upgrade=True), 409, "downgrade")
        assert "allow_downgrade" in exc.message
        assert reg.store.get("hello-tool")["version"] == "0.2.0"
        p, note = reg.install(old, upgrade=True, allow_downgrade=True)
        assert p["version"] == "0.1.0" and p["signature"]["verified"], p
        a = audits(reg, "upgrade", "hello-tool")[-1]["detail"]
        assert a["from"] == "0.2.0" and a["signed"] == {"key_id": fp[:16], "trusted": True}, a
    finally:
        reg.close()
        shutil.rmtree(old)
        shutil.rmtree(new)
    return "an older signed version takes allow_downgrade; the audit names the key"


# ---------------------------------------------------------------------------- 8
def test_policy_require_signature():
    key, pub, fp = keypair()
    reg = Reg("policy")
    early = folder("audit-hook")
    late = folder()
    good = signed(folder("classify-provider"), key)
    try:
        store = reg.store
        aid = reg.install(early)[0]["id"]
        store.set_enabled(aid, True, "admin")
        assert store.load(aid)["trust_state"]["runs"]
        gen = store.keys.gen
        assert store.keys.set_policy(True, "admin") == {"require_signature_sandboxed": True}
        assert store.keys.gen == gen + 1
        exc = refused(lambda: reg.install(late), 409, "signature_required")
        assert "requires a trusted signature" in exc.message and "not signed" in exc.message
        assert store.get("hello-tool") is None
        refused(lambda: store.load(aid), 409, "signature_required")
        store.set_enabled(aid, False, "admin")
        refused(lambda: store.set_enabled(aid, True, "admin"), 409, "signature_required")
        exc = refused(lambda: reg.install(good), 409, "signature_required")
        assert "does not trust" in exc.message, "signed by a key not trusted: as unsigned"
        trust_key(store, pub, fp)
        p, _ = reg.install(good)
        store.set_enabled(p["id"], True, "admin")
        assert store.load(p["id"])["trust_state"]["runs"]
        codes = [a["detail"]["code"] for a in refusals(reg)]
        assert codes.count("signature_required") >= 3, codes
        assert any(a["object_id"] == "plugins.require_signature_sandboxed"
                   for a in audits(reg, "set"))
        store.keys.set_policy(False, "admin")
        store.set_enabled(aid, True, "admin")
        assert store.load(aid)["trust_state"]["runs"]
    finally:
        reg.close()
        for d in (early, late, good):
            shutil.rmtree(d)
    return "policy on: unsigned sandboxed refused at install, enable and load"


# ---------------------------------------------------------------------------- 9
def test_trust_over_http():
    from proxy_util import Proxy
    from tools import contract_check
    pv = os.path.join(ROOT, "contracts", "plugin-v1")
    api = json.load(open(os.path.join(pv, "api.json")))
    away = {"X-Forwarded-For": "203.0.113.50"}
    key, pub, fp = keypair()
    src = signed(kitchen(name="web-host", trust="host"), key)
    plain = kitchen(name="web-plain", trust="host")

    def ok(doc, name):
        errs = contract_check.validate(doc, contract_check.load(f"{name}.schema.json", pv),
                                       root=pv)
        assert not errs, (name, errs[:5], json.dumps(doc)[:400])
        return doc

    def j(px, method, path, body=None, want=200, headers=None, schema=None):
        st, _, out = px.request(method, path, body, headers)
        doc = json.loads(out or b"{}")
        assert st == want, (method, path, st, doc)
        if st >= 400:
            ok(doc, "error")
            assert api["errors"].get(doc["error"]["type"]) == st, (doc["error"]["type"], st)
        elif schema:
            ok(doc, schema)
        return doc

    try:
        with Proxy({}) as px:
            got = j(px, "GET", "/v1/fleet/plugin-trust", schema="plugin-trust")
            assert got == {"contract_version": "1.0", "keys": [],
                           "policy": {"require_signature_sandboxed": False}}
            body = {"public_key": pub, "name": "Acme plugins", "levels": ["host"]}
            err = j(px, "POST", "/v1/fleet/plugin-trust/keys", body, 428)
            assert err["error"]["fingerprint"] == fp
            j(px, "POST", "/v1/fleet/plugin-trust/keys", dict(body, confirm_fingerprint="0" * 64),
              422)
            made = j(px, "POST", "/v1/fleet/plugin-trust/keys",
                     dict(body, confirm_fingerprint=fp), 201, schema="key")
            assert made["key"]["levels"] == ["host"] and made["key"]["id"] == fp[:16]
            j(px, "POST", "/v1/fleet/plugin-trust/keys", dict(body, confirm_fingerprint=fp), 409)
            j(px, "POST", "/v1/fleet/plugin-trust/keys", dict(body, color="red"), 400)
            for path, b in (("/v1/fleet/plugin-trust/keys", dict(body, confirm_fingerprint=fp)),
                            ("/v1/fleet/plugin-trust/policy",
                             {"require_signature_sandboxed": True}),
                            (f"/v1/fleet/plugin-trust/keys/{fp[:16]}", None)):
                st, _, out = px.request("DELETE" if b is None else "POST", path, b, away)
                assert st == 403 and json.loads(out)["error"]["type"] == "trust_required", \
                    (path, st)
            j(px, "GET", "/v1/fleet/plugin-trust", headers=away, schema="plugin-trust")
            # a signed and an unsigned host plugin, granted over HTTP
            base = "/v1/fleet/plugins"
            for d in (src, plain):
                j(px, "POST", base, {"source": {"type": "folder", "path": d}}, 201,
                  schema="install")
            tv = j(px, "GET", f"{base}/web-host/trust", schema="trust")["trust"]
            assert tv["grant_needs"] == "rights" and tv["signature"]["key_name"] == "Acme plugins"
            st, _, out = px.request("POST", f"{base}/web-host/trust", {"level": "host"}, away)
            assert st == 403 and json.loads(out)["error"]["type"] == "trust_required", \
                "a grant takes pairing's rights"
            tv = j(px, "POST", f"{base}/web-host/trust", {"level": "host"},
                   schema="trust")["trust"]
            assert tv["grant"]["basis"] == "signature"
            j(px, "POST", f"{base}/web-plain/trust", {"level": "host"}, 428)
            j(px, "POST", f"{base}/web-plain/trust", {"level": "host", "confirm": "web"}, 422)
            j(px, "POST", f"{base}/web-plain/trust", {"level": "root"}, 400)
            j(px, "POST", f"{base}/web-plain/trust", {"level": "host", "x": 1}, 400)
            tv = j(px, "POST", f"{base}/web-plain/trust", {"level": "host",
                                                           "confirm": "web-plain"},
                   schema="trust")["trust"]
            assert tv["grant"]["basis"] == "typed"
            one = j(px, "GET", f"{base}/web-plain", schema="plugin-one")
            assert one["plugin"]["trust"] == {"requested": "host", "granted": "host"}
            v = j(px, "POST", f"{base}/web-host/verify", {}, schema="verify")
            assert v["integrity"] == "ok" and v["reason"] is None
            # an upgrade asking more over HTTP
            m = json.load(open(os.path.join(plain, "plugin.json")))
            m.update(version="0.2.0", trust="engine")
            with open(os.path.join(plain, "plugin.json"), "w") as f:
                json.dump(m, f)
            up = {"source": {"type": "folder", "path": plain}, "upgrade": True}
            err = j(px, "POST", base, up, 409)
            assert err["error"]["type"] == "trust_confirm_needed"
            assert err["error"]["level"] == "engine" and err["error"]["installed"] == "0.1.0"
            j(px, "POST", base, dict(up, confirm_trust="web-plain"), 200, schema="install")
            pol = j(px, "POST", "/v1/fleet/plugin-trust/policy",
                    {"require_signature_sandboxed": True}, schema="policy")
            assert pol["policy"] == {"require_signature_sandboxed": True}
            j(px, "DELETE", f"/v1/fleet/plugin-trust/keys/{fp[:16]}", {}, 428)
            gone = j(px, "DELETE", f"/v1/fleet/plugin-trust/keys/{fp[:16]}",
                     {"confirm_fingerprint": fp}, schema="key-removed")
            assert gone["removed"] == fp[:16]
            j(px, "GET", "/v1/fleet/plugin-trust/nothing", want=404)
            tv = j(px, "GET", f"{base}/web-host/trust", schema="trust")["trust"]
            assert tv["runs"] is False and "rested on a trusted signature" in tv["why"], tv
    finally:
        shutil.rmtree(src)
        shutil.rmtree(plain)
    return "keys, policy and grants over HTTP; every answer fits the contract; rights hold"


# ---------------------------------------------------------------------------- 10
def test_sign_tool():
    d = tempfile.mkdtemp(prefix="plugin-sign-")
    src = folder()
    try:
        keyf = os.path.join(d, "acme.pem")
        env = dict(os.environ, SIGN_PASS="correct horse")

        def tool(*args, want=0):
            p = subprocess.run([sys.executable, TOOL, *args], capture_output=True, text=True,
                               timeout=60, env=env)
            assert p.returncode == want, (args, p.returncode, p.stdout, p.stderr)
            return p.stdout + p.stderr

        out = tool("keygen", keyf, "--passphrase-env", "SIGN_PASS")
        assert stat.S_IMODE(os.stat(keyf).st_mode) == 0o600
        with open(keyf) as f:
            pem = f.read()
        assert "ENCRYPTED" in pem
        body = [ln for ln in pem.splitlines() if ln and not ln.startswith("-----")]
        pub = out.split("public_key ")[1].split()[0]
        fp = out.split("fingerprint sha256:")[1].split()[0]
        assert len(fp) == 64
        tool("keygen", keyf, want=2)                               # never overwritten
        assert "ENCRYPTED" in open(keyf).read()
        outs = [out, tool("pubkey", keyf, "--passphrase-env", "SIGN_PASS")]
        assert f"public_key {pub}" in outs[-1]
        outs.append(tool("pubkey", keyf, want=2))                 # no passphrase
        outs.append(tool("sign", "--key", keyf, "--passphrase-env", "SIGN_PASS", src))
        assert "signed hello-tool 0.1.0" in outs[-1]
        outs.append(tool("verify", src, "--fingerprint", "sha256:" + fp))
        assert "ok hello-tool 0.1.0 signed by key " + fp[:16] in outs[-1]
        outs.append(tool("verify", src, "--fingerprint", "0" * 64, want=1))
        with open(os.path.join(src, "server.py"), "a") as f:
            f.write("# after signing\n")
        outs.append(tool("verify", src, want=1))
        assert "signature_invalid" in outs[-1] and "another tree" in outs[-1]
        unsigned = folder()
        outs.append(tool("verify", unsigned, want=1))
        assert "not signed" in outs[-1]
        bad = folder(manifest=dict(example("hello-tool"), name="Bad Name"))
        outs.append(tool("sign", "--key", keyf, "--passphrase-env", "SIGN_PASS", bad, want=1))
        assert not os.path.exists(os.path.join(bad, PM.SIGNATURE))
        for x in (unsigned, bad):
            shutil.rmtree(x)
        for o in outs:
            assert "PRIVATE" not in o and not any(b in o for b in body), "the private key leaked"
    finally:
        shutil.rmtree(d)
        shutil.rmtree(src)
    return "keygen 0600 once, sign, verify by fingerprint; the private key never printed"


if __name__ == "__main__":
    from scriptmode import run
    run(globals())
