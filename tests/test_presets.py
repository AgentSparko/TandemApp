"""The providers the Backends page offers (proxy/presets.py): a provider, a key and go.

  1  Every preset is well formed: an id, a label, a group, a key rule and at least one endpoint
     whose protocol is openai or anthropic and whose kind the relay knows; a cloud or self-hosted
     preset has an http(s) URL, a custom one none
                                                                         test_presets_are_well_formed
  2  GET /v1/config/presets and GET /v1/config answer the same list
                                                                         test_the_api_lists_them
  3  A backend made from a preset keeps it: its preset id, the kind and URL of the chosen
     protocol, and a stored cloud flag over the kind's (generic + cloud = a paid cloud API)
                                                                         test_a_backend_keeps_its_preset_and_cloud
  4  An unknown preset is refused, and nothing is saved
                                                                         test_an_unknown_preset_is_refused
  5  A PATCH sets the cloud flag back to the kind's (null)
                                                                         test_cloud_back_to_the_kind
  6  The page's mock offers the same providers (dashboard/mock/presets.ts, made from this list)
                                                                         test_the_mock_matches

CPU only. Run: python tests/test_presets.py
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from proxy import config as config_mod  # noqa: E402
from proxy import presets as presets_mod  # noqa: E402
from proxy_util import Proxy, run  # noqa: E402


def test_presets_are_well_formed():
    ids = set()
    for p in presets_mod.presets():
        assert p["id"] not in ids, p["id"]
        ids.add(p["id"])
        assert p["label"] and p["group"] in ("cloud", "self-hosted", "custom"), p
        assert p["key"] in ("required", "optional", "none"), p
        assert p["endpoints"], p
        for e in p["endpoints"]:
            assert e["protocol"] in ("openai", "anthropic"), p
            assert e["kind"] in config_mod.KINDS, p
            if p["group"] == "custom":
                assert e["url"] == "", p
            else:
                assert e["url"].startswith(("http://", "https://")), p
        protos = [e["protocol"] for e in p["endpoints"]]
        assert len(protos) == len(set(protos)), p
    assert {"openai", "anthropic", "openrouter", "zai", "nous", "custom-openai", "custom-anthropic"} <= ids
    return f"{len(ids)} presets"


def test_the_api_lists_them():
    with Proxy() as px:
        st, _, body = px.get("/v1/config/presets")
        assert st == 200, body
        listed = json.loads(body)["presets"]
        st, _, body = px.get("/v1/config")
        assert st == 200, body
        assert json.loads(body)["presets"] == listed
        assert [p["id"] for p in listed] == [p["id"] for p in presets_mod.PRESETS]
    return f"{len(listed)} presets on both routes"


def test_a_backend_keeps_its_preset_and_cloud():
    z = presets_mod.by_id("zai")
    oa = next(e for e in z["endpoints"] if e["protocol"] == "openai")
    with Proxy() as px:
        st, _, body = px.post("/v1/config/backends", {"id": "zai", "kind": oa["kind"], "url": oa["url"],
                                                      "preset": "zai", "cloud": True, "key": "sk-test-zai"})
        assert st == 201, body
        b = json.loads(body)
        assert b["preset"] == "zai" and b["kind"] == "generic" and b["cloud"] is True, b
        assert b["url"].rstrip("/") == oa["url"].rstrip("/"), b
        st, _, body = px.get("/v1/config/backends/zai")
        assert json.loads(body)["preset"] == "zai" and json.loads(body)["cloud"] is True
        # a generic backend without a flag is the kind's: not cloud
        st, _, body = px.post("/v1/config/backends", {"id": "local1", "kind": "generic",
                                                      "url": "http://127.0.0.1:9/v1", "preset": "ollama"})
        assert st == 201, body
        assert json.loads(body)["cloud"] is False and json.loads(body)["preset"] == "ollama"
    return "generic + cloud kept; a generic without the flag is local"


def test_an_unknown_preset_is_refused():
    with Proxy() as px:
        st, _, body = px.post("/v1/config/backends", {"id": "x1", "kind": "generic",
                                                      "url": "http://127.0.0.1:9/v1", "preset": "nope"})
        assert st == 400 or st == 422, (st, body)
        assert "preset" in json.loads(body)["error"]["message"], body
        st, _, body = px.get("/v1/config/backends/x1")
        assert st == 404, body
    return "refused, nothing saved"


def test_cloud_back_to_the_kind():
    with Proxy() as px:
        st, _, body = px.post("/v1/config/backends", {"id": "or1", "kind": "generic", "url": "https://openrouter.ai/api/v1",
                                                      "preset": "openrouter", "cloud": True})
        assert st == 201, body
        st, _, body = px.request("PATCH", "/v1/config/backends/or1", {"cloud": None})
        assert st == 200, body
        assert json.loads(body)["cloud"] is False, body
    return "null is the kind's flag"


def test_the_mock_matches():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = open(os.path.join(root, "dashboard", "mock", "presets.ts")).read()
    body = text[text.index("= [") + 2: text.rindex("];") + 1]
    assert json.loads(body) == presets_mod.presets(), "dashboard/mock/presets.ts differs: make it again from proxy/presets.py"
    return "the same list"


if __name__ == "__main__":
    run(globals())
