"""SRV-877: an Anthropic-compatible server as a backend of kind `anthropic`.

The translation (proxy/translate/anthropic.py) against golden fixtures, one or more per row of the
plan's cell 3 table, request and stream (tests/fixtures/proxy/anthropic/cell3.json, written from
the Messages API's documented shapes, never recorded from paid calls); the adapter's headers, path,
usage and model pages; then the relay end to end against a fake Messages server in this process
(tests/fake_anthropic.py): a streamed chat completion with a tool, the usage and cost with a
cache, what cannot be translated, dropped sampling, 529 tried again, an error in the stream, a
tool loop of 3 turns, model discovery across 3 pages, and the client's own key never upstream.

Run: python tests/test_proxy_anthropic.py
"""

from __future__ import annotations

import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from proxy_util import FIXTURE_KEYS, Proxy, run  # noqa: E402

import fake_anthropic  # noqa: E402
from proxy import adapters as A  # noqa: E402
from proxy import relay as relay_mod  # noqa: E402
from proxy.adapters.base import CHAT, Stamps  # noqa: E402
from proxy.translate import anthropic as tr  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "proxy", "anthropic",
                   "cell3.json")
KEY = FIXTURE_KEYS[0]
CLIENT_KEY = "client-own-secret-0001"
ROWS = ("system prompts", "max_tokens", "sampling", "other parameters", "tools", "tool choice",
        "tool calls", "images", "thinking", "cache control", "output format", "stop reasons",
        "stream")


def _fx() -> dict:
    with open(FIX) as f:
        return json.load(f)


def _frames(data: bytes) -> list:
    return [json.loads(f[6:]) if f != "data: [DONE]" else "[DONE]"
            for f in data.decode().split("\n\n") if f.strip()]


# ------------------------------------------------------------------------------- the translation
def test_every_row_of_the_table_has_golden_fixtures():
    fx = _fx()
    req = {c["row"] for c in fx["requests"].values()}
    out = {c["row"] for c in fx["answers"].values()} | {c["row"] for c in fx["streams"].values()}
    assert req | out == set(ROWS), set(ROWS) - (req | out)
    assert {r for r in ROWS if r != "stop reasons"} <= req, "every request row"
    assert {"stream", "cache control", "thinking", "tool calls", "stop reasons"} <= \
        {c["row"] for c in fx["streams"].values()}


def test_golden_requests():
    for name, c in _fx()["requests"].items():
        opts = dict(tr.DEFAULTS, **c.get("opts", {}))
        if "error" in c:
            try:
                tr.to_messages(c["body"], "model-a", opts, c.get("max_output"))
            except tr.TranslateError as e:
                assert e.param == c["error"] and e.body() == c["want_error"], name
            else:
                raise AssertionError(f"{name}: no 400")
            continue
        got = tr.to_messages(c["body"], "model-a", opts, c.get("max_output"))
        assert got == c["want"], (name, got)
        assert tr.dropped(c["body"], opts) == c["dropped"], name


def test_golden_answers_and_their_ledger_usage():
    fx = _fx()
    a = A.make({"id": "b", "kind": "anthropic"})
    for name, c in fx["answers"].items():
        out, u = tr.from_message(c["message"], fx["created"])
        assert out == c["want"], (name, out)
        assert a.read_usage(u) == c["ledger"], name
        st = Stamps()
        a.read_body(CHAT, out, st)
        assert st.finish_reason == c["want"]["choices"][0]["finish_reason"], name


def test_golden_streams_and_what_the_ledger_reader_sees():
    fx = _fx()
    a = A.make({"id": "b", "kind": "anthropic"})
    for name, c in fx["streams"].items():
        s = tr.StreamToChat(fx["created"])
        data = b"".join(s.feed(e.encode()) for e in c["events"])
        assert _frames(data) == c["want"], (name, _frames(data))
        assert a.read_usage(s.usage) == c["ledger"], name
        st = Stamps()
        for f in data.split(b"\n\n"):
            if f:
                a.read_frame(CHAT, f, st, False)
        if name == "error_after_text":
            assert st.error and relay_mod._finish(st) == "error" and st.done
        else:
            assert st.done and st.finish_reason in ("stop", "tool_calls", "length",
                                                     "content_filter"), name
        if name == "tools":
            assert st.tools == {0, 1} and st.finish_reason == "tool_calls"
        if name == "thinking_then_text":
            assert st.reasoning and b"sig-fixture" not in data


