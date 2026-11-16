"""`/v1/fleet/hooks/*`: the hooks' admin API (SRV-849, SEC-16; the hooks pages are E5's).

    GET    /v1/fleet/hooks                       every hook with its secret's hint, where its
                                                 payload goes, its breaker and its figures
    GET    /v1/fleet/hooks/stages                the stage table (contracts/hooks-v1/stages.json)
                                                 and `samples`: each stage's sample event (the
                                                 playground's default, contracts/hooks-v1/
                                                 examples)
    POST   /v1/fleet/hooks                       create; an http hook's answer holds its secret,
                                                 once (201)
    GET    /v1/fleet/hooks/{id}
    PATCH  /v1/fleet/hooks/{id}                  change fields (the kind is fixed)
    DELETE /v1/fleet/hooks/{id}
    POST   /v1/fleet/hooks/{id}/enable, /disable
    PUT    /v1/fleet/hooks/{id}/mode             {mode: off|shadow|on}
    POST   /v1/fleet/hooks/{id}/test             {stage?, event?}: the hook once against the
                                                 stage's sample event (the playground's call);
                                                 nothing counted, nothing applied; the answer
                                                 holds the event the hook was sent
    GET    /v1/fleet/hooks/{id}/calls?limit=N    the journal, newest first (never a payload)
    GET    /v1/fleet/hooks/calls?request=<id>    one request's trace: every journaled call of
                                                 that request, across hooks and stages, in the
                                                 order the request met them
    POST   /v1/fleet/hooks/{id}/secret           {grace_s?}: a new secret, answered once; the
                                                 previous one signs beside it for grace_s

THE RIGHTS. Every route takes the configuration's writes (`conductor_write`: the admin's token or
session, or this machine; anyone while no admin password is set). Granting the full payload, moving
a full hook's endpoint and rotating a secret take pairing's (`fleet_trust`): with no admin password
set only this machine may, as pairing a node. A caller with the writes but not pairing's rights is
refused 403 `trust_required`, as the settings interface refuses the same grant; one without the
writes 401. A grant outside the LAN answers 409 confirm_needed with its `confirm`
(proxy/hookstore.py). No answer holds a secret but the two that make one.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from proxy import hookstages
from proxy.hookstore import HookError

CONTRACT = hookstages.VERSION


def view(runner, h: dict) -> dict:
    out = {k: h[k] for k in ("id", "name", "kind", "stages", "role", "priority", "mode", "scope",
                             "policy", "timeout_ms", "payload", "endpoint", "target", "options",
                             "enabled", "created_at", "updated_at", "updated_by", "version",
                             "grant")}
    out["policy_effective"] = {s: runner.policy(h, s) for s in h["stages"]}
    out["secret"] = runner.store.secret_hint(h["id"], h["secret_ref"]) if h["kind"] == "http" \
        else None
    try:
        out["destination"] = runner.store.destination(h)
    except Exception:                                              # noqa: BLE001
        out["destination"] = {"host": None, "lan": False}
    out["metrics"] = runner.metrics(h["id"])
    return out


def _needs_trust(runner, method: str, sub: list, body) -> bool:
    if len(sub) == 2 and sub[1] == "secret":
        return True
    if not isinstance(body, dict):
        return False
    if method == "POST" and not sub:
        return body.get("payload") == "full"
    if method == "PATCH" and len(sub) == 1:
        if body.get("payload") == "full":
            return True
        if "endpoint" in body:
            h = runner.store.get(sub[0])
            return h is not None and h["payload"] == "full"
    return False


def handle(handler, svc, method: str, sub: list[str], body) -> None:
    """One /v1/fleet/hooks request; `sub` is the path after /v1/fleet/hooks."""
    runner = svc.hooks
    if not handler._allowed("conductor_write"):
        return
    if _needs_trust(runner, method, sub, body):
        from proxy import app as app_mod
        a = app_mod.STATE.get("auth")
        if a is not None and a.decide("fleet_trust", handler._peer(), handler.headers) != "ok":
            return _send(handler, 403, trust_required())
    from proxy import config_api
    who = config_api._who(handler, svc)
    q = {k: v[-1] for k, v in parse_qs(urlsplit(handler.path).query).items()}
    try:
        code, out = _route(runner, method, sub, body, who, q)
    except HookError as exc:
        code, out = exc.status, exc.body()
    return _send(handler, code, out)


def trust_required(what: str = "the full payload and a hook's secret") -> dict:
    """The refusal of a write that takes pairing's rights to a caller without them (SEC-16)."""
    return {"error": {"type": "trust_required", "message": (
        f"{what} take pairing's rights: set an admin password (App settings) to do this "
        f"from another machine, or ask from this one")}}


