"""A JSON schema as a form, and a value checked against it (SRV-857, contracts/plugin-v1).

    describe(schema) -> form          the form description a page draws (FORM below)
    validate(value, schema) -> [str]  what is wrong with a value, one sentence each, by its path
    check_schema(schema) -> [str]     why a plugin's settings schema cannot be used (STRICT below)
    defaults(schema) -> dict          the defaults of an object schema's properties

One generator serves a plugin's settings now, and a hook's settings and the conductor's documents
later: dashboard/src/lib/schema-form.ts is the same generator for the page, and both are held to the
same fixtures (contracts/plugin-v1/forms/*.json: a schema and the form it gives). Standard library
only.

THE KEYWORDS read: type (one or a list; "null" in the list makes the field nullable), properties,
required, additionalProperties, items, enum, const, anyOf and oneOf (a branch that is only
{"type": "null"} makes the other nullable; otherwise the raw editor), $ref to "#/$defs/<name>" or
"#/definitions/<name>", title, description, default, examples, readOnly, writeOnly, format,
pattern, minLength, maxLength, minimum, maximum, exclusiveMinimum, exclusiveMaximum, multipleOf,
minItems, maxItems, uniqueItems, minProperties, maxProperties. `validate` also reads allOf, not,
if/then/else, propertyNames and patternProperties (the manifest's own schema uses some of them).

FORM. {form: "1.0", title, description, fields: [field], notes: [str]}. A field:

    key, path        "a.b" and ["a", "b"]; a list's item is "a[]"
    label            the title, else the property's name in words ("max_tokens" -> "Max tokens")
    description      the schema's description or null
    type             string, integer, number, boolean, object, array, null or any
    control          switch (boolean), number (integer, number), text, textarea (a string over 200
                     characters), select (enum over 4 values), choice (enum of 2 to 4), const (one
                     fixed value, shown, not edited), secret (writeOnly: set or unset, never shown),
                     group (an object: `fields`), list (an array: `item`), json (the raw editor:
                     `why` says why)
    required, nullable, read_only, secret
    has_default, default
    options          [{value, label}] for select and choice
    min, max, exclusive_min, exclusive_max, step, min_length, max_length, pattern, format,
    min_items, max_items, unique     only when the schema sets them

LIMITS. Nesting deeper than MAX_DEPTH, more than MAX_FIELDS fields or an enum of more than MAX_OPTIONS
values fall back to the raw editor (`json`) for that part; each fallback adds a note.

STRICT (check_schema, a plugin's settings): the root is an object; every object, the root's and each
nested one, says additionalProperties false (wrong input is refused, never ignored); only the
keywords above; no writeOnly (a secret is a manifest `secrets` name, sealed apart); local $ref only,
no cycle; every default holds against its own schema; at most MAX_DEPTH levels and MAX_FIELDS fields.
"""

from __future__ import annotations

import json
import math
import re

FORM_VERSION = "1.0"
MAX_DEPTH = 4
MAX_FIELDS = 200
MAX_OPTIONS = 200
CHOICE_MAX = 4
TEXTAREA_OVER = 200
#: the keywords a form reads (the subset `check_schema` allows)
FORM_KEYWORDS = frozenset((
    "$schema", "$id", "$comment", "$defs", "definitions", "$ref", "type", "properties", "required",
    "additionalProperties", "items", "enum", "const", "anyOf", "oneOf", "title", "description",
    "default", "examples", "readOnly", "writeOnly", "format", "pattern", "minLength", "maxLength",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minItems",
    "maxItems", "uniqueItems", "minProperties", "maxProperties"))
TYPES = ("string", "integer", "number", "boolean", "object", "array", "null")


class SchemaError(ValueError):
    """A schema this module cannot follow (a $ref that does not resolve, a cycle)."""


# ======================================================================================= helpers
def _words(name: str) -> str:
    s = re.sub(r"[_\-]+", " ", str(name)).strip()
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)
    return (s[:1].upper() + s[1:].lower()) if s else str(name)


def _resolve(node, root: dict, seen: tuple = ()) -> tuple[dict, tuple]:
    """`node` with its $ref followed (local only), and the refs followed on the way."""
    while isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            raise SchemaError(f"$ref {ref!r}: only a local reference (#/$defs/...) is followed")
        if ref in seen:
            raise SchemaError(f"$ref {ref!r} refers to itself")
        seen = seen + (ref,)
        target = root
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, dict) or part not in target:
                raise SchemaError(f"$ref {ref!r} does not resolve")
            target = target[part]
        rest = {k: v for k, v in node.items() if k != "$ref"}
        node = {**target, **rest} if rest and isinstance(target, dict) else target
    if node is True or node is None:
        return {}, seen
    if node is False:
        return {"not": {}}, seen
    if not isinstance(node, dict):
        raise SchemaError("a schema is an object")
    return node, seen


