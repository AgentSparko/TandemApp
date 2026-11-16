"""SRV-857 (QA SRV-858): plugin-v1's manifest and the checks an install makes (proxy/pluginmanifest.py),
held to contracts/plugin-v1.

  Gherkin and the acceptance criteria:
  1  The plugin.json schema is published in contracts/plugin-v1 with a validator: the file is the
     module's schema, every example and both engine-stats manifests pass it, tools/plugin_check.py
     says ok for each and names the refusal for a bad one     test_schema_published_with_a_validator
  2  Manifest refusals: every fixture in contracts/plugin-v1/fixtures/refusals is refused with its
     code on its field (and its facts), and nothing else of the fixture's base is refused
                                                              test_every_refusal_fixture
  3  Missing annotation: Given a plugin whose tool lacks one of the four annotations, Then the
     install's checks refuse it naming the tool and the annotation, for each of the four; a tool
     with all four as booleans passes                         test_annotation_refusal
  4  Extra properties allowed: Given a tool schema that allows extra properties (missing, true, a
     schema, patternProperties, a nested object, an array's object items), Then it is refused;
     additionalProperties false at every level passes         test_extra_properties_refusal
  5  Incompatible version: Given a plugin that needs a newer TandemApp, Then it is refused with the
     needed version; a hooks API or plugin API of a newer minor or another major likewise; the
     range grammar (>=, <, ==, !=, ~=, wildcards, pre-releases) holds
                                                              test_compatibility
  6  The tree's sha256: stable for the same files, changed by any byte, a mode bit, a rename, a
     new file; litter (.git, __pycache__, .pyc, .DS_Store, ._*) and plugin.sig.json are not
     part of it; a symlink or a control character refuses the tree
                                                              test_tree_digest
  7  A signature is verified when present (SEC-18's statement: the plugin, its version, the
     tree's and plugin.json's sha256): a good one holds (its key id and fingerprint recorded,
     trusted null here: the registry's), a tampered tree, another tree's digest or plugin.json,
     another version, a wrong key, F1's older format or a broken file refuse the install
     (signature_invalid)                                      test_signature
  8  The vLLM and SGLang stats plugins have a manifest of kind engine-stats that passes, and their
     packages still import as before                          test_engine_stats_manifests
  9  The capabilities: the grammar of capabilities.json, which grants take the second confirm
     (prompts:full, engine:control, network outside the LAN)  test_capability_grammar_and_confirm

CPU only. Run: python tests/test_plugin_manifest.py
"""

from __future__ import annotations

import base64
import copy
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from proxy import pluginmanifest as PM  # noqa: E402
from proxy import providerstore  # noqa: E402
from proxy import schemaform  # noqa: E402

CON = os.path.join(ROOT, "contracts", "plugin-v1")
EX = os.path.join(CON, "examples")
HELLO = os.path.join(EX, "hello-tool")
VERSIONS = {"tandemapp": "0.15.0", "hooks_api": "1.0"}


