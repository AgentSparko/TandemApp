"""plugin-v1: a plugin's manifest, its tree and the checks an install makes (SRV-857).

contracts/plugin-v1/manifest.schema.json is MANIFEST_SCHEMA below for everyone outside the
controller (a plugin's author, tools/plugin_check.py); tests/test_plugin_manifest.py holds the two
equal. docs/plugins.md says the same in words.

    inspect(root, tandemapp=..., hooks_api=...) -> report     every check, nothing executed
    tree_files(root), tree_digest(root)                        the tree and its sha256
    in_range(version, spec), api_compatible(want, have)        compatibility

A PLUGIN is a folder with `plugin.json` at its root (a wheel: at the wheel's root or in one top-level
folder). Kind `plugin` (the default) is an MCP server run as a subprocess by the plugin host (F2);
kind `engine-stats` is a package an engine loads to send its figures (plugins/vllm, plugins/sglang):
the controller only records it.

THE TREE. Every regular file under the root, but `.git` and `__pycache__` folders, `*.pyc`,
`.DS_Store` and `._*` files (litter, never part of a plugin) and `plugin.sig.json` at the root (the
signature over the rest). A symlink, a device or a name with a control character refuses the tree.
At most MAX_FILES files and MAX_TREE_BYTES bytes.

THE DIGEST. sha256 over "plugin-tree-v1\\n" and one line a file in byte order of its path:
"<sha256 of the file> <x|-> <path>\\n" (x: executable by its owner). The registry records it at
install and checks it again on load: a tree that changed since reads `mismatch` and does not load.

THE SIGNATURE (optional). `plugin.sig.json` at the root (tools/plugin_sign.py writes it):

    {format: "tandemapp-plugin-sig-1", algorithm: "ed25519", public_key (base64 of 32 bytes),
     plugin, version, tree_sha256 (the tree's digest), manifest_sha256 (sha256 of plugin.json's
     bytes), signature (base64 of 64 bytes)}

The signature is over the STATEMENT, five lines of UTF-8 each ending in "\\n":

    tandemapp-plugin-sig-1
    plugin <name>
    version <version>
    tree <the tree's digest, hex>
    manifest <sha256 of plugin.json, hex>

The tree's digest is itself the sha256 over every file's sha256 (THE DIGEST), so the statement
covers every file, the manifest and the version. When present the signature must hold for this
tree and this manifest, else the install is refused (`signature_invalid`). Which keys are trusted
is proxy/plugintrust.py's (SEC-18): this module only says whether the signature holds, and by
which key (its id: the first 16 hex digits of its fingerprint, the sha256 of the 32 bytes).

THE CHECKS (each refusal one {code, field, message} with its facts): the manifest parses and fits
MANIFEST_SCHEMA (unknown fields are refused, not ignored); the kind's own rules; every MCP tool has
all four annotations (readOnlyHint, destructiveHint, idempotentHint, openWorldHint) as booleans and an
input schema of type object that says additionalProperties false at every object level; names are
unique within each contribution; a hook's stages and role are those of hooks-v1 and its payload is
covered by a requested `prompts:` capability; a decision provider's questions are the conductor's;
paths (panels, skills, an engine hook's wheel) stay inside the tree and exist; `secrets:<name>`
names a declared secret; the settings schema is one a form can draw (proxy/schemaform.py STRICT);
`tandemapp` holds for this TandemApp, `hooks_api` and `plugin_api` for this controller's.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import stat

from proxy import schemaform

PLUGIN_API = "1.0"
MANIFEST = "plugin.json"
SIGNATURE = "plugin.sig.json"
MAX_MANIFEST_BYTES = 256 * 1024
MAX_FILES = 5000
MAX_TREE_BYTES = 64 * 1024 * 1024
SKIP_DIRS = (".git", "__pycache__")
ANNOTATIONS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
KINDS = ("plugin", "engine-stats")
TRUST = ("sandboxed", "host", "engine")
PROMPT_LEVELS = ("meta", "redacted", "full")
#: engine-ext-v1's in-process hooks an `engine_hooks` contribution may name
EXT_HOOKS = ("plan", "prompt", "at_request", "at_block", "at_tool_call", "check_answer",
             "at_finish", "extras", "follow_ups", "finish")
#: the conductor's questions a decision provider may answer (proxy/providerstore.py QUESTIONS)
QUESTIONS = ("classify", "packages", "deliverable", "memory_intent", "memory_gate", "judge")

NAME_RE = r"^[a-z0-9][a-z0-9-]{0,47}$"
PART_RE = r"^[a-z0-9][a-z0-9_-]{0,47}$"
TOOL_RE = r"^[A-Za-z0-9_-]{1,64}$"
VERSION_RE = r"^(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})(-[0-9A-Za-z.]{1,32})?$"
API_RE = r"^[1-9][0-9]{0,2}\.(0|[1-9][0-9]{0,2})$"
SECRET_RE = r"^[A-Z][A-Z0-9_]{0,63}$"
#: a DNS name or an IPv4 address (an IPv6 host goes by its name: a settings key holds no bracket)
_HOST = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
CAPABILITY_RE = (r"^(?:prompts:(?:meta|redacted|full)|settings:read|engine:control"
                 r"|secrets:[A-Z][A-Z0-9_]{0,63}|network:" + _HOST + r"(?::[1-9][0-9]{0,4})?)$")
RANGE_RE = r"^\s*(?:(?:>=|<=|==|!=|~=|>|<)\s*[0-9][0-9A-Za-z.*-]{0,40}\s*(?:,\s*|$))+$"
#: the capabilities whose grant takes the second confirm (besides `network:` outside the LAN)
CONFIRM_ALWAYS = ("prompts:full", "engine:control")


def _str(n: int, pattern: str | None = None, min_len: int = 1) -> dict:
    out = {"type": "string", "minLength": min_len, "maxLength": n}
    if pattern:
        out["pattern"] = pattern
    return out


def _obj(props: dict, required=(), **kw) -> dict:
    out = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        out["required"] = list(required)
    out.update(kw)
    return out


def _list(item: dict, n: int, **kw) -> dict:
    return {"type": "array", "items": item, "maxItems": n, **kw}


MANIFEST_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "plugin-v1/manifest",
    "title": "plugin.json",
    "description": "A TandemApp plugin's manifest, plugin-v1 (docs/plugins.md). Every field not "
                   "listed here is refused.",
    "type": "object",
    "additionalProperties": False,
    "required": ["plugin_api", "name", "version", "tandemapp"],
    "properties": {
        "$schema": _str(200),
        "plugin_api": dict(_str(8, API_RE), description="the plugin-v1 version this manifest is "
                                                       "written for, MAJOR.MINOR"),
        "name": dict(_str(48, NAME_RE), description="the plugin's id: lowercase letters, digits "
                                                   "and dashes; one plugin of a name"),
        "version": dict(_str(64, VERSION_RE), description="MAJOR.MINOR.PATCH[-pre]"),
        "kind": {"enum": list(KINDS), "default": "plugin",
                 "description": "plugin: an MCP server the plugin host runs; engine-stats: a "
                                "package an engine loads, recorded only"},
        "title": _str(80),
        "description": _str(500),
        "author": _str(120),
        "license": _str(64),
        "homepage": _str(300, r"^https://"),
        "tandemapp": dict(_str(120, RANGE_RE), description="the TandemApp versions it runs on, "
                                                          "e.g. \">=0.15, <0.17\""),
        "hooks_api": dict(_str(8, API_RE), description="the hooks-v1 version it speaks "
                                                      "(kind plugin)"),
        "entry": _obj({"command": _list(_str(512), 32, minItems=1),
                       "env": {"type": "object", "maxProperties": 32,
                               "propertyNames": _str(64, r"^[A-Z_][A-Z0-9_]{0,63}$"),
                               "additionalProperties": _str(1024, min_len=0)}},
                      ["command"], description="the command the plugin host starts (argv, never "
                                               "a shell), from the manifest's folder"),
        "trust": {"enum": list(TRUST), "default": "sandboxed"},
        "contributes": _obj({
            "hooks": _list(_obj({
                "name": _str(48, PART_RE),
                "stages": _list(_str(64), 16, minItems=1, uniqueItems=True),
                "role": {"enum": ["transform", "guard", "decider", "observer"]},
                "payload": {"enum": list(PROMPT_LEVELS), "default": "meta"},
                "description": _str(500)}, ["name", "stages", "role"]), 32),
            "tools": _list(_obj({
                "name": _str(64, TOOL_RE),
                "title": _str(120),
                "description": _str(2048),
                "inputSchema": {"type": "object"},
                "outputSchema": {"type": "object"},
                "annotations": _obj({"title": _str(120),
                                     **{a: {"type": "boolean"} for a in ANNOTATIONS}})},
                ["name", "description", "inputSchema", "annotations"]), 64),
            "decision_providers": _list(_obj({
                "name": _str(48, PART_RE),
                "questions": _list({"enum": list(QUESTIONS)}, len(QUESTIONS), minItems=1,
                                   uniqueItems=True),
                "description": _str(500)}, ["name", "questions"]), 16),
            "backends": _list(_obj({
                "name": _str(48, PART_RE),
                "description": _str(500),
                "models": _list(_str(128), 64)}, ["name"]), 16),
            "panels": _list(_obj({
                "name": _str(48, PART_RE),
                "title": _str(80),
                "path": _str(256, r"\.html?$")}, ["name", "title", "path"]), 16),
            "skills": _list(_str(256), 16, uniqueItems=True),
            "engine_hooks": _list(_obj({
                "name": _str(48, PART_RE),
                "wheel": _str(256, r"\.whl$"),
                "hooks": _list({"enum": list(EXT_HOOKS)}, len(EXT_HOOKS), minItems=1,
                               uniqueItems=True)}, ["name", "wheel", "hooks"]), 4),
        }),
        "settings": {"type": "object",
                     "description": "a JSON schema of type object: the plugin's settings, drawn as "
                                    "a form (proxy/schemaform.py STRICT)"},
        "secrets": _list(_obj({"name": _str(64, SECRET_RE), "description": _str(300)},
                              ["name"]), 32),
        "capabilities": _list(_str(300, CAPABILITY_RE), 32, uniqueItems=True),
        "engine_stats": _obj({
            "engine": {"enum": ["vllm", "sglang"]},
            "package": _str(128),
            "entry_points": _list(_str(128), 8, minItems=1, uniqueItems=True),
            "install": _str(300),
            "env": _list(_str(64, r"^[A-Z_][A-Z0-9_]{0,63}$"), 16)},
            ["engine", "package", "entry_points"], description="kind engine-stats: what the engine "
                                                               "loads"),
        "git": _obj({"submodules": {"const": "ignore"}, "lfs": {"const": "ignore"}},
                    description="a git source: leave its submodules or LFS pointer files as they "
                                "are instead of refusing the install"),
    },
}


class TreeError(ValueError):
    def __init__(self, code: str, message: str, field: str | None = None):
        super().__init__(message)
        self.code, self.message, self.field = code, message, field


def export() -> dict:
    return json.loads(json.dumps(MANIFEST_SCHEMA))


# ===================================================================================== versions
_V = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.]+))?$")


def parse_version(s: str) -> tuple:
    """A comparable key of "X[.Y[.Z]][-pre]": a pre-release sorts before its release."""
    m = _V.match((s or "").strip())
    if not m:
        raise ValueError(f"not a version: {s!r}")
    nums = tuple(int(x) if x is not None else 0 for x in m.group(1, 2, 3))
    pre = m.group(4)
    if pre is None:
        return nums + ((1,),)
    parts = tuple((0, int(p), "") if p.isdigit() else (0, -1, p) for p in pre.split("."))
    return nums + ((0,) + parts,)


def _clause_holds(have: tuple, op: str, spec: str) -> bool:
    if spec.endswith(".*"):
        if op not in ("==", "!="):
            raise ValueError(f"a wildcard goes with == or != only: {op}{spec}")
        stem = [int(x) for x in spec[:-2].split(".")]
        same = list(have[:len(stem)]) == stem
        return same if op == "==" else not same
    want = parse_version(spec)
    if op == "~=":
        parts = spec.split("-")[0].split(".")
        if len(parts) < 2:
            raise ValueError(f"~= needs at least MAJOR.MINOR: {spec}")
        upper = [int(x) for x in parts[:-1]]
        upper[-1] += 1
        return have >= want and list(have[:len(upper)]) < upper
    if op == "<" and want[3] == (1,) and have[3] != (1,) and have[:3] == want[:3]:
        return False                  # <0.16 leaves out 0.16's pre-releases too (PEP 440)
    return {">=": have >= want, "<=": have <= want, ">": have > want, "<": have < want,
            "==": have == want, "!=": have != want}[op]


def parse_range(spec: str) -> list[tuple[str, str]]:
    if not isinstance(spec, str) or not re.match(RANGE_RE, spec):
        raise ValueError(f"not a version range: {spec!r} (e.g. \">=0.15, <0.17\")")
    out = []
    for c in spec.split(","):
        c = c.strip()
        if not c:
            continue
        m = re.match(r"^(>=|<=|==|!=|~=|>|<)\s*(.+)$", c)
        if not m:
            raise ValueError(f"not a version clause: {c!r}")
        op, v = m.group(1), m.group(2).strip()
        if not v.endswith(".*"):
            parse_version(v)
        out.append((op, v))
    return out


def in_range(version: str, spec: str) -> bool:
    have = parse_version(version)
    return all(_clause_holds(have, op, v) for op, v in parse_range(spec))


def api_compatible(want: str, have: str) -> bool:
    """A manifest's MAJOR.MINOR against this controller's: the same major, a minor not newer."""
    wm, wn = (int(x) for x in want.split("."))
    hm, hn = (int(x) for x in have.split("."))
    return wm == hm and wn <= hn


# ===================================================================================== the tree
def _litter(name: str) -> bool:
    return name == ".DS_Store" or name.startswith("._") or name.endswith(".pyc")


def _bad_name(name: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in name)


def tree_files(root: str) -> list[tuple[str, str, bool, int]]:
    """[(path from the root with /, absolute path, executable, size)] in byte order of the path.
    Raises TreeError for a symlink, a special file, a bad name or a tree over the limits."""
    if not os.path.isdir(root) or os.path.islink(root):
        raise TreeError("not_a_folder", f"{root} is not a folder")
    out, total = [], 0
    for base, dirs, names in os.walk(root, followlinks=False):
        keep = []
        for d in sorted(dirs):
            p = os.path.join(base, d)
            if _bad_name(d):
                raise TreeError("bad_name", f"a folder name with a control character under "
                                            f"{os.path.relpath(base, root)}")
            if os.path.islink(p):
                raise TreeError("symlink", f"{os.path.relpath(p, root)} is a symlink: a plugin's "
                                           f"tree holds files and folders only")
            if d in SKIP_DIRS:
                continue
            keep.append(d)
        dirs[:] = keep
        for n in names:
            p = os.path.join(base, n)
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            if _bad_name(n):
                raise TreeError("bad_name", "a file name with a control character")
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                raise TreeError("symlink", f"{rel} is a symlink: a plugin's tree holds files and "
                                           f"folders only")
            if not stat.S_ISREG(st.st_mode):
                raise TreeError("special_file", f"{rel} is not a regular file")
            if _litter(n) or rel == SIGNATURE:
                continue
            total += st.st_size
            out.append((rel, p, bool(st.st_mode & stat.S_IXUSR), st.st_size))
            if len(out) > MAX_FILES:
                raise TreeError("too_large", f"more than {MAX_FILES} files")
            if total > MAX_TREE_BYTES:
                raise TreeError("too_large", f"more than {MAX_TREE_BYTES // (1 << 20)} MiB")
    out.sort(key=lambda x: x[0].encode())
    return out


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_digest(root: str, files=None) -> str:
    files = tree_files(root) if files is None else files
    h = hashlib.sha256(b"plugin-tree-v1\n")
    for rel, p, x, _ in files:
        h.update(f"{file_sha256(p)} {'x' if x else '-'} {rel}\n".encode())
    return h.hexdigest()


SIG_FORMAT = "tandemapp-plugin-sig-1"
SIG_FIELDS = ("format", "algorithm", "public_key", "plugin", "version", "tree_sha256",
              "manifest_sha256", "signature")
SIG_MAX = 16 * 1024


def signature_statement(plugin: str, version: str, tree_sha256: str,
                        manifest_sha256: str) -> bytes:
    """What a plugin's signature is over (THE SIGNATURE)."""
    return (f"{SIG_FORMAT}\nplugin {plugin}\nversion {version}\ntree {tree_sha256}\n"
            f"manifest {manifest_sha256}\n").encode()


