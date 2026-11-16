"""`/v1/fleet/decision-providers/*`: the decision providers' admin API (SRV-851).

    GET    /v1/fleet/decision-providers              every provider (its keys' hints, its
                                                     breakers), the question ids and the kinds
    POST   /v1/fleet/decision-providers              create (201)
    GET    /v1/fleet/decision-providers/report       ?hours=24&provider=: per provider and
                                                     question: calls, agreement, p50/p95,
                                                     failures, fallbacks, per mode and A/B arm
    GET    /v1/fleet/decision-providers/{id}         ?health=1 adds the health view
    PATCH  /v1/fleet/decision-providers/{id}         change fields (the kind is fixed); a key
                                                     is set with a string and cleared with null
    DELETE /v1/fleet/decision-providers/{id}
    POST   /v1/fleet/decision-providers/{id}/enable, /disable
    PUT    /v1/fleet/decision-providers/{id}/mode    {mode, ab_percent?, sticky?}
    POST   /v1/fleet/decision-providers/{id}/test    {question?, text?}: one live call with a
                                                     sample question; nothing counted
    GET    /v1/fleet/decision-providers/{id}/health  ?force=1 probes now (else cached 10 s)
    GET    /v1/fleet/decision-providers/{id}/calls   ?limit=N: this process's recent calls

THE RIGHTS. Every route takes the configuration's writes (`conductor_write`), as the hooks' and
the backends' (a backend's key is set the same way). No answer, log line or audit row holds a
key: the views carry its hint (set, last 4 characters, a sha256 prefix).

The settings interface (D1, settings-v1) reads and writes these through this module's store; the
adapter is its work, not this one's.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from proxy import providerstore
from proxy.providerstore import ProviderError

CONTRACT = "1.0"


def handle(handler, svc, method: str, sub: list[str], body) -> None:
    """One /v1/fleet/decision-providers request; `sub` is the path after it."""
    if not handler._allowed("conductor_write"):
        return
    from proxy import config_api
    who = config_api._who(handler, svc)
    q = {k: v[-1] for k, v in parse_qs(urlsplit(handler.path).query).items()}
    try:
        code, out = _route(svc.providers, method, sub, body, who, q)
    except ProviderError as exc:
        code, out = exc.status, exc.body()
    handler._json(code, out, extra_headers=(("Cache-Control", "no-store"),))


def _flag(q: dict, k: str) -> bool:
    return str(q.get(k, "")).lower() in ("1", "true", "yes")


def _route(ps, method: str, sub: list[str], body, who: str, q: dict) -> tuple[int, dict]:
    st = ps.store
    if body is not None and not isinstance(body, dict):
        raise ProviderError(400, "bad_request", "the body must be a JSON object")
    body = body or {}
    if not sub:
        if method == "GET":
            return 200, {"contract_version": CONTRACT,
                         "providers": [ps.view(p) for p in st.list()],
                         "questions": {k: {"what": v} for k, v in
                                       providerstore.QUESTIONS.items()},
                         "kinds": list(providerstore.KINDS), "modes": list(providerstore.MODES),
                         "sticky": list(providerstore.STICKY),
                         "replay": ps.replay is not None}
        if method == "POST":
            return 201, {"provider": ps.view(st.create(body, who))}
    elif sub == ["report"] and method == "GET":
        try:
            hours = float(q.get("hours", 24))
        except ValueError:
            raise ProviderError(400, "bad_request", "hours: a number 0.01..720")
        if not 0.01 <= hours <= 720:
            raise ProviderError(400, "bad_request", "hours: a number 0.01..720")
        pid = q.get("provider")
        if pid is not None:
            st.need(pid)
        return 200, dict(ps.report(pid, hours), contract_version=CONTRACT)
    elif len(sub) == 1:
        pid = sub[0]
        if method == "GET":
            return 200, {"provider": ps.view(st.need(pid), health=_flag(q, "health"))}
        if method == "PATCH":
            return 200, {"provider": ps.view(st.update(pid, body, who))}
        if method == "DELETE":
            st.delete(pid, who)
            return 200, {"deleted": pid}
    elif len(sub) == 2:
        pid, act = sub
        if act in ("enable", "disable") and method == "POST":
            return 200, {"provider": ps.view(st.update(pid, {"enabled": act == "enable"},
                                                       who))}
        if act == "mode" and method in ("PUT", "POST"):
            if "mode" not in body or set(body) - {"mode", "ab_percent", "sticky"}:
                raise ProviderError(422, "invalid", "{mode: on|shadow|ab, ab_percent?, "
                                                    "sticky?}", "mode")
            return 200, {"provider": ps.view(st.update(pid, body, who))}
        if act == "test" and method == "POST":
            if set(body) - {"question", "text"}:
                raise ProviderError(422, "invalid", "{question?, text?}")
            return 200, ps.test(pid, body.get("question"), body.get("text"))
        if act == "health" and method == "GET":
            return 200, ps.health(pid, force=_flag(q, "force"))
        if act == "calls" and method == "GET":
            st.need(pid)
            try:
                limit = max(1, min(500, int(q.get("limit", 100))))
            except ValueError:
                raise ProviderError(400, "bad_request", "limit: a number 1..500")
            return 200, {"provider": pid, "calls": ps.calls(pid, limit)}
    raise ProviderError(404, "not_found",
                        f"no route {method} /v1/fleet/decision-providers/{'/'.join(sub)}")