def test_a_stream_cut_anywhere_translates_the_same():
    """The relay feeds the translator whatever pieces the socket gives (relay._translate)."""
    fx = _fx()
    rnd = random.Random(877)
    for name, c in fx["streams"].items():
        raw = ("\n\n".join(c["events"]) + "\n\n").encode()
        for _ in range(20):
            s, st, buf, out = tr.StreamToChat(fx["created"]), Stamps(), bytearray(), b""
            i = 0
            while i < len(raw):
                j = min(len(raw), i + rnd.randint(1, 40))
                buf += raw[i:j]
                out += bytes(relay_mod._translate(s, buf, st, False))
                buf = s.rest
                i = j
            out += bytes(relay_mod._translate(s, buf, st, True))
            assert _frames(out) == c["want"], name
            assert st.native == s.usage


def test_golden_errors():
    for name, c in _fx()["errors"].items():
        data = json.dumps(c["body"]).encode() if c["body"] is not None else b"<html>bad</html>"
        st, body = tr.error_out(c["status"], data)
        assert (st, json.loads(body)) == (c["want_status"], c["want"]), name


# ------------------------------------------------------------------------------- the adapter
def test_adapter_headers_path_and_what_it_never_forwards():
    from email.message import Message
    from proxy import http1
    a = A.make({"id": "b", "kind": "anthropic"})
    h = a.headers("k")
    assert ("x-api-key", "k") in h and ("anthropic-version", "2023-06-01") in h
    assert not any(k == "Authorization" for k, _ in h)
    b = A.make({"id": "b", "kind": "anthropic",
                "anthropic": {"auth": "bearer", "api_version": "2026-10-21"}})
    assert ("Authorization", "Bearer k") in b.headers("k") and \
        ("anthropic-version", "2026-10-21") in b.headers("k")
    assert not any(k == "x-api-key" for k, _ in b.headers("k"))
    m = Message()
    m["Authorization"], m["anthropic-beta"], m["X-Tandem"] = f"Bearer {CLIENT_KEY}", "x", "raw"
    assert a.forward(m, "k") == [] and a.forward(m, None) == []
    for url, want in (("https://api.example", "/v1/messages"),
                      ("https://api.example/v1", "/v1/messages"),
                      ("https://gw.example/anthropic/", "/anthropic/v1/messages"),
                      ("https://gw.example/anthropic/v1", "/anthropic/v1/messages")):
        p = http1.Pool(url)
        assert a.upstream_path(p, CHAT) == want, url
        assert a.upstream_path(p, "/v1/models") == want.replace("messages", "models"), url
        p.close()
    assert a.retryable(529) and a.retryable(429) and not a.retryable(500)
    assert not A.make({"kind": "openai"}).retryable(529), "the other kinds keep their set"
    assert a.passes("request-id") and a.passes("anthropic-ratelimit-requests-remaining")
    assert a.passes("retry-after") and not a.passes("set-cookie")
    assert a.routes() == (CHAT, "/v1/messages", "/v1/messages/count_tokens") and \
        a.inject_usage() is False and a.cloud