def manifest_path(root: str, manifest_dir: str = "") -> str:
    return os.path.join(root, *[p for p in manifest_dir.split("/") if p], MANIFEST)


def no_signature() -> dict:
    return {"present": False, "verified": False, "algorithm": None, "key_id": None,
            "fingerprint": None, "public_key": None, "plugin": None, "version": None,
            "trusted": None, "key_name": None, "error": None}


def signature_file_sha256(root: str) -> str | None:
    """The sha256 of plugin.sig.json as it is now, or None without one: the registry records it,
    since the tree's digest leaves the signature out."""
    path = os.path.join(root, SIGNATURE)
    if not os.path.isfile(path) or os.path.islink(path):
        return None
    return file_sha256(path)


def check_signature(root: str, digest: str, manifest_dir: str = "") -> dict:
    """{present, verified, algorithm, key_id, fingerprint, public_key, plugin, version, trusted,
    key_name, error}: `error` set when present and it does not hold. `trusted` and `key_name` are
    the registry's (proxy/plugintrust.py), null here."""
    path = os.path.join(root, SIGNATURE)
    out = no_signature()
    if os.path.lexists(path) and (os.path.islink(path) or not os.path.isfile(path)):
        out.update(present=True, error=f"{SIGNATURE} is not a regular file")
        return out
    if not os.path.isfile(path):
        return out
    out["present"] = True
    try:
        if os.path.getsize(path) > SIG_MAX:
            raise ValueError(f"over {SIG_MAX // 1024} KiB")
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        if not isinstance(doc, dict):
            raise ValueError("a JSON object")
        if "signed" in doc and "format" not in doc:
            raise ValueError("an older signature format: sign it again with "
                             "tools/plugin_sign.py")
        if set(doc) != set(SIG_FIELDS):
            raise ValueError(f"fields: {', '.join(SIG_FIELDS)}")
        if doc["format"] != SIG_FORMAT:
            raise ValueError(f"format: {SIG_FORMAT}")
        out["algorithm"] = doc["algorithm"]
        if doc["algorithm"] != "ed25519":
            raise ValueError("algorithm: ed25519")
        for k in ("public_key", "plugin", "version", "tree_sha256", "manifest_sha256",
                  "signature"):
            if not isinstance(doc[k], str):
                raise ValueError(f"{k}: a string")
        pub = base64.b64decode(doc["public_key"], validate=True)
        sig = base64.b64decode(doc["signature"], validate=True)
        if len(pub) != 32 or len(sig) != 64:
            raise ValueError("public_key is 32 bytes, signature 64")
        fp = hashlib.sha256(pub).hexdigest()
        out.update(key_id=fp[:16], fingerprint=fp, public_key=doc["public_key"],
                   plugin=doc["plugin"], version=doc["version"])
        if not re.match(NAME_RE, doc["plugin"]) or not re.match(VERSION_RE, doc["version"]):
            raise ValueError("plugin and version: a plugin's name and version")
        if doc["tree_sha256"] != digest:
            raise ValueError("it signs another tree: a file changed, was added or was removed "
                             "after signing (tree_sha256 differs from this tree's digest)")
        mpath = manifest_path(root, manifest_dir)
        msha = file_sha256(mpath) if os.path.isfile(mpath) else None
        if doc["manifest_sha256"] != msha:
            raise ValueError("it signs another plugin.json (manifest_sha256 differs)")
        try:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        except ImportError:                                        # pragma: no cover
            raise ValueError("the cryptography package is missing")
        try:
            Ed25519PublicKey.from_public_bytes(pub).verify(sig, signature_statement(
                doc["plugin"], doc["version"], digest, msha))
        except InvalidSignature:
            raise ValueError("the signature does not hold for this tree")
        out["verified"] = True
    except (ValueError, TypeError, KeyError, OSError, binascii.Error) as exc:
        out["error"] = str(exc) or type(exc).__name__
    return out