def tree(files: dict, manifest=None, raw: str | None = None) -> str:
    d = tempfile.mkdtemp(prefix="plugin-manifest-")
    if raw is not None:
        files = dict(files, **{"plugin.json": raw})
    elif manifest is not None:
        files = dict(files, **{"plugin.json": json.dumps(manifest, indent=1)})
    for rel, body in files.items():
        p = os.path.join(d, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w" if isinstance(body, str) else "wb") as f:
            f.write(body)
    return d


def hello() -> dict:
    with open(os.path.join(HELLO, "plugin.json")) as f:
        return json.load(f)


def check(m: dict, files=None, **kw) -> dict:
    d = tree(files or {"server.py": "pass\n"}, m)
    try:
        return PM.inspect(d, **{**VERSIONS, **kw})
    finally:
        shutil.rmtree(d)


def codes(rep: dict) -> list[tuple[str, str | None]]:
    return [(e["code"], e["field"]) for e in rep["errors"]]


# ---------------------------------------------------------------------------- 1
def test_schema_published_with_a_validator():
    with open(os.path.join(CON, "manifest.schema.json")) as f:
        assert json.load(f) == PM.export(), "manifest.schema.json differs from MANIFEST_SCHEMA"
    assert PM.QUESTIONS == tuple(providerstore.QUESTIONS), "the questions drifted"
    from proxy import hookstages
    assert set(PM.MANIFEST_SCHEMA["properties"]["contributes"]["properties"]["hooks"]["items"]
               ["properties"]["role"]["enum"]) == set(hookstages.ROLES)
    plugins = [os.path.join(EX, n) for n in sorted(os.listdir(EX))] + \
        [os.path.join(ROOT, "plugins", "vllm"), os.path.join(ROOT, "plugins", "sglang")]
    for d in plugins:
        rep = PM.inspect(d, **VERSIONS)
        assert rep["ok"] and not rep["errors"], (d, rep["errors"])
        assert len(rep["sha256"]) == 64 and rep["signature"]["present"] is False
    tool = os.path.join(ROOT, "tools", "plugin_check.py")
    p = subprocess.run([sys.executable, tool, HELLO], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0 and "ok hello-tool 0.1.0" in p.stdout, (p.stdout, p.stderr)
    m = hello()
    m["contributes"]["tools"][0]["annotations"].pop("openWorldHint")
    d = tree({}, m)
    try:
        p = subprocess.run([sys.executable, tool, "--json", d], capture_output=True, text=True,
                           timeout=60)
        assert p.returncode == 1, p
        got = json.loads(p.stdout)
        assert got["ok"] is False and got["errors"][0]["code"] == "annotation_missing", got
    finally:
        shutil.rmtree(d)
    with open(os.path.join(CON, "README.md")) as f:
        readme = f.read()
    with open(os.path.join(CON, "api.json")) as f:
        api = json.load(f)
    for name in os.listdir(CON):
        if name.endswith(".json"):
            assert f"`{name}`" in readme, f"the README does not name {name}"
            with open(os.path.join(CON, name)) as f:
                json.load(f)
    for r in api["routes"]:
        for k in ("schema", "body"):
            if r.get(k):
                assert os.path.isfile(os.path.join(CON, r[k])), r
    return f"{len(plugins)} manifests pass; the tool names a refusal"


# ---------------------------------------------------------------------------- 2
def test_every_refusal_fixture():
    files = sorted(glob.glob(os.path.join(CON, "fixtures", "refusals", "*.json")))
    assert len(files) >= 40, len(files)
    seen = set()
    for path in files:
        with open(path) as f:
            fx = json.load(f)
        extra = fx.get("files") or {}
        if "raw" in fx:
            d = tree(extra, raw=fx["raw"])
        elif "manifest" in fx:
            d = tree(dict({"server.py": "pass\n"}, **extra), fx["manifest"])
        else:
            d = tree(extra)
        try:
            rep = PM.inspect(d, **VERSIONS)
        finally:
            shutil.rmtree(d)
        name = os.path.basename(path)
        assert not rep["ok"], (name, "passed")
        hit = [e for e in rep["errors"] if e["code"] == fx["code"] and e["field"] == fx["field"]]
        assert hit, (name, fx["code"], fx["field"], codes(rep))
        for k, v in (fx.get("facts") or {}).items():
            assert hit[0].get(k) == v, (name, k, hit[0])
        assert all(isinstance(e["message"], str) and e["message"] for e in rep["errors"])
        seen.add(fx["code"])
    assert {"unknown_field", "missing_field", "bad_value", "kind_rules", "annotation_missing",
            "extra_properties", "tool_schema", "duplicate_name", "stage_unknown",
            "role_not_allowed", "capability_missing", "secret_undeclared", "trust_mismatch",
            "path_outside", "path_missing", "settings_schema", "incompatible_tandemapp",
            "incompatible_hooks_api", "incompatible_plugin_api", "manifest_missing",
            "manifest_invalid_json"} <= seen, seen
    with open(os.path.join(CON, "api.json")) as f:
        api = json.load(f)
    assert seen <= set(api["checks"]), sorted(seen - set(api["checks"]))
    return f"{len(files)} fixtures, {len(seen)} codes, each on its field"


# ---------------------------------------------------------------------------- 3
def test_annotation_refusal():
    for a in PM.ANNOTATIONS:
        m = hello()
        m["contributes"]["tools"][0]["annotations"].pop(a)
        rep = check(m)
        assert codes(rep) == [("annotation_missing",
                               f"contributes.tools[0].annotations.{a}")], codes(rep)
        e = rep["errors"][0]
        assert e["tool"] == "greet" and e["annotation"] == a
        assert "greet" in e["message"] and a in e["message"], e
    m = hello()
    m["contributes"]["tools"].append(dict(copy.deepcopy(m["contributes"]["tools"][0]),
                                          name="wave", annotations={}))
    rep = check(m)
    assert sorted(e["annotation"] for e in rep["errors"]) == sorted(PM.ANNOTATIONS)
    assert {e["tool"] for e in rep["errors"]} == {"wave"}
    assert check(hello())["ok"]
    return "each of the four refused by name; all four pass"


# ---------------------------------------------------------------------------- 4
def test_extra_properties_refusal():
    def with_schema(s):
        m = hello()
        m["contributes"]["tools"][0]["inputSchema"] = s
        return check(m)
    strict = {"type": "object", "properties": {"q": {"type": "string"}},
              "additionalProperties": False}
    bad = {
        "missing": ({"type": "object", "properties": {}}, "contributes.tools[0].inputSchema"),
        "true": (dict(strict, additionalProperties=True), "contributes.tools[0].inputSchema"),
        "a schema": (dict(strict, additionalProperties={"type": "string"}),
                     "contributes.tools[0].inputSchema"),
        "patternProperties": (dict(strict, patternProperties={"^x": {"type": "string"}}),
                              "contributes.tools[0].inputSchema.patternProperties"),
        "nested": ({"type": "object", "additionalProperties": False, "properties": {
            "o": {"type": "object", "properties": {"a": {"type": "integer"}}}}},
            "contributes.tools[0].inputSchema.properties.o"),
        "array items": ({"type": "object", "additionalProperties": False, "properties": {
            "l": {"type": "array", "items": {"type": "object", "properties": {}}}}},
            "contributes.tools[0].inputSchema.properties.l.items"),
        "via $ref": ({"type": "object", "additionalProperties": False, "$defs": {
            "p": {"type": "object", "properties": {}}}, "properties": {"p": {"$ref": "#/$defs/p"}}},
            "contributes.tools[0].inputSchema.properties.p"),
    }
    for why, (s, field) in bad.items():
        rep = with_schema(s)
        assert ("extra_properties", field) in codes(rep), (why, codes(rep))
        assert all(e["code"] == "extra_properties" for e in rep["errors"]), (why, codes(rep))
    good = {"type": "object", "additionalProperties": False, "properties": {
        "o": {"type": "object", "additionalProperties": False, "properties": {
            "l": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                             "properties": {"x": {"type": "number"}}}}}}}}
    assert with_schema(good)["ok"]
    return f"{len(bad)} ways to allow extra input refused; strict at every level passes"