def _send(handler, code: int, out) -> None:
    handler._json(code, out, extra_headers=(("Cache-Control", "no-store"),))


def _route(runner, method: str, sub: list[str], body, who: str, q: dict) -> tuple[int, dict]:
    st = runner.store
    if body is not None and not isinstance(body, dict):
        raise HookError(400, "bad_request", "the body must be a JSON object")
    body = body or {}
    if not sub:
        if method == "GET":
            return 200, {"contract_version": CONTRACT,
                         "hooks": [view(runner, h) for h in st.list()],
                         "kinds_available": list(hookstages.KINDS_NOW),
                         "builtins": sorted(runner.builtins)}
        if method == "POST":
            h, secret = st.create(body, who)
            out = {"hook": view(runner, h)}
            if secret is not None:
                out["secret"] = secret
            return 201, out
    elif sub == ["stages"] and method == "GET":
        from proxy import hooksample
        return 200, dict(hookstages.export(), samples=hooksample.samples())
    elif sub == ["calls"] and method == "GET":
        rid = q.get("request")
        if not rid or len(rid) > 200:
            raise HookError(400, "bad_request", "request: the id of a request (its "
                                                "X-Request-Id, or a journal row's `request`)")
        return 200, {"request": rid, "calls": runner.trace(rid)}
    elif len(sub) == 1:
        hid = sub[0]
        if method == "GET":
            return 200, {"hook": view(runner, st.need(hid))}
        if method == "PATCH":
            return 200, {"hook": view(runner, st.update(hid, body, who))}
        if method == "DELETE":
            st.delete(hid, who)
            return 200, {"deleted": hid}
    elif len(sub) == 2:
        hid, act = sub
        if act in ("enable", "disable") and method == "POST":
            return 200, {"hook": view(runner, st.update(hid, {"enabled": act == "enable"},
                                                        who))}
        if act == "mode" and method in ("PUT", "POST"):
            if "mode" not in body:
                raise HookError(422, "invalid", "mode: off, shadow or on", "mode")
            return 200, {"hook": view(runner, st.update(hid, {"mode": body["mode"]}, who))}
        if act == "test" and method == "POST":
            ev = body.get("event")
            if ev is not None and not isinstance(ev, dict):
                raise HookError(422, "invalid", "event: an object", "event")
            stage = body.get("stage")
            if stage is not None and stage not in hookstages.STAGES:
                raise HookError(422, "invalid", f"stage: no stage {stage!r}", "stage")
            return 200, runner.test(hid, stage, dict(ev) if ev else None)
        if act == "calls" and method == "GET":
            st.need(hid)
            try:
                limit = max(1, min(500, int(q.get("limit", 100))))
            except ValueError:
                raise HookError(400, "bad_request", "limit: a number 1..500")
            return 200, {"hook": hid, "calls": runner.calls(hid, limit),
                         "metrics": runner.metrics(hid)}
        if act == "secret" and method == "POST":
            secret = st.rotate(hid, who, body.get("grace_s", 0))
            return 200, {"hook": view(runner, st.need(hid)), "secret": secret}
    raise HookError(404, "not_found", f"no route {method} /v1/fleet/hooks/{'/'.join(sub)}")