# ===================================================================================== the checks
def _e(code: str, field: str | None, message: str, **facts) -> dict:
    out = {"code": code, "field": field, "message": message}
    out.update(facts)
    return out


def _field_of(path: str) -> str:
    p = path[1:] if path.startswith("$") else path
    return p[1:] if p.startswith(".") else p


def _schema_errors(doc: dict) -> list[dict]:
    out = []
    for msg in schemaform.validate(doc, MANIFEST_SCHEMA, limit=50):
        path, _, why = msg.partition(": ")
        field = _field_of(path)
        m = re.match(r"^(\S+) is required$", why)
        if m:
            f = f"{field}.{m.group(1)}" if field else m.group(1)
            out.append(_e("missing_field", f, f"{f} is required"))
        elif why == "not a known field":
            out.append(_e("unknown_field", field, f"{field}: not a field of plugin-v1 (unknown "
                                                  f"fields are refused, not ignored)"))
        else:
            out.append(_e("bad_value", field or None, f"{field or 'plugin.json'}: {why}"))
    return out


def _extra_props(node, path: str, root: dict, out: list, depth: int = 0) -> None:
    """Every object in a tool's input schema says additionalProperties false."""
    if depth > 16 or not isinstance(node, dict):
        return
    try:
        node, _ = schemaform._resolve(node, root)
    except schemaform.SchemaError:
        return
    t = node.get("type")
    tl = [t] if isinstance(t, str) else (t if isinstance(t, list) else [])
    if "object" in tl or "properties" in node:
        if node.get("additionalProperties", True) is not False:
            out.append(path)
        if node.get("patternProperties"):
            out.append(path + ".patternProperties")
        for k, sub in (node.get("properties") or {}).items():
            _extra_props(sub, f"{path}.properties.{k}", root, out, depth + 1)
    if isinstance(node.get("items"), dict):
        _extra_props(node["items"], f"{path}.items", root, out, depth + 1)
    for key in ("anyOf", "oneOf", "allOf"):
        for i, sub in enumerate(node.get(key) or []):
            _extra_props(sub, f"{path}.{key}[{i}]", root, out, depth + 1)
    for name, sub in (node.get("$defs") or {}).items():
        _extra_props(sub, f"{path}.$defs.{name}", root, out, depth + 1)