# ---------------------------------------------------------------------------- 5
def test_compatibility():
    m = hello()
    m["tandemapp"] = ">=0.17, <0.19"
    rep = check(m)
    assert codes(rep) == [("incompatible_tandemapp", "tandemapp")], codes(rep)
    e = rep["errors"][0]
    assert e["needed"] == ">=0.17, <0.19" and e["have"] == "0.15.0"
    assert ">=0.17, <0.19" in e["message"] and "0.15.0" in e["message"], e
    assert check(m, tandemapp="0.17.2")["ok"]
    assert codes(check(m, tandemapp="0.19.0")) == [("incompatible_tandemapp", "tandemapp")]
    for ha, ok in (("1.0", True), ("1.1", False), ("2.0", False)):
        m = hello()
        m["hooks_api"] = ha
        assert check(m)["ok"] is ok, ha
        if not ok:
            assert codes(check(m)) == [("incompatible_hooks_api", "hooks_api")]
    m = hello()
    m["hooks_api"] = "1.1"
    assert check(m, hooks_api="1.2")["ok"]
    m = hello()
    m["plugin_api"] = "1.1"
    assert codes(check(m)) == [("incompatible_plugin_api", "plugin_api")]
    rng = [("0.15.0", ">=0.15", True), ("0.15.0", ">0.15", False),
           ("0.16.0-rc1", "<0.16", False), ("0.16.0-rc1", ">=0.16.0-rc1, <0.16.1", True),
           ("0.16.0-rc2", "<0.16.0-rc3", True), ("0.16.0", ">=0.16.0-rc1", True),
           ("0.15.3", "==0.15.*", True), ("0.16.0", "==0.15.*", False),
           ("0.15.3", "!=0.15.3", False), ("0.15.9", "~=0.15.2", True),
           ("0.16.0", "~=0.15.2", False), ("0.99.0", "~=0.15", True), ("1.0.0", "~=0.15", False),
           ("0.4.1-test", "<0.4.1", False), ("0.4.1-test", "<=0.4.1", True),
           ("0.4.1-test", "<0.4.2", True), ("0.15.0", ">=0.14 , <1", True)]
    for v, spec, want in rng:
        assert PM.in_range(v, spec) is want, (v, spec)
    for bad in ("newest", ">= ", "0.15", ">=0.15;<1", "~=1", ">=0.1.*"):
        try:
            PM.in_range("0.15.0", bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} parsed")
    return "needed versions named; range grammar holds"