def _types(node: dict) -> list[str]:
    t = node.get("type")
    if isinstance(t, str):
        return [t]
    if isinstance(t, list):
        return [x for x in t if isinstance(x, str)]
    if "const" in node:
        return [_json_type(node["const"])]
    if isinstance(node.get("enum"), list) and node["enum"]:
        got = []
        for v in node["enum"]:
            jt = _json_type(v)
            if jt not in got:
                got.append(jt)
        return got
    if "properties" in node:
        return ["object"]
    if "items" in node:
        return ["array"]
    return []


def _json_type(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "integer" if v.is_integer() else "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    return "object"


def _is(v, t: str) -> bool:
    if t == "null":
        return v is None
    if t == "boolean":
        return isinstance(v, bool)
    if t == "integer":
        return (isinstance(v, int) and not isinstance(v, bool)) or \
            (isinstance(v, float) and v.is_integer())
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool) and \
            not (isinstance(v, float) and (math.isnan(v) or math.isinf(v)))
    if t == "string":
        return isinstance(v, str)
    if t == "array":
        return isinstance(v, list)
    if t == "object":
        return isinstance(v, dict)
    return False


def _same(a, b) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True) and \
        _json_type(a) == _json_type(b)


# ======================================================================================= validate
def validate(value, schema, *, root: dict | None = None, path: str = "$",
             limit: int = 50) -> list[str]:
    """Every way `value` breaks `schema` (at most `limit`), each "<path>: <why>"."""
    out: list[str] = []
    _check(value, schema, root if root is not None else schema, path, out, limit, 0)
    return out[:limit]


def _check(v, node, root, path, out, limit, depth) -> None:
    if len(out) >= limit:
        return
    if depth > 64:
        out.append(f"{path}: the schema nests too deep")
        return
    try:
        node, _ = _resolve(node, root)
    except SchemaError as exc:
        out.append(f"{path}: {exc}")
        return
    if "not" in node and not node["not"] and len(node) == 1:
        out.append(f"{path}: no value is allowed here")
        return
    ts = node.get("type")
    if ts is not None:
        tl = [ts] if isinstance(ts, str) else list(ts)
        if not any(_is(v, t) for t in tl):
            out.append(f"{path}: expected {' or '.join(tl)}, got {_json_type(v)}")
            return
    if "const" in node and not _same(v, node["const"]):
        out.append(f"{path}: must be {json.dumps(node['const'])}")
    if "enum" in node and not any(_same(v, e) for e in node["enum"]):
        shown = ", ".join(json.dumps(e) for e in node["enum"][:12])
        out.append(f"{path}: one of {shown}{', ...' if len(node['enum']) > 12 else ''}")
    for key, sub in (("anyOf", node.get("anyOf")), ("oneOf", node.get("oneOf"))):
        if isinstance(sub, list) and sub:
            ok = sum(1 for s in sub if not validate(v, s, root=root, path=path, limit=1))
            if key == "anyOf" and ok == 0:
                out.append(f"{path}: matches none of the allowed forms")
            if key == "oneOf" and ok != 1:
                out.append(f"{path}: must match exactly one of the allowed forms (matches {ok})")
    for s in node.get("allOf") or []:
        _check(v, s, root, path, out, limit, depth + 1)
    if "not" in node and node["not"] and not validate(v, node["not"], root=root, path=path,
                                                      limit=1):
        out.append(f"{path}: a value of a form that is not allowed here")
    if "if" in node:
        branch = "then" if not validate(v, node["if"], root=root, path=path, limit=1) else "else"
        if branch in node:
            _check(v, node[branch], root, path, out, limit, depth + 1)
    if isinstance(v, str):
        n = len(v)
        if "minLength" in node and n < node["minLength"]:
            out.append(f"{path}: at least {node['minLength']} characters")
        if "maxLength" in node and n > node["maxLength"]:
            out.append(f"{path}: at most {node['maxLength']} characters")
        if "pattern" in node:
            try:
                if not re.search(node["pattern"], v):
                    out.append(f"{path}: does not match {node['pattern']}")
            except re.error:
                out.append(f"{path}: the schema's pattern does not compile")
    if _is(v, "number"):
        if "minimum" in node and v < node["minimum"]:
            out.append(f"{path}: at least {node['minimum']}")
        if "maximum" in node and v > node["maximum"]:
            out.append(f"{path}: at most {node['maximum']}")
        if "exclusiveMinimum" in node and v <= node["exclusiveMinimum"]:
            out.append(f"{path}: more than {node['exclusiveMinimum']}")
        if "exclusiveMaximum" in node and v >= node["exclusiveMaximum"]:
            out.append(f"{path}: less than {node['exclusiveMaximum']}")
        m = node.get("multipleOf")
        if isinstance(m, (int, float)) and m > 0 and abs(v / m - round(v / m)) > 1e-9:
            out.append(f"{path}: a multiple of {m}")
    if isinstance(v, list):
        if "minItems" in node and len(v) < node["minItems"]:
            out.append(f"{path}: at least {node['minItems']} items")
        if "maxItems" in node and len(v) > node["maxItems"]:
            out.append(f"{path}: at most {node['maxItems']} items")
        if node.get("uniqueItems"):
            seen = set()
            for x in v:
                k = json.dumps(x, sort_keys=True)
                if k in seen:
                    out.append(f"{path}: the items must differ")
                    break
                seen.add(k)
        items = node.get("items")
        if isinstance(items, (dict, bool)):
            for i, x in enumerate(v):
                _check(x, items, root, f"{path}[{i}]", out, limit, depth + 1)
    if isinstance(v, dict):
        props = node.get("properties") or {}
        for r in node.get("required") or []:
            if r not in v:
                out.append(f"{path}: {r} is required")
        if "minProperties" in node and len(v) < node["minProperties"]:
            out.append(f"{path}: at least {node['minProperties']} entries")
        if "maxProperties" in node and len(v) > node["maxProperties"]:
            out.append(f"{path}: at most {node['maxProperties']} entries")
        names = node.get("propertyNames")
        pats = node.get("patternProperties") or {}
        extra = node.get("additionalProperties", True)
        for k, x in v.items():
            sub_path = f"{path}.{k}"
            if names is not None:
                for e in validate(k, names, root=root, path=f"{path} key {k!r}", limit=1):
                    out.append(e)
            matched = False
            if k in props:
                matched = True
                _check(x, props[k], root, sub_path, out, limit, depth + 1)
            for p, s in pats.items():
                try:
                    if re.search(p, k):
                        matched = True
                        _check(x, s, root, sub_path, out, limit, depth + 1)
                except re.error:
                    out.append(f"{path}: the schema's pattern {p!r} does not compile")
            if matched:
                continue
            if extra is False:
                out.append(f"{sub_path}: not a known field")
            elif isinstance(extra, dict):
                _check(x, extra, root, sub_path, out, limit, depth + 1)