def _inside(root: str, rel: str) -> str | None:
    """The absolute path of `rel` under `root`, or None when it leaves the root."""
    if not isinstance(rel, str) or rel.startswith("/") or "\\" in rel:
        return None
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return os.path.join(root, *parts)


def _semantic(m: dict, root: str, files: set) -> tuple[list[dict], list[str]]:
    errs: list[dict] = []
    warns: list[str] = []
    kind = m.get("kind", "plugin")
    contributes = m.get("contributes") or {}
    caps = list(m.get("capabilities") or [])
    if kind == "plugin":
        for f in ("entry", "hooks_api"):
            if f not in m:
                errs.append(_e("missing_field", f, f"{f} is required for kind plugin"))
        if "engine_stats" in m:
            errs.append(_e("kind_rules", "engine_stats", "engine_stats is for kind engine-stats"))
    else:
        if "engine_stats" not in m:
            errs.append(_e("missing_field", "engine_stats", "engine_stats is required for kind "
                                                            "engine-stats"))
        for f in ("entry", "settings", "secrets"):
            if f in m:
                errs.append(_e("kind_rules", f, f"{f}: an engine-stats plugin runs in the engine; "
                                                f"the plugin host never starts it"))
        if any(contributes.get(k) for k in contributes):
            errs.append(_e("kind_rules", "contributes", "an engine-stats plugin contributes "
                                                        "nothing to the controller"))
        if caps:
            errs.append(_e("kind_rules", "capabilities", "an engine-stats plugin takes no "
                                                         "capability"))
    # names unique within each contribution
    for part in ("hooks", "tools", "decision_providers", "backends", "panels", "engine_hooks"):
        seen = set()
        for i, item in enumerate(contributes.get(part) or []):
            n = item.get("name")
            if n in seen:
                errs.append(_e("duplicate_name", f"contributes.{part}[{i}].name",
                               f"contributes.{part}: two entries named {n!r}", name=n))
            seen.add(n)
    # the MCP tool contract: four annotations, wrong input refused
    for i, t in enumerate(contributes.get("tools") or []):
        name = t.get("name")
        ann = t.get("annotations") if isinstance(t.get("annotations"), dict) else {}
        for a in ANNOTATIONS:
            if a not in ann:
                errs.append(_e("annotation_missing", f"contributes.tools[{i}].annotations.{a}",
                               f"tool {name!r}: annotation {a} is missing (each tool states all "
                               f"four: {', '.join(ANNOTATIONS)})", tool=name, annotation=a))
        sch = t.get("inputSchema")
        if isinstance(sch, dict):
            if sch.get("type") != "object":
                errs.append(_e("tool_schema", f"contributes.tools[{i}].inputSchema.type",
                               f"tool {name!r}: its input schema is of type object", tool=name))
            bad: list[str] = []
            _extra_props(sch, "", sch, bad)
            for b in bad:
                f = f"contributes.tools[{i}].inputSchema{b}"
                errs.append(_e("extra_properties", f, f"tool {name!r}: {('inputSchema' + b)} "
                                                      f"allows extra properties; say "
                                                      f"\"additionalProperties\": false so wrong "
                                                      f"input is refused", tool=name))
            try:
                json.dumps(sch)
            except (TypeError, ValueError):
                errs.append(_e("tool_schema", f"contributes.tools[{i}].inputSchema",
                               f"tool {name!r}: the input schema is not JSON", tool=name))
    # hooks: the stages and roles of hooks-v1, the payload covered by a capability
    from proxy import hookstages as HS
    levels = [c.split(":", 1)[1] for c in caps if c.startswith("prompts:")]
    top = max((PROMPT_LEVELS.index(x) for x in levels), default=-1)
    for i, h in enumerate(contributes.get("hooks") or []):
        for s in h.get("stages") or []:
            if s not in HS.STAGES:
                errs.append(_e("stage_unknown", f"contributes.hooks[{i}].stages",
                               f"hook {h.get('name')!r}: no stage {s!r} in hooks-v1", stage=s))
            elif h.get("role") not in HS.STAGES[s]["roles"]:
                errs.append(_e("role_not_allowed", f"contributes.hooks[{i}].role",
                               f"hook {h.get('name')!r}: a {h.get('role')} cannot run at {s} "
                               f"(it takes {', '.join(HS.STAGES[s]['roles'])})", stage=s))
        want = h.get("payload", "meta")
        if PROMPT_LEVELS.index(want) > top:
            errs.append(_e("capability_missing", f"contributes.hooks[{i}].payload",
                           f"hook {h.get('name')!r} sees payload {want}: request "
                           f"prompts:{want} in capabilities", capability=f"prompts:{want}"))
    if contributes.get("decision_providers") and top < 0:
        errs.append(_e("capability_missing", "contributes.decision_providers",
                       "a decision provider sees the question's input: request a prompts: "
                       "capability (meta, redacted or full)", capability="prompts:meta"))
    # engine hooks run in the engine process
    if contributes.get("engine_hooks") and m.get("trust") != "engine":
        errs.append(_e("trust_mismatch", "trust", "engine_hooks run inside the engine: the "
                                                  "manifest says trust engine"))
    # paths inside the tree
    for i, p in enumerate(contributes.get("panels") or []):
        _path_check(errs, root, files, p.get("path"), f"contributes.panels[{i}].path", "file")
    for i, p in enumerate(contributes.get("engine_hooks") or []):
        _path_check(errs, root, files, p.get("wheel"), f"contributes.engine_hooks[{i}].wheel",
                    "file")
    for i, d in enumerate(contributes.get("skills") or []):
        full = _path_check(errs, root, files, d, f"contributes.skills[{i}]", "dir")
        if full:
            has = any(f.startswith(_norm(d) + "/") and f.endswith("/SKILL.md") for f in files)
            if not has:
                errs.append(_e("path_missing", f"contributes.skills[{i}]",
                               f"{d}: no <skill>/SKILL.md in this folder"))
    # secrets and capabilities agree
    names = [s.get("name") for s in (m.get("secrets") or []) if isinstance(s, dict)]
    if len(set(names)) != len(names):
        errs.append(_e("duplicate_name", "secrets", "two secrets of the same name"))
    for c in caps:
        if c.startswith("secrets:") and c.split(":", 1)[1] not in names:
            errs.append(_e("secret_undeclared", "capabilities",
                           f"{c}: no secret {c.split(':', 1)[1]} in secrets", capability=c))
    for n in names:
        if f"secrets:{n}" not in caps:
            warns.append(f"secret {n} is stored but never reaches the plugin: request "
                         f"secrets:{n} to pass it")
    if len([c for c in caps if c.startswith("prompts:")]) > 1:
        warns.append("more than one prompts: capability: the highest one counts")
    # the settings schema
    if "settings" in m:
        for why in schemaform.check_schema(m["settings"]):
            path = why.partition(": ")[0].replace(" default", "")
            errs.append(_e("settings_schema", "settings" + path.lstrip("$"), f"settings: {why}"))
    # an entry's command is argv: the first word a program name or a path inside the tree
    cmd = ((m.get("entry") or {}).get("command") or [None])[0]
    if isinstance(cmd, str) and ("/" in cmd and not cmd.startswith("./")):
        if cmd.startswith("/"):
            warns.append(f"entry: {cmd} is an absolute path on the host; a program name (python3) "
                         f"or a path inside the plugin (./server) travels better")
        elif _inside(root, cmd) is None:
            errs.append(_e("path_outside", "entry.command", f"entry: {cmd} leaves the plugin's "
                                                            f"folder"))
    return errs, warns