# ---------------------------------------------------------------------------- 6
def test_tree_digest():
    files = {"plugin.json": "{}", "server.py": "print(1)\n", "lib/a.txt": "a"}
    d = tree(files)
    try:
        base = PM.tree_digest(d)
        assert base == PM.tree_digest(d)
        d2 = tree(files)
        try:
            assert PM.tree_digest(d2) == base, "the same files, another folder"
        finally:
            shutil.rmtree(d2)
        for junk in (".git/HEAD", "__pycache__/x.cpython-312.pyc", "lib/y.pyc", ".DS_Store",
                     "lib/._a.txt", PM.SIGNATURE):
            p = os.path.join(d, *junk.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write("x")
        assert PM.tree_digest(d) == base, "litter is not part of the tree"
        changes = []
        with open(os.path.join(d, "lib", "a.txt"), "w") as f:
            f.write("b")
        changes.append(PM.tree_digest(d))
        with open(os.path.join(d, "lib", "a.txt"), "w") as f:
            f.write("a")
        assert PM.tree_digest(d) == base
        os.chmod(os.path.join(d, "server.py"), 0o755)
        changes.append(PM.tree_digest(d))
        os.chmod(os.path.join(d, "server.py"), 0o644)
        os.rename(os.path.join(d, "lib", "a.txt"), os.path.join(d, "lib", "b.txt"))
        changes.append(PM.tree_digest(d))
        os.rename(os.path.join(d, "lib", "b.txt"), os.path.join(d, "lib", "a.txt"))
        with open(os.path.join(d, "new.txt"), "w") as f:
            f.write("")
        changes.append(PM.tree_digest(d))
        os.remove(os.path.join(d, "new.txt"))
        assert PM.tree_digest(d) == base
        assert len(set(changes)) == 4 and base not in changes
        os.symlink("/etc/hostname", os.path.join(d, "lib", "link"))
        rep = PM.inspect(d, **VERSIONS)
        assert codes(rep) == [("symlink", None)], codes(rep)
        try:
            PM.tree_digest(d)
            raise AssertionError("a symlink hashed")
        except PM.TreeError as exc:
            assert exc.code == "symlink"
        os.remove(os.path.join(d, "lib", "link"))
        with open(os.path.join(d, "bad\nname"), "w") as f:
            f.write("")
        assert codes(PM.inspect(d, **VERSIONS)) == [("bad_name", None)]
    finally:
        shutil.rmtree(d)
    return "stable, sensitive to bytes, mode, names and files; litter ignored"


# ---------------------------------------------------------------------------- 7
def _sign(d: str, key=None, digest: str | None = None, **over) -> str:
    """Sign the tree at `d` as tools/plugin_sign.py does; `digest` and `over` forge a part."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = key or Ed25519PrivateKey.generate()
    dg = digest or PM.tree_digest(d)
    pub = key.public_key().public_bytes(serialization.Encoding.Raw,
                                        serialization.PublicFormat.Raw)
    with open(os.path.join(d, "plugin.json")) as f:
        m = json.load(f)
    facts = {"plugin": m["name"], "version": m["version"], "tree_sha256": dg,
             "manifest_sha256": PM.file_sha256(os.path.join(d, "plugin.json"))}
    facts.update(over)
    doc = {"format": PM.SIG_FORMAT, "algorithm": "ed25519",
           "public_key": base64.b64encode(pub).decode(), **facts,
           "signature": base64.b64encode(key.sign(PM.signature_statement(**facts))).decode()}
    with open(os.path.join(d, PM.SIGNATURE), "w") as f:
        json.dump(doc, f)
    return dg


def test_signature():
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError:
        return "not run here: the cryptography package is missing"
    d = tree({"server.py": "pass\n"}, hello())
    try:
        dg = _sign(d)
        rep = PM.inspect(d, **VERSIONS)
        sig = rep["signature"]
        assert rep["ok"] and sig["present"] and sig["verified"] and sig["error"] is None, rep
        assert sig["trusted"] is None and len(sig["key_id"]) == 16 and rep["sha256"] == dg
        assert sig["fingerprint"].startswith(sig["key_id"]) and len(sig["fingerprint"]) == 64
        assert (sig["plugin"], sig["version"]) == ("hello-tool", "0.1.0")
        # the statement: five lines, the format, plugin, version, tree and manifest
        msg = PM.signature_statement("hello-tool", "0.1.0", dg, "m" * 64).decode()
        assert msg == (f"tandemapp-plugin-sig-1\nplugin hello-tool\nversion 0.1.0\ntree {dg}\n"
                       f"manifest {'m' * 64}\n"), msg
        with open(os.path.join(d, "server.py"), "a") as f:
            f.write("# changed after signing\n")
        rep = PM.inspect(d, **VERSIONS)
        assert codes(rep) == [("signature_invalid", PM.SIGNATURE)], codes(rep)
        assert "another tree" in rep["errors"][0]["message"]
        _sign(d, digest="0" * 64)                                    # a digest of another tree
        assert codes(PM.inspect(d, **VERSIONS)) == [("signature_invalid", PM.SIGNATURE)]
        _sign(d, manifest_sha256="0" * 64)                          # another plugin.json
        rep = PM.inspect(d, **VERSIONS)
        assert codes(rep) == [("signature_invalid", PM.SIGNATURE)]
        assert "another plugin.json" in rep["errors"][0]["message"]
        _sign(d, version="0.0.9")                                   # names another version
        rep = PM.inspect(d, **VERSIONS)
        assert codes(rep) == [("signature_invalid", PM.SIGNATURE)], codes(rep)
        assert "0.0.9" in rep["errors"][0]["message"]
        dg = PM.tree_digest(d)
        _sign(d)
        with open(os.path.join(d, PM.SIGNATURE)) as f:
            doc = json.load(f)
        other = Ed25519PrivateKey.generate()
        doc["signature"] = base64.b64encode(other.sign(PM.signature_statement(
            doc["plugin"], doc["version"], doc["tree_sha256"], doc["manifest_sha256"]))).decode()
        with open(os.path.join(d, PM.SIGNATURE), "w") as f:
            json.dump(doc, f)
        rep = PM.inspect(d, **VERSIONS)
        assert codes(rep) == [("signature_invalid", PM.SIGNATURE)]
        assert "does not hold" in rep["errors"][0]["message"]
        # F1's format (signed, no format) is refused with what to do
        with open(os.path.join(d, PM.SIGNATURE), "w") as f:
            json.dump({"algorithm": "ed25519", "public_key": doc["public_key"], "signed": dg,
                       "signature": doc["signature"]}, f)
        rep = PM.inspect(d, **VERSIONS)
        assert codes(rep) == [("signature_invalid", PM.SIGNATURE)]
        assert "older signature format" in rep["errors"][0]["message"]
        with open(os.path.join(d, PM.SIGNATURE), "w") as f:
            f.write("{broken")
        assert codes(PM.inspect(d, **VERSIONS)) == [("signature_invalid", PM.SIGNATURE)]
        os.remove(os.path.join(d, PM.SIGNATURE))
        os.mkdir(os.path.join(d, PM.SIGNATURE))                    # not a regular file
        rep = PM.inspect(d, **VERSIONS)
        assert ("signature_invalid", PM.SIGNATURE) in codes(rep), codes(rep)
    finally:
        shutil.rmtree(d)
    return "verified when present; tampered, foreign, wrong-key, renamed, old and broken refused"


# ---------------------------------------------------------------------------- 8
def test_engine_stats_manifests():
    for eng, pkg in (("vllm", "tandem_vllm_plugin"), ("sglang", "tandem_sglang_plugin")):
        d = os.path.join(ROOT, "plugins", eng)
        rep = PM.inspect(d, **VERSIONS)
        assert rep["ok"], (eng, rep["errors"])
        m = rep["manifest"]
        assert m["kind"] == "engine-stats" and m["engine_stats"]["engine"] == eng
        assert "entry" not in m and not rep["capabilities"]
        with open(os.path.join(d, "pyproject.toml")) as f:
            pyproject = f.read()
        for ep in m["engine_stats"]["entry_points"]:
            assert f'[project.entry-points."{ep}"]' in pyproject, (eng, ep)
        assert m["engine_stats"]["package"] in pyproject
        # the package still imports as the engine would load it (its own stand-ins absent: a
        # missing engine is the package's own quiet case)
        p = subprocess.run([sys.executable, "-I", "-c",
                            f"import sys; sys.path.insert(0, {d!r}); import {pkg}; "
                            f"print({pkg}.__version__)"], capture_output=True, text=True,
                           timeout=60)
        assert p.returncode == 0 and p.stdout.strip() == m["version"], (eng, p.stdout, p.stderr)
    return "both engine-stats manifests pass; versions agree with the packages"


# ---------------------------------------------------------------------------- 9
def test_capability_grammar_and_confirm():
    import re
    good = ["prompts:meta", "prompts:redacted", "prompts:full", "settings:read", "engine:control",
            "secrets:API_KEY", "network:api.example.com", "network:192.168.1.30:8720",
            "network:localhost"]
    bad = ["prompts:all", "network:*", "network:Example.com", "network:[::1]", "secrets:lower",
           "disk:write", "network:", "network:host:0", "engine:*"]
    for c in good:
        assert re.match(PM.CAPABILITY_RE, c), c
    for c in bad:
        assert not re.match(PM.CAPABILITY_RE, c), c
    lan = {"192.168.1.30": True, "api.example.com": False, "localhost": True,
           "203.0.113.7": False}
    is_lan = lan.__getitem__
    assert PM.needs_confirm("prompts:full", is_lan) and PM.needs_confirm("engine:control", is_lan)
    assert not PM.needs_confirm("prompts:meta", is_lan)
    assert not PM.needs_confirm("prompts:redacted", is_lan)
    assert not PM.needs_confirm("network:192.168.1.30:8720", is_lan)
    assert PM.needs_confirm("network:api.example.com", is_lan)
    assert PM.needs_confirm("network:203.0.113.7:443", is_lan)
    assert not PM.needs_confirm("secrets:API_KEY", is_lan)
    with open(os.path.join(CON, "capabilities.json")) as f:
        doc = json.load(f)
    pats = [c["pattern"] for c in doc["capabilities"]]
    assert len(pats) == 7 and doc["levels"] == list(PM.PROMPT_LEVELS)
    m = hello()
    m["contributes"]["hooks"] = [{"name": "h", "stages": ["request.finished"],
                                  "role": "observer", "payload": "redacted"}]
    m["capabilities"] = ["prompts:full"]
    assert check(m)["ok"], "a higher prompts level covers a lower payload"
    m["capabilities"] = ["prompts:meta"]
    assert codes(check(m)) == [("capability_missing", "contributes.hooks[0].payload")]
    s = schemaform.check_schema(hello()["settings"])
    assert s == [], s
    return f"{len(good)} good, {len(bad)} bad; the confirm rule holds"


if __name__ == "__main__":
    from scriptmode import run
    run(globals())