# ======================================================================================= strict
def check_schema(schema, *, max_depth: int = MAX_DEPTH, max_fields: int = MAX_FIELDS,
                 allow_write_only: bool = False) -> list[str]:
    """Why `schema` cannot be a plugin's settings schema (STRICT); empty when it can."""
    out: list[str] = []
    if not isinstance(schema, dict):
        return ["the settings schema is an object"]
    if schema.get("type") != "object":
        out.append("$: the settings schema's root is {\"type\": \"object\", ...}")
    count = [0]
    _strict(schema, schema, "$", out, 0, count, (), max_depth, allow_write_only)
    if count[0] > max_fields:
        out.append(f"$: {count[0]} fields, at most {max_fields}")
    return out[:50]


def _strict(node, root, path, out, depth, count, refs, max_depth, allow_write_only) -> None:
    if len(out) >= 50:
        return
    try:
        node, refs = _resolve(node, root, refs)
    except SchemaError as exc:
        out.append(f"{path}: {exc}")
        return
    unknown = sorted(k for k in node if k not in FORM_KEYWORDS)
    if unknown:
        out.append(f"{path}: keyword {unknown[0]!r} is not one a form reads "
                   f"({len(unknown)} such)")
    t = _types(node)
    for x in t:
        if x not in TYPES:
            out.append(f"{path}: type {x!r} is not a JSON type")
    if node.get("writeOnly") and not allow_write_only:
        out.append(f"{path}: writeOnly: a secret is a name in the manifest's `secrets`, kept "
                   f"sealed; not a setting")
    for k in ("pattern",):
        if k in node:
            try:
                re.compile(node[k])
            except (re.error, TypeError):
                out.append(f"{path}: pattern does not compile")
    if "object" in t or "properties" in node:
        if depth >= max_depth:
            out.append(f"{path}: nested deeper than {max_depth} levels")
            return
        if node.get("additionalProperties", True) is not False:
            out.append(f"{path}: an object says \"additionalProperties\": false (wrong input "
                       f"is refused, never ignored)")
        props = node.get("properties") or {}
        if not isinstance(props, dict):
            out.append(f"{path}: properties is an object")
            props = {}
        req = node.get("required") or []
        for r in req:
            if r not in props:
                out.append(f"{path}: required names {r!r}, which is not a property")
        for k, sub in props.items():
            count[0] += 1
            _strict(sub, root, f"{path}.{k}", out, depth + 1, count, refs, max_depth,
                    allow_write_only)
    if "array" in t or "items" in node:
        items = node.get("items")
        if not isinstance(items, dict):
            out.append(f"{path}: an array says what its items are (items: a schema)")
        else:
            if depth >= max_depth:
                out.append(f"{path}: nested deeper than {max_depth} levels")
                return
            _strict(items, root, f"{path}[]", out, depth + 1, count, refs, max_depth,
                    allow_write_only)
    for key in ("anyOf", "oneOf"):
        for i, sub in enumerate(node.get(key) or []):
            _strict(sub, root, f"{path}.{key}[{i}]", out, depth, count, refs, max_depth,
                    allow_write_only)
    if "default" in node:
        bad = validate(node["default"], node, root=root, path=f"{path} default", limit=1)
        if bad:
            out.append(bad[0])


