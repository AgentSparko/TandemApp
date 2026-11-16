"""The API token routes (SRV-867, proxy/tokens.py).

    GET    /v1/auth/tokens[?all=1]       your tokens; with all=1 every token (permission `users`),
                                         the service identities and the imported keys under review
    POST   /v1/auth/tokens               {label, scopes, models?, days? (a number or "never"),
                                         limits?}: 201 with the token, shown this once
    GET    /v1/auth/tokens/{id}          one token (never its secret)
    PATCH  /v1/auth/tokens/{id}          {label?, models?, limits?} (its owner; limits under the
                                         owner's role, SRV-873); {needs_review: false} confirms
                                         an imported key (permission `users`)
    DELETE /v1/auth/tokens/{id}          revoke
    POST   /v1/auth/tokens/{id}/rotate   {grace_s?}: a new secret under the same id and label;
                                         the old one works grace_s more seconds (default 0)
    GET    /v1/auth/tokens/{id}/usage    what it used, per day and per model, its last refusals
                                         and its limits' state (SRV-873, proxy/tokenusage.py)

A signed-in account only: a token never manages tokens (the gate refuses it), and local mode makes
none (a token is for another device: set the owner's password first). A person manages their own
tokens; an admin also confirms or revokes an imported key and revokes another account's token,
but never rotates or renames it. Only an owner touches an owner's token, but for the imported
admin token, which every admin may confirm or revoke. A service identity is read-only here
(SRV-869), but for an imported one (the metrics token, the shared collect token), which an admin
confirms or revokes under Review.
Every answer is `Cache-Control: no-store`.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from proxy import authz
from proxy import tokens as tokens_mod

NOSTORE = (("Cache-Control", "no-store"),)


def _err(handler, status: int, code: str, message: str, **facts) -> None:
    handler._json(status, {"error": {"type": code, "code": code, "message": message, **facts}},
                  extra_headers=NOSTORE)


def handle(handler, a, method: str, rest: list, body) -> None:
    p = handler._principal
    if p is not None and p.kind == "local":
        return _err(handler, 409, "password_first", "set the owner's password first: local "
                    "mode trusts this machine only, and a token is for another device")
    if p is None or p.kind != "account":
        return _err(handler, 403, "permission_denied", "tokens belong to a signed-in account",
                    permission="self")
    ts = a.tokens
    try:
        if not rest:
            if method == "GET":
                q = {k: v[-1] for k, v in parse_qs(urlsplit(handler.path).query).items()}
                if q.get("all") in ("1", "true"):
                    if not authz.allow(p, "users", a.policy):
                        return _err(handler, 403, "permission_denied", "every token needs the "
                                    "permission users", permission="users")
                    rows = ts.all()
                else:
                    rows = ts.mine(p.id)
                return handler._json(200, {"tokens": [ts.view(r) for r in rows]},
                                     extra_headers=NOSTORE)
            if method != "POST":
                return _err(handler, 405, "method_not_allowed", "the tokens take GET and POST")
            if not isinstance(body, dict) or set(body) - {"label", "scopes", "models", "days",
                                                          "limits"}:
                return _err(handler, 400, "bad_request", "the body is {label, scopes, models?, "
                            "days?, limits?}")
            view, token = ts.create(a.store.get(p.id), label=body.get("label"),
                                    scopes=body.get("scopes"), models=body.get("models"),
                                    days=body.get("days"), limits=body.get("limits"),
                                    actor=p.name)
            return handler._json(201, {**view, "token": token}, extra_headers=NOSTORE)
        row = ts.get(rest[0])
        if row is None or len(rest) > 2:
            return _err(handler, 404, "not_found", "no such token")
        mine = row["owner_id"] == p.id
        admin = authz.allow(p, "users", a.policy)
        owner = authz.allow(p, "owner", a.policy)
        tok_owner = a.store.get(row["owner_id"]) if row["owner_id"] else None
        others_ok = admin and (owner or tok_owner is None or tok_owner["role"] != "owner"
                               or row["source"] == "admin_token")
        if not mine and not others_ok:
            # someone else's token: as if it were not there
            return _err(handler, 404, "not_found", "no such token")
        if len(rest) == 2 and rest[1] == "usage" and method == "GET":
            from proxy import tokenusage
            q = {k: v[-1] for k, v in parse_qs(urlsplit(handler.path).query).items()}
            return tokenusage.token(handler, a, row, q)
        if len(rest) == 2:
            if rest[1] != "rotate" or method != "POST":
                return _err(handler, 404, "not_found", "no such token route")
            if not mine:
                return _err(handler, 403, "permission_denied", "only its owner rotates a token")
            grace = (body or {}).get("grace_s", 0) if isinstance(body, dict) or body is None \
                else None
            view, token = ts.rotate(row["id"], grace_s=grace, actor=p.name)
            return handler._json(200, {**view, "token": token}, extra_headers=NOSTORE)
        if method == "GET":
            return handler._json(200, ts.view(row), extra_headers=NOSTORE)
        if row["kind"] == "service" and not (row["source"] in tokens_mod.SERVICE_IMPORTS
                                             and admin):
            # SRV-869: listed read-only; an imported one (the metrics or the shared collect
            # token) an admin confirms or revokes under Review
            return _err(handler, 409, "service_identity", "a service identity is managed by "
                        "TandemApp itself")
        if method == "DELETE":
            return handler._json(200, ts.revoke(row["id"], actor=p.name,
                                                why="revoked" if mine else f"revoked by {p.name}"),
                                 extra_headers=NOSTORE)
        if method == "PATCH":
            if not isinstance(body, dict) or not body:
                return _err(handler, 400, "bad_request", "the body is {label?, models?, "
                            "limits?} or {needs_review: false}")
            if "needs_review" in body:
                if body != {"needs_review": False}:
                    return _err(handler, 400, "bad_request", "confirm with {needs_review: "
                                "false} alone")
                if not admin:
                    return _err(handler, 403, "permission_denied", "an admin confirms an "
                                "imported key", permission="users")
                return handler._json(200, ts.confirm(row["id"], actor=p.name),
                                     extra_headers=NOSTORE)
            if not mine:
                return _err(handler, 403, "permission_denied", "only its owner changes a token")
            return handler._json(200, ts.update(row["id"], body, actor=p.name),
                                 extra_headers=NOSTORE)
        return _err(handler, 405, "method_not_allowed", "a token takes GET, PATCH and DELETE")
    except tokens_mod.TokenError as exc:
        return handler._json(exc.status, exc.body(), extra_headers=NOSTORE)
