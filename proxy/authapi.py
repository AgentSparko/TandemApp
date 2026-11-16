"""The sign-in, session and account routes (SEC-22, SEC-24), with TANDEM_AUTH=accounts only.

    GET    /v1/auth/session            who am I: account, role, permissions, the CSRF token, the
                                       provider and its capabilities, setup_required, local_mode
    POST   /v1/auth/session            {username, password} -> the session cookie
    DELETE /v1/auth/session            sign out
    PUT    /v1/auth/password           {current, password}: every session of the account ends;
                                       this browser gets a new one
    GET    /v1/auth/sessions[?user=]   the live sessions (another account's: permission `users`)
    DELETE /v1/auth/sessions[?user=]   sign out everywhere (another account: `users`)
    DELETE /v1/auth/sessions/{id}      end one session (another account's: `users`)
    GET    /v1/auth/users              the accounts (permission `users`)
    POST   /v1/auth/users              {username, role, password, display_name?}
    PATCH  /v1/auth/users/{id}         {role?, enabled?, display_name?}
    DELETE /v1/auth/users/{id}

Only an owner makes, changes or removes an owner. The last enabled owner cannot be disabled,
demoted or removed (409 `last_owner`, checked first). A disable, a lower role and a removal end
the account's sessions. A wrong password and an unknown name get the same 401 and cost the same
one hash. No answer, log line or audit row holds a password, a hash, a salt, a session id or a
CSRF token.

The old sign-in routes answer over the accounts while the switch is on: GET
/v1/dashboard/session and /v1/fleet/session say whether this browser is signed in, their POST
and /v1/chat-app/session's send a person to /v1/auth/session, their DELETE signs out, and
/v1/fleet/app/password's writes send a person to /v1/auth/password.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, unquote, urlsplit

from proxy import accounts as acc_mod
from proxy import authz
from proxy import identity as identity_mod
from tandemapp import auth as auth_mod

CONTRACT = "1.0"
NOSTORE = (("Cache-Control", "no-store"),)
WRONG = {"error": {"type": "unauthorized", "code": "wrong_credentials",
                   "message": "the username or the password is not right"}}


def _iso(ms: int | None) -> str | None:
    if not ms:
        return None
    from tandemapp import dashboard_api
    return dashboard_api.iso_utc_s(int(ms // 1000))


def _err(handler, status: int, code: str, message: str, extra: tuple = (), **facts) -> None:
    handler._json(status, {"error": {"type": code, "code": code, "message": message, **facts}},
                  extra_headers=NOSTORE + tuple(extra))


def _body(raw: bytes | None):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        raise acc_mod.AccountError(400, "bad_request", "the body is not JSON")


def _account_view(row: dict) -> dict:
    return {"id": row["id"], "username": row["username"], "display_name": row["display_name"],
            "role": row["role"], "must_change": row["must_change"]}


def _session_answer(handler, a, p) -> dict:
    acc = a.store.get(p.id) if p is not None and p.kind == "account" else None
    return {"contract_version": CONTRACT, "authenticated": p is not None,
            "account": _account_view(acc) if acc else None,
            "principal": None if p is None else {"kind": p.kind, "name": p.name},
            "permissions": a.permissions(p), "csrf": p.csrf if p is not None else None,
            "provider": {"kind": a.provider.kind, "capabilities": a.provider.capabilities()},
            "setup_required": a.store.setup_required(), "local_mode": False,
            "expires_at": _iso(p.expires_at) if p is not None else None}


def handle(handler, a, method: str, path: str, raw: bytes | None) -> None:
    """One /v1/auth/* request; the gate (proxy/accounts.py) has let it in."""
    parts = [unquote(x) for x in path.rstrip("/").split("/")[3:]]
    q = {k: v[-1] for k, v in parse_qs(urlsplit(handler.path).query).items()}
    try:
        body = _body(raw)
        if parts == ["session"]:
            return _session(handler, a, method, body)
        if parts[:1] == ["sessions"]:
            return _sessions(handler, a, method, parts[1:], q)
        if parts == ["password"] and method == "PUT":
            return _password(handler, a, body)
        if parts[:1] == ["users"]:
            return _users(handler, a, method, parts[1:], body)
    except acc_mod.AccountError as exc:
        return handler._json(exc.status, exc.body(), extra_headers=NOSTORE)
    return _err(handler, 404, "not_found", f"no route {method} {path}")


# ------------------------------------------------------------------------------ sign-in
def _session(handler, a, method: str, body) -> None:
    p = handler._principal
    if method == "GET":
        return handler._json(200, _session_answer(handler, a, p), extra_headers=NOSTORE)
    if method == "DELETE":
        return _sign_out(handler, a, p)
    if method != "POST":
        return _err(handler, 405, "method_not_allowed", "the session takes GET, POST and DELETE")
    bad = auth_mod.same_origin_json(handler.headers)
    if bad is not None:
        return _err(handler, bad[0], bad[1], "a sign-in comes from this server's own page, as "
                                             "JSON")
    if not isinstance(body, dict) or set(body) - {"username", "password"}:
        return _err(handler, 400, "bad_request", "the body is {username, password}")
    peer = handler._peer()
    address = a.limits.key(peer, handler.headers)
    name = body.get("username")
    key = name.strip().lower()[:64] if isinstance(name, str) else ""
    wait = a.limits.wait(address, key)
    if wait:
        return _err(handler, 429, "too_many", "too many failed sign-ins; wait and try again",
                    extra=(("Retry-After", str(wait)),))
    got = a.provider.authenticate(key, body.get("password"))
    if isinstance(got, identity_mod.Denied) and got.reason in ("held", "busy"):
        return _err(handler, 429, "too_many", "too many sign-ins at once; wait and try again",
                    extra=(("Retry-After", "60" if got.reason == "held" else "1"),))
    if not isinstance(got, identity_mod.Identity):
        a.limits.failed(address, key)
        a.cfg.log_audit(acc_mod._clean(key, 64) or "-", "sign_in_failed", "session", None,
                        detail={"address": acc_mod._clean(address, 64)})
        return handler._json(401, WRONG, extra_headers=NOSTORE)
    row = a.store.by_username(got.username)
    a.limits.succeeded(key)
    sid, srow = a.store.open_session(row["id"], address, handler.headers.get("User-Agent") or "")
    a.store.mark_login(row["id"])
    a.cfg.log_audit(row["username"], "sign_in", "session", acc_mod.public_id(srow["id_hash"]),
                    detail={"address": acc_mod._clean(address, 64)})
    cookie = a.cookie(sid, peer, handler.headers)
    return handler._json(200, {"contract_version": CONTRACT, "account": _account_view(row),
                               "csrf": acc_mod.csrf_of(sid),
                               "expires_at": _iso(min(srow["idle_until"], srow["expires_at"]))},
                         extra_headers=NOSTORE + (("Set-Cookie", cookie),))


def _sign_out(handler, a, p) -> None:
    clear = a.cookie(None, handler._peer(), handler.headers)
    if p is not None and p.session:
        a.store.revoke([p.session], actor=p.name, why="signed out")
        a.cfg.log_audit(p.name, "sign_out", "session", acc_mod.public_id(p.session))
    return handler._send_empty(204, NOSTORE + (("Set-Cookie", clear),))


# ------------------------------------------------------------------------------ sessions
def _may_users(a, p) -> bool:
    return authz.allow(p, "users", a.policy)


def _sessions(handler, a, method: str, rest: list, q: dict) -> None:
    p = handler._principal
    if p is None or p.kind != "account":
        return _err(handler, 403, "permission_denied", "sessions belong to an account",
                    permission="self")
    if method == "GET" and not rest:
        whose = q.get("user") or p.id
        if whose != p.id and not _may_users(a, p):
            return _err(handler, 403, "permission_denied", "another account's sessions need "
                        "the permission users", permission="users")
        if q.get("user") == "*" and _may_users(a, p):
            rows = a.store.sessions(None)
        else:
            rows = a.store.sessions(whose)
        out = [{**{k: v for k, v in r.items() if k != "_id_hash"},
                "created_at": _iso(r["created_at"]), "last_seen_at": _iso(r["last_seen_at"]),
                "idle_until": _iso(r["idle_until"]), "expires_at": _iso(r["expires_at"]),
                "current": r["_id_hash"] == p.session} for r in rows]
        return handler._json(200, {"contract_version": CONTRACT, "sessions": out},
                             extra_headers=NOSTORE)
    if method != "DELETE" or len(rest) > 1:
        return _err(handler, 404, "not_found", "no such sessions route")
    if rest:
        row = a.store.find_session(rest[0])
        if row is None:
            return _err(handler, 404, "not_found", "no such session")
        if row["account_id"] != p.id and not _may_users(a, p):
            return _err(handler, 403, "permission_denied", "another account's session needs the "
                        "permission users", permission="users")
        a.store.revoke([row["id_hash"]], actor=p.name,
                       why="ended" if row["account_id"] == p.id else f"ended by {p.name}")
        if row["id_hash"] == p.session:
            return handler._send_empty(204, NOSTORE + (("Set-Cookie", a.cookie(
                None, handler._peer(), handler.headers)),))
        return handler._send_empty(204, NOSTORE)
    whose = q.get("user") or p.id
    if whose != p.id and not _may_users(a, p):
        return _err(handler, 403, "permission_denied", "another account's sessions need the "
                    "permission users", permission="users")
    if a.store.get(whose) is None:
        return _err(handler, 404, "not_found", "no such account")
    a.store.revoke_account(whose, actor=p.name, why="signed out everywhere" if whose == p.id
                           else f"ended by {p.name}")
    if whose == p.id:
        return handler._send_empty(204, NOSTORE + (("Set-Cookie", a.cookie(
            None, handler._peer(), handler.headers)),))
    return handler._send_empty(204, NOSTORE)


# ------------------------------------------------------------------------------ password
def _password(handler, a, body) -> None:
    p = handler._principal
    if p is None or p.kind != "account":
        return _err(handler, 403, "permission_denied", "a password belongs to an account",
                    permission="self")
    if not isinstance(body, dict) or set(body) - {"current", "password"} or \
            "password" not in body:
        return _err(handler, 400, "bad_request", "the body is {current, password}")
    peer = handler._peer()
    address = a.limits.key(peer, handler.headers)
    wait = a.limits.wait(address, p.name)
    if wait:
        return _err(handler, 429, "too_many", "too many wrong passwords; wait and try again",
                    extra=(("Retry-After", str(wait)),))
    got = a.provider.authenticate(p.name, body.get("current"))
    if isinstance(got, identity_mod.Denied) and got.reason in ("held", "busy"):
        return _err(handler, 429, "too_many", "too many checks at once; wait and try again",
                    extra=(("Retry-After", "60" if got.reason == "held" else "1"),))
    if not isinstance(got, identity_mod.Identity):
        a.limits.failed(address, p.name)
        return _err(handler, 403, "wrong_password", "the current password is not right")
    a.store.set_password(p.id, body["password"], actor=p.name)
    a.store.revoke_account(p.id, actor=p.name, why="password changed")
    sid, srow = a.store.open_session(p.id, address, handler.headers.get("User-Agent") or "")
    return handler._json(200, {"contract_version": CONTRACT, "csrf": acc_mod.csrf_of(sid),
                               "expires_at": _iso(min(srow["idle_until"], srow["expires_at"]))},
                         extra_headers=NOSTORE + (("Set-Cookie", a.cookie(sid, peer,
                                                                          handler.headers)),))


# ------------------------------------------------------------------------------ users
def _users(handler, a, method: str, rest: list, body) -> None:
    p = handler._principal
    owner = authz.allow(p, "owner", a.policy)
    st = a.store
    if not rest:
        if method == "GET":
            return handler._json(200, {"contract_version": CONTRACT,
                                       "users": [st.view(r) for r in st.all()]},
                                 extra_headers=NOSTORE)
        if method != "POST":
            return _err(handler, 405, "method_not_allowed", "the users take GET and POST")
        if not isinstance(body, dict) or set(body) - {"username", "role", "password",
                                                      "display_name"} or \
                "password" not in body:
            # the one-time set-password code comes with the first-run setup
            return _err(handler, 400, "bad_request", "the body is {username, role, password, "
                                                     "display_name?}")
        if body.get("role") == "owner" and not owner:
            return _err(handler, 403, "permission_denied", "only an owner makes an owner",
                        permission="owner")
        row = st.create(body.get("username"), body.get("role"), body["password"],
                        display_name=body.get("display_name"), actor=p.name)
        return handler._json(201, {"contract_version": CONTRACT, "user": st.view(row)},
                             extra_headers=NOSTORE)
    if len(rest) != 1:
        return _err(handler, 404, "not_found", "no such users route")
    target = st.get(rest[0])
    if target is None:
        return _err(handler, 404, "not_found", "no such account")
    if method == "PATCH":
        if not isinstance(body, dict):
            return _err(handler, 400, "bad_request", "the body is {role?, enabled?, "
                                                     "display_name?}")
        role = body.get("role", target["role"])
        enabled = body.get("enabled", target["enabled"])
        if st._last_owner(target) and (role != "owner" or enabled is False):
            return _err(handler, 409, "last_owner", "this is the last enabled owner: make "
                                                    "another owner first")
        if (target["role"] == "owner" or role == "owner") and not owner:
            return _err(handler, 403, "permission_denied", "only an owner changes an owner",
                        permission="owner")
        row, ended = st.update(target["id"], body, actor=p.name)
        if ended and not row["enabled"] and a.on_account_removed is not None:
            a.on_account_removed(row["id"])
        return handler._json(200, {"contract_version": CONTRACT, "user": st.view(row),
                                   "sessions_ended": ended}, extra_headers=NOSTORE)
    if method == "DELETE":
        if st._last_owner(target):
            return _err(handler, 409, "last_owner", "this is the last enabled owner: make "
                                                    "another owner first")
        if target["role"] == "owner" and not owner:
            return _err(handler, 403, "permission_denied", "only an owner removes an owner",
                        permission="owner")
        st.remove(target["id"], actor=p.name)
        if a.on_account_removed is not None:
            a.on_account_removed(target["id"])
        return handler._send_empty(204, NOSTORE)
    return _err(handler, 405, "method_not_allowed", "a user takes PATCH and DELETE")


# ------------------------------------------------------------------------------ the old routes
def legacy_session(handler, a, method: str, fleet: bool) -> None:
    """/v1/dashboard/session (fleet False) and /v1/fleet/session over the accounts."""
    p = handler._principal
    if method == "GET":
        if fleet:
            from proxy import fleet as fleet_mod
            return handler._json(200, {"contract_version": fleet_mod.CONTRACT,
                                       "password_set": True, "authenticated": p is not None,
                                       "expires_at": _iso(p.expires_at) if p else None},
                                 extra_headers=NOSTORE)
        if p is None:
            return _err(handler, 401, "unauthorized", "sign in at /v1/auth/session",
                        extra=(("WWW-Authenticate", 'Bearer realm="TandemLLM"'),))
        from tandemapp import dashboard_api
        return handler._json(200, {"contract_version": dashboard_api.SESSION_CONTRACT,
                                   "authenticated": True, "login": True, "password_set": True,
                                   "expires_at": _iso(p.expires_at)}, extra_headers=NOSTORE)
    if method == "DELETE":
        return _sign_out(handler, a, p)
    return _err(handler, 404, "not_found", "sign in at /v1/auth/session with a username and "
                                           "a password")


def legacy_password(handler) -> None:
    return _err(handler, 404, "not_found", "change a password at /v1/auth/password")