def defaults(schema) -> dict:
    """{property: its default} of an object schema's top level (those that have one)."""
    try:
        node, _ = _resolve(schema, schema)
    except SchemaError:
        return {}
    out = {}
    for k, sub in (node.get("properties") or {}).items():
        try:
            s, _ = _resolve(sub, schema)
        except SchemaError:
            continue
        if "default" in s:
            out[k] = json.loads(json.dumps(s["default"]))
    return out


# ======================================================================================= describe
def describe(schema, *, max_depth: int = MAX_DEPTH, max_fields: int = MAX_FIELDS) -> dict:
    """The form of `schema` (FORM above). An object schema gives its properties as the fields; any
    other schema one field with the key "value"."""
    st = {"root": schema if isinstance(schema, dict) else {}, "count": 0, "notes": [],
          "max_depth": max_depth, "max_fields": max_fields}
    try:
        node, refs = _resolve(schema, st["root"])
    except SchemaError as exc:
        node, refs = {}, ()
        st["notes"].append(f"$: {exc}")
    title = node.get("title") if isinstance(node.get("title"), str) else None
    desc = node.get("description") if isinstance(node.get("description"), str) else None
    if _types(node) == ["object"] or (not _types(node) and "properties" in node):
        req = set(node.get("required") or [])
        fields = [_field(sub, [k], k in req, st, 1, refs)
                  for k, sub in (node.get("properties") or {}).items()]
    else:
        fields = [_field(node, ["value"], True, st, 1, refs)]
    return {"form": FORM_VERSION, "title": title, "description": desc, "fields": fields,
            "notes": st["notes"]}


def _key(path: list) -> str:
    return ".".join(path).replace(".[]", "[]")


def _raw(base: dict, why: str, st: dict) -> dict:
    base.update({"control": "json", "why": why})
    st["notes"].append(f"{base['key']}: {why}")
    return base


def _field(node, path: list, required: bool, st: dict, depth: int, refs: tuple) -> dict:
    st["count"] += 1
    name = path[-1] if path[-1] != "[]" else (path[-2] if len(path) > 1 else "item")
    base = {"key": _key(path), "path": list(path), "label": _words(name), "description": None,
            "type": "any", "control": "json", "required": bool(required), "nullable": False,
            "read_only": False, "secret": False, "has_default": False, "default": None}
    try:
        node, refs = _resolve(node, st["root"], refs)
    except SchemaError as exc:
        return _raw(base, str(exc), st)
    if isinstance(node.get("title"), str) and node["title"].strip():
        base["label"] = node["title"].strip()
    if isinstance(node.get("description"), str):
        base["description"] = node["description"]
    base["read_only"] = node.get("readOnly") is True
    base["secret"] = node.get("writeOnly") is True
    if "default" in node:
        base["has_default"] = True
        base["default"] = node["default"]
    if st["count"] > st["max_fields"]:
        return _raw(base, f"more than {st['max_fields']} fields: edited as JSON", st)
    # a nullable branch: anyOf/oneOf of one schema and {"type": "null"}
    for key in ("anyOf", "oneOf"):
        alts = node.get(key)
        if isinstance(alts, list) and alts:
            plain = [a for a in alts if not (isinstance(a, dict) and a.get("type") == "null"
                                            and len(a) == 1)]
            if len(plain) == 1 and len(alts) == 2:
                merged = {k: v for k, v in node.items() if k != key}
                try:
                    inner, refs2 = _resolve(plain[0], st["root"], refs)
                except SchemaError as exc:
                    return _raw(base, str(exc), st)
                merged = {**inner, **merged}
                st["count"] -= 1
                f = _field(merged, path, required, st, depth, refs2)
                f["nullable"] = True
                return f
            consts = [a for a in alts if isinstance(a, dict) and "const" in a]
            if len(consts) == len(alts):
                node = {**{k: v for k, v in node.items() if k != key},
                        "enum": [a["const"] for a in consts]}
                labels = [a.get("title") if isinstance(a.get("title"), str) else None
                          for a in consts]
                f = _field_plain(node, base, st, depth, refs, path, labels)
                return f
            return _raw(base, f"{key} of {len(alts)} forms: edited as JSON", st)
    return _field_plain(node, base, st, depth, refs, path, None)


