"""SRV-857 (QA SRV-858): the form generator and the schema checks (proxy/schemaform.py), the one
module that turns a JSON schema into the form a page draws; dashboard/src/lib/schema-form.ts is the
same generator and dashboard/tests/schema-form.test.ts holds it to the same fixtures.

  1  Every fixture in contracts/plugin-v1/forms gives exactly its form, and each form validates
     against form.schema.json                                  test_fixtures
  2  Every schema the contracts publish (the conductor's documents, hooks-v1 with its events,
     settings-v1, plugin-v1: over 80) gives a form that validates; where the generator cannot
     draw a part it falls back to the raw editor with a note   test_every_contract_schema
  3  The controls by type, enum size, length, nullability, secrets, constants; groups and lists
     nest; the limits (depth, fields, options) fall back with a note
                                                               test_controls_and_limits
  4  validate(): types, enum, const, ranges, lengths, patterns, items, required, unknown fields,
     nested paths, anyOf/oneOf/allOf/not/if, $ref; booleans are not integers
                                                               test_validate
  5  check_schema() (a plugin's settings schema): an open object, writeOnly, an unknown keyword,
     a bad default, a remote or cyclic $ref, too deep, too many fields are refused; the examples
     pass; defaults() reads the top level's                    test_check_schema

Run: python tests/test_schema_form.py
"""

from __future__ import annotations

import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from proxy import schemaform as SF  # noqa: E402
from tools import contract_check  # noqa: E402

CON = os.path.join(ROOT, "contracts", "plugin-v1")


def form_ok(form: dict, where: str = "") -> None:
    errs = contract_check.validate(form, contract_check.load("form.schema.json", CON), root=CON)
    assert not errs, (where, errs[:5])


def fields(form) -> list[dict]:
    out = []

    def walk(fs):
        for f in fs:
            out.append(f)
            walk(f.get("fields") or [])
            if f.get("item"):
                walk([f["item"]])
    walk(form["fields"] if isinstance(form, dict) else form)
    return out


def test_fixtures():
    files = sorted(glob.glob(os.path.join(CON, "forms", "*.json")))
    assert len(files) >= 12, files
    for p in files:
        with open(p) as f:
            fx = json.load(f)
        got = json.loads(json.dumps(SF.describe(fx["schema"])))
        assert got == fx["form"], (os.path.basename(p), json.dumps(got)[:400])
        form_ok(got, p)
    return f"{len(files)} fixtures"


def test_every_contract_schema():
    files = []
    for d in ("conductor-v1", "hooks-v1", os.path.join("hooks-v1", "events"), "settings-v1",
              "plugin-v1"):
        files += sorted(glob.glob(os.path.join(ROOT, "contracts", d, "*.schema.json")))
    assert len(files) > 80, len(files)
    n = raw = 0
    for p in files:
        with open(p) as f:
            form = SF.describe(json.load(f))
        form_ok(form, p)
        fs = fields(form)
        n += len(fs)
        raw += sum(1 for f in fs if f["control"] == "json")
        # each raw editor has its note; a root that does not resolve adds one of its own
        assert len([f for f in fs if f["control"] == "json"]) == \
            len([x for x in form["notes"] if not x.startswith("$: ")]), p
    assert n > 500, n
    return f"{len(files)} schemas, {n} fields, {raw} on the raw editor"


