"""The stages of hooks-v1 (SRV-849): where a remote hook runs, what it may do, its budget.

contracts/hooks-v1/stages.json is this table for everyone outside the controller (a hook's author,
the hooks pages); tests/test_hooks_contract.py holds the two equal (`export()`).

    STAGES[name] = {when, may, actions, roles, budget_ms, sync, on_failure, fields, content,
                    decision, first_token, wired}

    when         one sentence: when the stage runs
    may          the design's words: observe, modify, deny, decide, choose fallback
    actions      the result actions a hook at this stage may answer (`continue` always)
    roles        the roles a hook may take here: transform (modify), guard (deny), decider
                 (decide), observer (nothing; it sees the final value)
    budget_ms    the stage's budget per request: every sync hook of the stage shares it, and a
                 hook's own timeout is at most this. None: an async stage
    sync         False: every hook here is an observer, called off the request's path
    on_failure   the default failure policy of a hook here (`open`, `closed`); `drop` for an
                 async stage. A guard defaults to `closed` everywhere
    fields       the fields a `patch` may set or remove (null removes)
    content      the subset of `fields` that holds what people wrote or the model answered: a
                 patch of one needs payload `full` (a hook that saw less cannot rewrite it)
    decision     the keys a `decision` may hold
    first_token  sync hooks here count toward the 800 ms cap before the first token
    wired        the controller calls the stage in this version; the others are called by the
                 work that owns them (decision.ask: decision providers; tool.*, memory.write,
                 session.*: chatd and the conductor; engine.lifecycle: engine control), through
                 `proxy.hooks.Runner.run` and `Runner.ask`
"""

from __future__ import annotations

VERSION = "1.0"
CAP_MS = 800                      # sync hooks before the first token, per request
ASYNC_TIMEOUT_MS = 5000           # an observer's default timeout off the request's path
MAX_TIMEOUT_MS = 30000
KINDS = ("http", "builtin", "plugin", "mcp", "inproc")
KINDS_NOW = ("http", "builtin")   # plugin (F1), mcp (F1/F2), inproc (E3) refuse until they exist
PAYLOADS = ("meta", "redacted", "full")
MODES = ("off", "shadow", "on")
POLICIES = ("open", "closed")
ROLES = ("transform", "guard", "decider", "observer")
ROLE_ACTIONS = {"transform": ("continue", "modify"), "guard": ("continue", "deny"),
                "decider": ("continue", "decide"), "observer": ("continue",)}
ACTIONS = ("continue", "modify", "deny", "decide")

# the request's sampling and output fields a hook may set at the request stages (not `stream`,
# `stream_options` or `n`: the relay reads them to frame the answer)
PARAMS = ("temperature", "top_p", "top_k", "min_p", "max_tokens", "max_completion_tokens",
          "max_output_tokens", "stop", "seed", "presence_penalty", "frequency_penalty",
          "repetition_penalty", "response_format", "reasoning_effort", "chat_template_kwargs",
          "tool_choice", "parallel_tool_calls", "logit_bias", "metadata", "user")
REQUEST_CONTENT = ("messages", "prompt", "input", "instructions", "tools")