def _norm(rel: str) -> str:
    return "/".join(p for p in rel.split("/") if p not in ("", "."))


def _path_check(errs, root, files, rel, field, kind) -> str | None:
    full = _inside(root, rel) if isinstance(rel, str) else None
    if full is None:
        errs.append(_e("path_outside", field, f"{field}: {rel!r} leaves the plugin's folder"))
        return None
    n = _norm(rel)
    ok = n in files if kind == "file" else any(f.startswith(n + "/") for f in files)
    if not ok:
        errs.append(_e("path_missing", field, f"{field}: {rel} is not in the plugin"))
        return None
    return full


def requested(manifest: dict) -> list[str]:
    return list(manifest.get("capabilities") or [])


def compat(manifest: dict, *, tandemapp: str, hooks_api: str) -> list[dict]:
    """The compatibility refusals of a manifest that fits the schema."""
    errs = []
    want = manifest.get("plugin_api")
    if isinstance(want, str) and re.match(API_RE, want) and \
            not api_compatible(want, PLUGIN_API):
        errs.append(_e("incompatible_plugin_api", "plugin_api",
                       f"written for plugin-v1 {want}; this TandemApp reads plugin-v1 "
                       f"{PLUGIN_API}", needed=want, have=PLUGIN_API))
    rng = manifest.get("tandemapp")
    if isinstance(rng, str):
        try:
            ok = in_range(tandemapp, rng)
        except ValueError as exc:
            errs.append(_e("bad_value", "tandemapp", f"tandemapp: {exc}"))
        else:
            if not ok:
                errs.append(_e("incompatible_tandemapp", "tandemapp",
                               f"needs TandemApp {rng}; this is TandemApp {tandemapp}",
                               needed=rng, have=tandemapp))
    ha = manifest.get("hooks_api")
    if isinstance(ha, str) and re.match(API_RE, ha) and not api_compatible(ha, hooks_api):
        errs.append(_e("incompatible_hooks_api", "hooks_api",
                       f"needs hooks API {ha}; this TandemApp speaks hooks API {hooks_api}",
                       needed=ha, have=hooks_api))
    return errs