def test_controls_and_limits():
    f = SF.describe({"type": "object", "required": ["a"], "properties": {
        "a": {"type": "boolean"}, "b": {"enum": ["x", "y"]}, "c": {"enum": list("abcde")},
        "d": {"type": "string", "maxLength": 201}, "e": {"type": "string", "maxLength": 200},
        "s": {"type": "string", "writeOnly": True, "default": "nope"},
        "k": {"const": 3}, "n": {"type": ["number", "null"], "minimum": 0},
        "o": {"anyOf": [{"$ref": "#/$defs/p"}, {"type": "null"}]},
        "g": {"type": "object", "properties": {"x": {"type": "integer"}}},
        "l": {"type": "array", "items": {"type": "string"}, "maxItems": 3}},
        "$defs": {"p": {"type": "integer", "title": "Port"}}})
    by = {x["key"]: x for x in f["fields"]}
    assert [by[k]["control"] for k in "abcdeskn"] == ["switch", "choice", "select", "textarea",
                                                        "text", "secret", "const", "number"]
    assert by["a"]["required"] and not by["b"]["required"]
    assert by["s"]["secret"] and by["s"]["default"] is None and not by["s"]["has_default"]
    assert by["n"]["nullable"] and by["n"]["min"] == 0
    assert by["o"]["nullable"] and by["o"]["label"] == "Port" and by["o"]["step"] == 1
    assert by["g"]["control"] == "group" and by["g"]["fields"][0]["key"] == "g.x"
    assert by["l"]["control"] == "list" and by["l"]["item"]["key"] == "l[]"
    assert by["l"]["max_items"] == 3
    many = SF.describe({"type": "object", "properties": {f"k{i}": {"type": "integer"}
                                                          for i in range(SF.MAX_FIELDS + 3)}})
    assert sum(1 for x in many["fields"] if x["control"] == "json") == 3 and \
        len(many["notes"]) == 3
    deep = SF.describe({"type": "object", "properties": {"a": {"type": "object", "properties": {
        "b": {"type": "string"}}}}}, max_depth=1)
    assert deep["fields"][0]["control"] == "json" and "deeper than 1" in deep["fields"][0]["why"]
    big = SF.describe({"type": "object", "properties": {"e": {"enum": list(range(250))}}})
    assert big["fields"][0]["control"] == "json"
    words = SF.describe({"type": "object", "properties": {"max_tokens": {}, "retryAfterS": {},
                                                          "x": {"title": " Given "}}})
    assert [x["label"] for x in words["fields"]] == ["Max tokens", "Retry after s", "Given"]
    one = SF.describe({"type": "string", "title": "Only"})
    assert one["fields"][0]["key"] == "value" and one["title"] == "Only"
    return "controls by type; limits fall back with a note"


def test_validate():
    s = {"type": "object", "required": ["n"], "additionalProperties": False, "$defs": {
        "port": {"type": "integer", "minimum": 1, "maximum": 65535}}, "properties": {
        "n": {"type": "integer", "minimum": 0, "maximum": 10},
        "s": {"type": "string", "minLength": 2, "maxLength": 4, "pattern": "^[a-z]+$"},
        "e": {"enum": ["a", "b"]}, "c": {"const": 1}, "p": {"$ref": "#/$defs/port"},
        "l": {"type": "array", "items": {"type": "number"}, "maxItems": 2, "uniqueItems": True},
        "o": {"type": "object", "additionalProperties": False, "properties": {
            "x": {"type": "boolean"}}},
        "u": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "one": {"oneOf": [{"type": "integer"}, {"type": "number"}]},
        "nt": {"not": {"const": "bad"}},
        "f": {"type": "number", "exclusiveMinimum": 0, "multipleOf": 0.5}}}
    assert SF.validate({"n": 3, "s": "ab", "e": "a", "c": 1, "p": 80, "l": [1, 2.5],
                        "o": {"x": True}, "u": None, "f": 1.5, "nt": "ok"}, s) == []
    cases = [({"n": True}, "$.n: expected integer"), ({}, "$: n is required"),
             ({"n": 11}, "$.n: at most 10"), ({"n": 1, "s": "a"}, "$.s: at least 2"),
             ({"n": 1, "s": "abcde"}, "$.s: at most 4"), ({"n": 1, "s": "AB"}, "$.s: does not match"),
             ({"n": 1, "e": "z"}, "$.e: one of"), ({"n": 1, "c": 2}, "$.c: must be 1"),
             ({"n": 1, "p": 0}, "$.p: at least 1"), ({"n": 1, "l": [1, 1]}, "$.l: the items must"),
             ({"n": 1, "l": [1, 2, 3]}, "$.l: at most 2 items"), ({"n": 1, "l": ["x"]}, "$.l[0]"),
             ({"n": 1, "o": {"y": 1}}, "$.o.y: not a known field"), ({"n": 1, "z": 1}, "$.z: not a"),
             ({"n": 1, "u": 3}, "$.u: matches none"), ({"n": 1, "one": 2}, "$.one: must match exactly"),
             ({"n": 1, "nt": "bad"}, "$.nt: a value of a form"), ({"n": 1, "f": 0}, "$.f: more than 0"),
             ({"n": 1, "f": 0.7}, "$.f: a multiple of 0.5"), ({"n": 1, "c": True}, "$.c: must be 1")]
    for v, want in cases:
        got = SF.validate(v, s)
        assert got and any(g.startswith(want) for g in got), (v, want, got)
    cond = {"if": {"properties": {"k": {"const": "a"}}}, "then": {"required": ["x"]},
            "else": {"required": ["y"]}}
    assert SF.validate({"k": "a"}, cond) == ["$: x is required"]
    assert SF.validate({"k": "b"}, cond) == ["$: y is required"]
    assert SF.validate({"k": "b", "y": 1}, {"allOf": [cond, {"maxProperties": 1}]}) == \
        ["$: at most 1 entries"]
    assert SF.validate(1, {"$ref": "#/nope"})[0].startswith("$: $ref '#/nope' does not resolve")
    return f"{len(cases)} refusals by path"