STAGES: dict[str, dict] = {
    "request.received": {
        "when": "after auth, before routing",
        "may": ["modify", "deny"], "roles": ["transform", "guard", "observer"],
        "budget_ms": 150, "on_failure": "open",
        "fields": ["model", *PARAMS, *REQUEST_CONTENT], "content": list(REQUEST_CONTENT),
        "decision": [], "first_token": True, "wired": True},
    "route.select": {
        "when": "choose the backend, model and profile",
        "may": ["decide"], "roles": ["decider", "observer"],
        "budget_ms": 150, "on_failure": "open",
        "fields": [], "content": [], "decision": ["backend", "model", "profile"],
        "first_token": True, "wired": True},
    "request.before_engine": {
        "when": "the final body before it is sent to the backend",
        "may": ["modify", "deny"], "roles": ["transform", "guard", "observer"],
        "budget_ms": 250, "on_failure": "open",
        "fields": [*PARAMS, *REQUEST_CONTENT], "content": list(REQUEST_CONTENT),
        "decision": [], "first_token": True, "wired": True},
    "decision.ask": {
        "when": "a question for a decision provider",
        "may": ["decide"], "roles": ["decider", "observer"],
        "budget_ms": 400, "on_failure": "open",
        "fields": [], "content": [], "decision": ["answer"],
        "first_token": True, "wired": False},
    "stream.chunk": {
        "when": "each event of a streamed answer",
        "may": ["observe"], "roles": ["observer"],
        "budget_ms": None, "on_failure": "drop",
        "fields": [], "content": [], "decision": [], "first_token": False, "wired": True},
    "tool_call.detected": {
        "when": "the answer holds a tool call",
        "may": ["modify", "deny"], "roles": ["transform", "guard", "observer"],
        "budget_ms": 500, "on_failure": "open",
        "fields": ["tool_calls"], "content": ["tool_calls"], "decision": [],
        "first_token": True, "wired": True},
    "tool.before_exec": {
        "when": "chatd, before a tool runs",
        "may": ["deny", "modify"], "roles": ["transform", "guard", "observer"],
        "budget_ms": 500, "on_failure": "open",
        "fields": ["arguments"], "content": ["arguments"], "decision": [],
        "first_token": False, "wired": False},
    "tool.after_exec": {
        "when": "chatd, after a tool ran",
        "may": ["deny", "modify"], "roles": ["transform", "guard", "observer"],
        "budget_ms": 500, "on_failure": "open",
        "fields": ["result"], "content": ["result"], "decision": [],
        "first_token": False, "wired": False},
    "response.after_engine": {
        "when": "the whole answer",
        "may": ["modify", "deny", "retry"], "roles": ["transform", "guard", "decider", "observer"],
        "budget_ms": 1000, "on_failure": "open",
        "fields": ["choices", "output"], "content": ["choices", "output"], "decision": ["retry"],
        "first_token": True, "wired": True},
    "request.error": {
        "when": "an upstream error or timeout before any byte reached the client",
        "may": ["choose fallback"], "roles": ["decider", "observer"],
        "budget_ms": 250, "on_failure": "open",
        "fields": [], "content": [], "decision": ["fallback"],
        "first_token": True, "wired": True},
    "request.finished": {
        "when": "the ledger row is written",
        "may": ["observe"], "roles": ["observer"],
        "budget_ms": None, "on_failure": "drop",
        "fields": [], "content": [], "decision": [], "first_token": False, "wired": True},
    "memory.write": {
        "when": "a note about to be stored",
        "may": ["modify", "deny"], "roles": ["transform", "guard", "observer"],
        "budget_ms": 500, "on_failure": "open",
        "fields": ["text"], "content": ["text"], "decision": [],
        "first_token": False, "wired": False},
    "session.start": {
        "when": "a session starts",
        "may": ["observe", "modify"], "roles": ["transform", "observer"],
        "budget_ms": 250, "on_failure": "open",
        "fields": ["profile"], "content": [], "decision": [],
        "first_token": False, "wired": False},
    "session.compact": {
        "when": "a session is compacted",
        "may": ["observe", "modify"], "roles": ["transform", "observer"],
        "budget_ms": 250, "on_failure": "open",
        "fields": ["instruction"], "content": ["instruction"], "decision": [],
        "first_token": False, "wired": False},
    "engine.lifecycle": {
        "when": "an engine starts, stops, changes mode or applies settings",
        "may": ["observe"], "roles": ["observer"],
        "budget_ms": None, "on_failure": "drop",
        "fields": [], "content": [], "decision": [], "first_token": False, "wired": False},
}

for _name, _s in STAGES.items():
    _s["sync"] = _s["budget_ms"] is not None
    _s["actions"] = ["continue"] + sorted({a for r in _s["roles"] for a in ROLE_ACTIONS[r]}
                                          - {"continue"}, key=ACTIONS.index)
del _name, _s


def default_policy(stage: str, role: str) -> str:
    """A hook's failure policy when it names none: a guard's is closed, anything else the
    stage's (`drop` reads as open off the request's path)."""
    if role == "guard":
        return "closed"
    return "closed" if STAGES[stage]["on_failure"] == "closed" else "open"


def export() -> dict:
    """stages.json: the contract's form of this table."""
    keys = ("when", "may", "actions", "roles", "budget_ms", "sync", "on_failure", "fields",
            "content", "decision", "first_token", "wired")
    return {"contract": "hooks-v1", "version": VERSION, "cap_ms": CAP_MS,
            "async_timeout_ms": ASYNC_TIMEOUT_MS, "max_timeout_ms": MAX_TIMEOUT_MS,
            "kinds": list(KINDS), "kinds_available": list(KINDS_NOW),
            "payloads": list(PAYLOADS), "modes": list(MODES), "policies": list(POLICIES),
            "roles": {r: list(a) for r, a in ROLE_ACTIONS.items()},
            "stages": {n: {k: s[k] for k in keys} for n, s in STAGES.items()}}