def test_prepare_drops_the_usage_chunk_unless_the_client_asked():
    a = A.make({"id": "b", "kind": "anthropic"})
    body = {"model": "x", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
    out, injected = a.prepare(CHAT, b"", body, "model-a", max_output=8000)
    assert injected and json.loads(out)["max_tokens"] == 8000 and json.loads(out)["stream"]
    asked = dict(body, stream_options={"include_usage": True})
    assert a.prepare(CHAT, b"", asked, "model-a")[1] is False
    assert a.prepare(CHAT, b"", dict(body, stream=False), "model-a")[1] is False


def test_usage_and_cost_with_a_cache():
    """Gherkin: 100 input, 2000 cache-read, 500 cache-write tokens."""
    a = A.make({"id": "b", "kind": "anthropic"})
    u = a.read_usage({"input_tokens": 100, "cache_read_input_tokens": 2000,
                      "cache_creation_input_tokens": 500, "output_tokens": 100})
    assert u == {"prompt_tokens": 2600, "completion_tokens": 100, "cached_tokens": 2000,
                 "cache_write_tokens": 500, "reasoning_tokens": None}
    p = {"input": 3.0, "cached_input": 0.3, "cache_write": 3.75, "output": 15.0}
    assert a.price(u, p) == 100 * 3 + 600 + 1875 + 1500
    # an OpenAI-shaped object still reads as before
    assert a.read_usage({"prompt_tokens": 5, "completion_tokens": 1})["prompt_tokens"] == 5


def test_model_pages_parse_and_follow():
    a = A.make({"id": "b", "kind": "anthropic"})
    p1 = {"data": [fake_anthropic._model("model-a", 0)], "has_more": True,
          "first_id": "model-a", "last_id": "model-a"}
    assert a.next_page(p1) == "model-a"
    assert a.next_page(dict(p1, has_more=False)) is None
    assert a.models_query() == "?limit=1000"
    assert a.models_query("m/1") == "?limit=1000&after_id=m%2F1"
    m = a.parse_models(p1)[0]
    assert m == {"id": "model-a", "context_length": 200000, "name": "Model A",
                 "max_output": 32000, "input_modalities": ["text", "image"],
                 "effort": {"levels": ["low", "medium", "high", "max"], "default": None}}


def test_the_harness_switch_reads_effort():
    a = A.make({"id": "b", "kind": "anthropic"})
    body = {"model": "x", "messages": []}
    assert a.thinking(body, True, "high")[1] == "reasoning_effort=high"
    assert a.thinking(body, False, "off") == (body, "unsupported")
    assert a.thinking(dict(body, reasoning_effort="low"), True, "high")[1] == "client"


# ------------------------------------------------------------------------------- end to end
def _env(port, extra=None, url_tail=""):
    env = {"TANDEM_BACKENDS": "anth", "TANDEM_BACKEND_ANTH_KIND": "anthropic",
           "TANDEM_BACKEND_ANTH_URL": f"http://127.0.0.1:{port}{url_tail}",
           "TANDEM_BACKEND_ANTH_KEY": KEY}
    env.update(extra or {})
    return env


def _user(text="hi"):
    return [{"role": "user", "content": text}]


TOOL = {"type": "function", "function": {"name": "get_weather", "parameters": {
    "type": "object", "properties": {"city": {"type": "string"}}}}}


def test_relay_streams_a_chat_completion_with_a_tool():
    for auth, tail in (("x-api-key", ""), ("bearer", "/v1")):
        srv, port = fake_anthropic.start(key=KEY, auth=auth)
        extra = {"TANDEM_BACKEND_ANTH_ANTHROPIC_AUTH": auth} if auth == "bearer" else {}
        with Proxy(_env(port, extra, tail)) as px:
            px.sync()
            st, hdrs, data = px.post("/v1/chat/completions", {
                "model": "model-a", "stream": True, "messages": _user("weather?"),
                "tools": [TOOL]}, headers={"Authorization": f"Bearer {CLIENT_KEY}"})
            assert st == 200 and hdrs.get("X-Tandem-Backend") == "anth", (st, data)
            req = srv.fake.last
            assert req["path"] == "/v1/messages" and req["body"]["stream"] is True
            assert req["headers"].get("anthropic-version") == "2023-06-01"
            if auth == "bearer":
                assert req["headers"].get("Authorization") == f"Bearer {KEY}"
            else:
                assert req["headers"].get("x-api-key") == KEY and \
                    "Authorization" not in req["headers"]
            assert CLIENT_KEY not in json.dumps(srv.fake.requests), "the client's key upstream"
            assert req["body"]["tools"][0]["name"] == "get_weather" and \
                req["body"]["max_tokens"] == 32000, "64000 capped by the model's limit"
            fr = _frames(data)
            assert fr[0]["choices"][0]["delta"]["role"] == "assistant"
            assert any(f != "[DONE]" and f["choices"] and f["choices"][0]["delta"].get("content")
                       for f in fr)
            calls = [f["choices"][0]["delta"]["tool_calls"][0] for f in fr
                     if f != "[DONE]" and f["choices"]
                     and f["choices"][0]["delta"].get("tool_calls")]
            assert calls[0]["id"] == "toolu_fake_1" and calls[0]["function"]["name"] == \
                "get_weather"
            assert json.loads("".join(c["function"]["arguments"] for c in calls)) == \
                {"city": "City 1"}
            assert fr[-2]["choices"][0]["finish_reason"] == "tool_calls" and fr[-1] == "[DONE]"
            assert not any(f != "[DONE]" and "usage" in f for f in fr), \
                "no usage chunk the client did not ask for"
            r = px.rows()[-1]
            assert r["backend_kind"] == "anthropic" and r["finish_reason"] == "tool_calls"
            assert r["tool_calls"] == 1 and \
                r["upstream_request"] == f"msg_fake_{len(srv.fake.requests)}"
            for f in px.files():
                with open(f, "rb") as fh:
                    blob = fh.read()
                assert KEY.encode() not in blob and CLIENT_KEY.encode() not in blob, f
        srv.shutdown()


def test_relay_usage_and_cost_with_a_cache():
    srv, port = fake_anthropic.start(key=KEY)
    srv.fake.usage = {"input_tokens": 100, "cache_read_input_tokens": 2000,
                      "cache_creation_input_tokens": 500}
    with Proxy(_env(port, {"TANDEM_PRICE_1": "anth/model-a=3.0,0.3,15.0,3.75"})) as px:
        px.sync()
        for stream in (False, True):
            body = {"model": "model-a", "messages": _user(), "stream": stream}
            if stream:
                body["stream_options"] = {"include_usage": True}
            st, _, data = px.post("/v1/chat/completions", body)
            assert st == 200, data
            u = (json.loads(data) if not stream else
                 [f for f in _frames(data) if f != "[DONE]" and "usage" in f][0])["usage"]
            assert u["prompt_tokens"] == 2600 and u["prompt_tokens_details"]["cached_tokens"] \
                == 2000 and "cache_write_tokens" not in json.dumps(u)
            r = px.rows()[-1]
            assert (r["prompt_tokens"], r["cached_tokens"], r["cache_write_tokens"],
                    r["completion_tokens"]) == (2600, 2000, 500, 5), r
            assert r["cost_micros"] == round(100 * 3.0 + 2000 * 0.3 + 500 * 3.75 + 5 * 15.0)
            assert srv.fake.last["body"]["cache_control"] == {"type": "ephemeral"}
    srv.shutdown()


def test_relay_refuses_what_cannot_be_translated():
    srv, port = fake_anthropic.start(key=KEY)
    with Proxy(_env(port)) as px:
        px.sync()
        n0 = len(srv.fake.requests)
        for extra, field in (({"n": 2}, "n"),
                             ({"response_format": {"type": "json_object"}}, "response_format")):
            st, _, data = px.post("/v1/chat/completions",
                                  dict({"model": "model-a", "messages": _user()}, **extra))
            e = json.loads(data)["error"]
            assert st == 400 and e["param"] == field and field in e["message"], data
        assert len(srv.fake.requests) == n0, "nothing went upstream"
        rows = px.rows()
        assert [r["status"] for r in rows[-2:]] == [400, 400]
        assert rows[-1]["error_type"] == "untranslatable"
    srv.shutdown()


def test_relay_drops_sampling_and_says_so():
    srv, port = fake_anthropic.start(key=KEY)
    with Proxy(_env(port)) as px:
        px.sync()
        st, hdrs, _ = px.post("/v1/chat/completions", {"model": "model-a", "messages": _user(),
                                                       "temperature": 0.2, "seed": 3})
        assert st == 200 and "temperature" not in srv.fake.last["body"]
        assert hdrs.get("X-Tandem-Dropped") == "temperature,seed"
        st, hdrs, _ = px.post("/v1/chat/completions", {"model": "model-a", "messages": _user()})
        assert "X-Tandem-Dropped" not in hdrs
    with Proxy(_env(port, {"TANDEM_BACKEND_ANTH_ANTHROPIC_SAMPLING": "pass"})) as px:
        px.sync()
        st, hdrs, _ = px.post("/v1/chat/completions", {"model": "model-a", "messages": _user(),
                                                       "temperature": 0.2})
        assert srv.fake.last["body"]["temperature"] == 0.2 and "X-Tandem-Dropped" not in hdrs
    srv.shutdown()


def test_relay_tries_a_529_again_before_any_byte():
    srv, port = fake_anthropic.start(key=KEY)
    overloaded = {"status": 529, "body": {"type": "error", "error": {
        "type": "overloaded_error", "message": "Overloaded"}}}
    with Proxy(_env(port)) as px:
        px.sync()
        px.svc.relay.sleep = lambda s: None
        for stream in (True, False):
            srv.fake.queue.append(dict(overloaded))
            n0 = len(srv.fake.requests)
            st, _, data = px.post("/v1/chat/completions", {"model": "model-a",
                                                           "messages": _user(),
                                                           "stream": stream})
            assert st == 200 and len(srv.fake.requests) == n0 + 2, data
            if stream:
                assert data.count(b'"role":"assistant"') == 1 and data.endswith(
                    b"data: [DONE]\n\n")
            r = px.rows()[-1]
            assert r["retries"] == 1 and r["status"] == 200
        # overloaded on every try: the client gets OpenAI's error shape, 503, its Retry-After
        srv.fake.queue.extend(dict(overloaded) for _ in range(3))
        st, hdrs, data = px.post("/v1/chat/completions", {"model": "model-a",
                                                          "messages": _user()})
        e = json.loads(data)["error"]
        assert st == 503 and e["type"] == "overloaded_error" and hdrs.get("retry-after") == "0"
        assert px.rows()[-1]["finish_reason"] == "refused"
        # an upstream 400 (a model that refuses a forced tool) reaches the client as it is
        srv.fake.queue.append({"status": 400, "body": {"type": "error", "error": {
            "type": "invalid_request_error", "message": "tool_choice any is not supported"}}})
        st, _, data = px.post("/v1/chat/completions", {
            "model": "model-a", "messages": _user(), "tools": [TOOL],
            "tool_choice": "required"})
        assert st == 400 and json.loads(data)["error"]["message"] == \
            "tool_choice any is not supported"
    srv.shutdown()


def test_relay_an_error_in_the_stream():
    srv, port = fake_anthropic.start(key=KEY)
    with Proxy(_env(port)) as px:
        px.sync()
        srv.fake.queue.append({"message": {"content": [{"type": "text", "text": "abcdefgh"}],
                                           "stop_reason": "end_turn",
                                           "usage": {"input_tokens": 4, "output_tokens": 2}},
                               "error_after": 1})
        st, _, data = px.post("/v1/chat/completions", {"model": "model-a", "messages": _user(),
                                                       "stream": True})
        fr = _frames(data)
        assert st == 200 and fr[1]["choices"][0]["delta"]["content"] == "abcd"
        assert fr[-2]["error"]["type"] == "overloaded_error" and fr[-1] == "[DONE]"
        assert data.count(b"[DONE]") == 1
        r = px.rows()[-1]
        assert r["finish_reason"] == "error" and r["error_type"] == "upstream_stream_error"
    srv.shutdown()


def test_relay_a_tool_loop_of_three_turns():
    srv, port = fake_anthropic.start(key=KEY)
    with Proxy(_env(port)) as px:
        px.sync()
        msgs = _user("weather in two cities")
        for turn in range(3):
            st, _, data = px.post("/v1/chat/completions", {"model": "model-a", "messages": msgs,
                                                           "tools": [TOOL]})
            ch = json.loads(data)["choices"][0]
            assert st == 200, data
            if turn < 2:
                assert ch["finish_reason"] == "tool_calls"
                call = ch["message"]["tool_calls"][0]
                msgs = msgs + [ch["message"], {"role": "tool", "tool_call_id": call["id"],
                                               "content": f"result {turn + 1}"}]
            else:
                assert ch["finish_reason"] == "stop"
                assert ch["message"]["content"] == "done after 2 results"
        sent = srv.fake.last["body"]["messages"]
        assert [m["role"] for m in sent] == ["user", "assistant", "user", "assistant", "user"]
        assert sent[2]["content"][0] == {"type": "tool_result", "tool_use_id": "toolu_fake_1",
                                         "content": "result 1"}
        assert sent[3]["content"][1]["type"] == "tool_use" and \
            sent[3]["content"][1]["input"] == {"city": "City 2"}
    srv.shutdown()


def test_model_discovery_follows_after_id_across_three_pages():
    srv, port = fake_anthropic.start(key=KEY)
    with Proxy(_env(port, url_tail="/v1")) as px:
        px.sync()
        assert srv.fake.model_pages == 3
        assert [r["path"] for r in srv.fake.requests] == [
            "/v1/models?limit=1000", "/v1/models?limit=1000&after_id=model-b",
            "/v1/models?limit=1000&after_id=model-d"]
        st, _, data = px.get("/v1/models")
        cards = {m["id"]: m["tandem"] for m in json.loads(data)["data"]}
        assert sorted(cards) == ["model-a", "model-b", "model-c", "model-d", "model-e"]
        assert cards["model-c"]["name"] == "Model C" and cards["model-c"]["max_output"] == 32000
        assert px.svc.health.view("anth")["state"] == "up"
    # a wrong key: reachable, the key refused
    with Proxy(_env(port, {"TANDEM_BACKEND_ANTH_KEY": FIXTURE_KEYS[1]})) as px:
        px.sync()
        v = px.svc.health.view("anth")
        assert v["state"] == "down" and v["key_rejected"]
    srv.shutdown()


def test_settings_of_the_kind():
    srv, port = fake_anthropic.start(key=KEY)
    with Proxy(_env(port, {"TANDEM_BACKEND_ANTH_ANTHROPIC_THINKING_SHAPE": "budget"})) as px:
        b = px.svc.cfg.backend("anth")
        assert b["anthropic"] == {"thinking_shape": "budget"} and b["cloud"]
        st, _, data = px.post("/v1/config/backends", {
            "id": "a2", "kind": "anthropic", "url": f"http://127.0.0.1:{port}", "key": KEY,
            "anthropic": {"sampling": "pass", "api_version": "2026-10-21"}})
        assert st == 201, data
        assert px.svc.cfg.backend("a2")["anthropic"] == {"sampling": "pass",
                                                         "api_version": "2026-10-21"}
        st, _, data = px.request("PATCH", "/v1/config/backends/a2",
                                 {"anthropic": {"auto_cache": False}})
        assert st == 200 and px.svc.cfg.backend("a2")["anthropic"]["auto_cache"] is False
        st, _, data = px.request("PATCH", "/v1/config/backends/a2",
                                 {"anthropic": {"sampling": "maybe"}})
        assert st == 422 and "sampling" in data.decode()
        st, _, data = px.request("PATCH", "/v1/config/backends/anth",
                                 {"anthropic": {"thinking_shape": "off"}})
        assert st == 422 and "TANDEM_BACKEND_ANTH_ANTHROPIC_THINKING_SHAPE" in data.decode()
        st, _, data = px.get("/v1/config")
        k = json.loads(data)["kinds"]["anthropic"]
        assert k["cloud"] is True and k["url"] is None, "a base URL is always given"
        st, _, data = px.post("/v1/config/backends", {"id": "a3", "kind": "anthropic"})
        assert st == 422, data
        # the settings scope lists a kind's own keys on that kind's backends only
        from proxy import settingscopes
        spec = settingscopes.BackendsScope(px.svc.cfg).spec("controller")
        assert "backends.a2.anthropic.sampling" in spec
        assert spec["backends.anth.anthropic.thinking_shape"]["writable"] is False
    srv.shutdown()


if __name__ == "__main__":
    run(globals())