def test_check_schema():
    for name in ("hello-tool", "audit-hook", "classify-provider"):
        with open(os.path.join(CON, "examples", name, "plugin.json")) as f:
            assert SF.check_schema(json.load(f)["settings"]) == [], name
    ok = {"type": "object", "additionalProperties": False, "properties": {"a": {"type": "string"}}}
    bad = {
        "open root": ({"type": "object", "properties": {}}, "additionalProperties"),
        "open nested": (dict(ok, properties={"o": {"type": "object", "properties": {}}}),
                        "$.o: an object says"),
        "not an object": ({"type": "string"}, "root"),
        "writeOnly": (dict(ok, properties={"t": {"type": "string", "writeOnly": True}}),
                      "$.t: writeOnly"),
        "keyword": (dict(ok, properties={"t": {"type": "string", "x-ui": 1}}), "keyword 'x-ui'"),
        "default": (dict(ok, properties={"t": {"type": "integer", "default": "x"}}),
                    "$.t default: expected integer"),
        "remote ref": (dict(ok, properties={"t": {"$ref": "other.json#/a"}}), "only a local"),
        "cycle": (dict(ok, properties={"t": {"$ref": "#/$defs/a"}},
                       **{"$defs": {"a": {"$ref": "#/$defs/a"}}}), "refers to itself"),
        "array without items": (dict(ok, properties={"t": {"type": "array"}}), "items"),
        "bad pattern": (dict(ok, properties={"t": {"type": "string", "pattern": "("}}),
                        "pattern does not compile"),
        "required not a property": (dict(ok, required=["zz"]), "required names 'zz'"),
    }
    deep = {"type": "object", "additionalProperties": False, "properties": {}}
    node = deep
    for i in range(SF.MAX_DEPTH + 1):
        child = {"type": "object", "additionalProperties": False, "properties": {}}
        node["properties"][f"l{i}"] = child
        node = child
    bad["too deep"] = (deep, "nested deeper")
    bad["too many"] = ({"type": "object", "additionalProperties": False, "properties": {
        f"k{i}": {"type": "integer"} for i in range(SF.MAX_FIELDS + 1)}}, "fields, at most")
    for why, (s, want) in bad.items():
        got = SF.check_schema(s)
        assert any(want in g for g in got), (why, want, got)
    assert SF.check_schema(ok) == []
    assert SF.defaults({"type": "object", "properties": {"a": {"default": [1]}, "b": {},
                                                         "c": {"$ref": "#/$defs/c"}},
                        "$defs": {"c": {"default": "z"}}}) == {"a": [1], "c": "z"}
    return f"{len(bad)} schemas refused, the examples pass"


if __name__ == "__main__":
    from scriptmode import run
    run(globals())