def this_tandemapp() -> str:
    from proxy import _version
    return _version.release() or "0.0.0"


def this_hooks_api() -> str:
    from proxy import hookstages
    return hookstages.VERSION


def inspect(root: str, *, manifest_dir: str = "", tandemapp: str | None = None,
            hooks_api: str | None = None) -> dict:
    """Every check on the tree at `root` (its manifest in `manifest_dir` under it). Reads files,
    never runs one. {ok, manifest, errors, warnings, sha256, signature, files, bytes,
    capabilities, compat: {tandemapp, hooks_api, plugin_api}}."""
    tandemapp = tandemapp or this_tandemapp()
    hooks_api = hooks_api or this_hooks_api()
    rep = {"ok": False, "manifest": None, "errors": [], "warnings": [], "sha256": None,
           "signature": None, "files": 0, "bytes": 0, "capabilities": [],
           "manifest_dir": manifest_dir,
           "compat": {"tandemapp": tandemapp, "hooks_api": hooks_api, "plugin_api": PLUGIN_API}}
    try:
        files = tree_files(root)
    except TreeError as exc:
        rep["errors"].append(_e(exc.code, exc.field, exc.message))
        return rep
    rep["files"] = len(files)
    rep["bytes"] = sum(f[3] for f in files)
    rep["sha256"] = tree_digest(root, files)
    rep["signature"] = check_signature(root, rep["sha256"], manifest_dir)
    if rep["signature"]["error"]:
        rep["errors"].append(_e("signature_invalid", SIGNATURE,
                                f"{SIGNATURE}: {rep['signature']['error']}"))
    mdir = os.path.join(root, *[p for p in manifest_dir.split("/") if p]) if manifest_dir else root
    mpath = os.path.join(mdir, MANIFEST)
    if not os.path.isfile(mpath):
        rep["errors"].append(_e("manifest_missing", None, f"no {MANIFEST} at the plugin's root"))
        return rep
    if os.path.getsize(mpath) > MAX_MANIFEST_BYTES:
        rep["errors"].append(_e("manifest_too_large", None, f"{MANIFEST} is over "
                                                            f"{MAX_MANIFEST_BYTES // 1024} KiB"))
        return rep
    try:
        with open(mpath, encoding="utf-8") as f:
            m = json.load(f)
    except (ValueError, UnicodeDecodeError) as exc:
        rep["errors"].append(_e("manifest_invalid_json", None, f"{MANIFEST} is not JSON: {exc}"))
        return rep
    if not isinstance(m, dict):
        rep["errors"].append(_e("bad_value", None, f"{MANIFEST} is a JSON object"))
        return rep
    rep["manifest"] = m
    sig = rep["signature"]
    if sig["verified"] and (sig["plugin"], sig["version"]) != (m.get("name"), m.get("version")):
        sig["verified"] = False
        sig["error"] = (f"it signs {sig['plugin']} {sig['version']}, this is {m.get('name')} "
                        f"{m.get('version')}")
        rep["errors"].append(_e("signature_invalid", SIGNATURE, f"{SIGNATURE}: {sig['error']}"))
    errs = _schema_errors(m)
    rel_files = set()
    prefix = (manifest_dir.strip("/") + "/") if manifest_dir else ""
    for rel, _, _, _ in files:
        if rel.startswith(prefix):
            rel_files.add(rel[len(prefix):])
    if not errs:
        more, warns = _semantic(m, mdir, rel_files)
        errs += more
        rep["warnings"] += warns
        errs += compat(m, tandemapp=tandemapp, hooks_api=hooks_api)
    rep["errors"] += errs
    rep["capabilities"] = requested(m) if isinstance(m.get("capabilities"), list) else []
    rep["ok"] = not rep["errors"]
    return rep


def needs_confirm(capability: str, is_lan=None) -> bool:
    """A grant of `capability` takes the second confirm: prompts:full, engine:control, and a
    network host outside the LAN (proxy/hookcall.py host_is_lan)."""
    if capability in CONFIRM_ALWAYS:
        return True
    if capability.startswith("network:"):
        host = network_host(capability)
        if is_lan is None:
            from proxy import hookcall
            is_lan = hookcall.host_is_lan
        return not is_lan(host)
    return False


def network_host(capability: str) -> str:
    rest = capability.split(":", 1)[1]
    return rest.rsplit(":", 1)[0] if re.search(r":\d+$", rest) else rest