def _field_plain(node, base, st, depth, refs, path, labels) -> dict:
    ts = _types(node)
    if "null" in ts and len(ts) > 1:
        base["nullable"] = True
        ts = [t for t in ts if t != "null"]
    if len(ts) == 2 and set(ts) == {"integer", "number"}:
        ts = ["number"]
    if len(ts) != 1:
        base["type"] = "any" if not ts else "any"
        return _raw(base, "no single type: edited as JSON" if ts else
                    "no type: edited as JSON", st)
    t = ts[0]
    base["type"] = t
    if "const" in node:
        base.update({"control": "const", "default": node["const"], "has_default": True})
        return base
    if base["secret"]:
        base["control"] = "secret"
        base["has_default"], base["default"] = False, None
        return base
    if isinstance(node.get("enum"), list) and node["enum"] and t != "boolean":
        vals = [v for v in node["enum"] if v is not None]
        if None in node["enum"]:
            base["nullable"] = True
        if len(vals) > MAX_OPTIONS:
            return _raw(base, f"an enum of {len(vals)} values: edited as JSON", st)
        base["control"] = "choice" if 2 <= len(vals) <= CHOICE_MAX else "select"
        base["options"] = [{"value": v, "label": (labels[i] if labels and i < len(labels)
                                                  and labels[i] else _option(v))}
                           for i, v in enumerate(vals)]
        return base
    if t == "boolean":
        base["control"] = "switch"
        return base
    if t in ("integer", "number"):
        base["control"] = "number"
        for src, dst in (("minimum", "min"), ("maximum", "max"),
                         ("exclusiveMinimum", "exclusive_min"),
                         ("exclusiveMaximum", "exclusive_max")):
            if _is(node.get(src), "number"):
                base[dst] = node[src]
        if t == "integer":
            base["step"] = 1
        if _is(node.get("multipleOf"), "number"):
            base["step"] = node["multipleOf"]
        return base
    if t == "string":
        ml = node.get("maxLength")
        base["control"] = "textarea" if _is(ml, "integer") and ml > TEXTAREA_OVER else "text"
        for src, dst in (("minLength", "min_length"), ("maxLength", "max_length"),
                         ("pattern", "pattern"), ("format", "format")):
            if src in node and node[src] is not None:
                base[dst] = node[src]
        return base
    if t == "object":
        if depth >= st["max_depth"]:
            return _raw(base, f"nested deeper than {st['max_depth']} levels: edited as JSON", st)
        props = node.get("properties")
        if not isinstance(props, dict) or not props:
            return _raw(base, "an object without named properties: edited as JSON", st)
        req = set(node.get("required") or [])
        base["control"] = "group"
        base["fields"] = [_field(sub, path + [k], k in req, st, depth + 1, refs)
                          for k, sub in props.items()]
        return base
    if t == "array":
        items = node.get("items")
        if not isinstance(items, dict):
            return _raw(base, "an array without one item schema: edited as JSON", st)
        if depth >= st["max_depth"]:
            return _raw(base, f"nested deeper than {st['max_depth']} levels: edited as JSON", st)
        base["control"] = "list"
        for src, dst in (("minItems", "min_items"), ("maxItems", "max_items")):
            if _is(node.get(src), "integer"):
                base[dst] = node[src]
        if node.get("uniqueItems") is True:
            base["unique"] = True
        base["item"] = _field(items, path + ["[]"], True, st, depth + 1, refs)
        return base
    if t == "null":
        base.update({"control": "const", "default": None, "has_default": True})
        return base
    return _raw(base, f"type {t!r}: edited as JSON", st)


def _option(v) -> str:
    if isinstance(v, str):
        return v
    return json.dumps(v, separators=(",", ":"), ensure_ascii=False)
