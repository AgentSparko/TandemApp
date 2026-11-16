"""What an API token and an account used (SRV-873).

    GET /v1/auth/tokens/{id}/usage[?days=30&tz=]   one token (its owner, or the permission `users`)
    GET /v1/auth/usage[?user=<account id>&days=&tz=]  an account: yourself; another account
                                                      needs the permission `users`

Both read the ledger (index on token_id and time; the partial index on user) over the last `days`
local days (1 to 400, default 30) in `tz` (the page's, default Europe/Berlin):

    totals      requests, refused, prompt, cached and completion tokens, total, cost_micros
    days        the same per local day, every day of the range (zeros included)
    models      per model, the most tokens first, with `priced`: false when an answered row of
                the model had token counts and no cost (a model without a price: no spend limit
                applies to it; a row without counts, or a stream a limit cut, says nothing)
    unpriced    those models' ids
    refusals    the last 10 refused rows: when, the code (a limit's, a model the token may not
                use, ...) and the model
    last_used_at

A token's answer adds the token (never its secret) and `limits` (proxy/tokenlimits.py state):
per limit that holds, the limit, what is used, what is left and whether it is reached, and
`over`. An account's rows are its own requests: the ledger's `user` is its username (the page,
its tokens' memory owner) or one of its tokens. A token never calls these routes (the gate
refuses every /v1/auth/* route to a token); a token with `usage:read` reads the same rows through
the ledger views' `token=` filter, for its owner's tokens only (another's needs `users`).
"""

from __future__ import annotations

import datetime as _dt

from proxy import authz

NOSTORE = (("Cache-Control", "no-store"),)
MAX_DAYS = 400
LAST_REFUSALS = 10
_SUMS = ("COUNT(*), SUM(finish_reason = 'refused'), SUM(COALESCE(prompt_tokens, 0)), "
         "SUM(COALESCE(cached_tokens, 0)), SUM(COALESCE(completion_tokens, 0)), "
         "SUM(COALESCE(cost_micros, 0)), COUNT(cost_micros)")


def _err(handler, status: int, code: str, message: str, **facts) -> None:
    handler._json(status, {"error": {"type": code, "code": code, "message": message, **facts}},
                  extra_headers=NOSTORE)


def _sums(row) -> dict:
    n, ref, p, c, o, cost, cost_n = (int(x or 0) for x in row)
    return {"requests": n, "refused": ref, "prompt_tokens": p, "cached_tokens": c,
            "completion_tokens": o, "total_tokens": p + o,
            "cost_micros": cost if cost_n else None}


def _range(q: dict):
    from tandemapp import dashboard_api as dapi
    try:
        days = int(q.get("days") or 30)
    except ValueError:
        days = 0
    if not 1 <= days <= MAX_DAYS:
        raise dapi.ApiError(400, "bad_request", f"days is 1 to {MAX_DAYS}")
    tz_name = q.get("tz") or dapi.DEFAULT_TZ
    tz = dapi.zone(tz_name)
    return days, tz_name, tz


def usage(reader, where: str, args: list, q: dict, clock) -> dict:
    """The answer's ledger part for the rows `where` (SQL over `requests`) selects."""
    from tandemapp import dashboard_api as dapi
    days, tz_name, tz = _range(q)
    today = _dt.datetime.fromtimestamp(clock(), tz).date()
    first = today - _dt.timedelta(days=days - 1)
    edges = [dapi.local_midnight_ms(first + _dt.timedelta(days=i), tz) for i in range(days + 1)]
    out = {"from": first.isoformat(), "to": today.isoformat(), "tz": tz_name,
           "ledger": reader is not None, "totals": _sums((0,) * 7),
           "days": [{"date": (first + _dt.timedelta(days=i)).isoformat(), **_sums((0,) * 7)}
                    for i in range(days)],
           "models": [], "unpriced": [], "refusals": [], "last_used_at": None}
    db = reader() if reader is not None else None
    if db is None:
        return out
    span = f"({where}) AND ts_ms >= ? AND ts_ms < ?"
    sargs = list(args) + [edges[0], edges[-1]]
    out["totals"] = _sums(db.execute(f"SELECT {_SUMS} FROM requests WHERE {span}",
                                     sargs).fetchone())
    case = "CASE " + " ".join(f"WHEN ts_ms < {e} THEN {i}" for i, e in enumerate(edges[1:])) \
        + " END"
    for i, *sums in db.execute(f"SELECT {case} AS i, {_SUMS} FROM requests WHERE {span} "
                               f"GROUP BY i", sargs):
        if i is not None and 0 <= i < days:
            out["days"][i].update(_sums(sums))
    models = []
    for model, unpriced, *sums in db.execute(
            f"SELECT model, SUM(cost_micros IS NULL AND COALESCE(prompt_tokens, completion_tokens) "
            f"IS NOT NULL AND COALESCE(finish_reason, '') NOT IN ('refused', 'limit') AND "
            f"COALESCE(status, 0) < 400), {_SUMS} FROM requests WHERE {span} GROUP BY model",
            sargs):
        models.append({"model": model, "priced": not unpriced, **_sums(sums)})
    models.sort(key=lambda m: (-m["total_tokens"], -m["requests"], str(m["model"])))
    out["models"] = models
    out["unpriced"] = [m["model"] for m in models if not m["priced"]]
    out["refusals"] = [{"at": dapi.iso_utc(ts), "code": et or f"http_{st}", "status": st,
                        "model": model}
                       for ts, et, st, model in db.execute(
                           f"SELECT ts_ms, error_type, status, model FROM requests WHERE "
                           f"({where}) AND finish_reason IN ('refused', 'limit') ORDER BY ts_ms "
                           f"DESC LIMIT {LAST_REFUSALS}", list(args))]
    last = db.execute(f"SELECT MAX(ts_ms) FROM requests WHERE ({where}) AND "
                      f"COALESCE(finish_reason, '') != 'refused'", list(args)).fetchone()[0]
    out["last_used_at"] = dapi.iso_utc(last)
    return out


def token(handler, a, row: dict, q: dict) -> None:
    """GET /v1/auth/tokens/{id}/usage: tokenapi.py checked who may see the token."""
    from tandemapp import dashboard_api as dapi
    lim = a.token_limits
    try:
        out = usage(lim.reader if lim is not None else None, "token_id = ?", [row["id"]], q,
                    lim.clock if lim is not None else _now)
    except dapi.ApiError as exc:
        return _err(handler, exc.status, exc.kind, exc.message)
    owner = a.store.get(row["owner_id"]) if row["owner_id"] else None
    role = owner["role"] if owner else None
    out["token"] = a.tokens.view(row)
    out["limits"] = lim.state(row["id"], row["limits"], role) if lim is not None and \
        row["kind"] != "service" else {"limits": {}, "over": False, "refused_at": None}
    return handler._json(200, out, extra_headers=NOSTORE)


def account(handler, a, q: dict) -> None:
    """GET /v1/auth/usage[?user=<account id>]: your own, or (permission `users`) another's."""
    from tandemapp import dashboard_api as dapi
    p = handler._principal
    if p is None or p.kind != "account":
        return _err(handler, 403, "permission_denied", "usage belongs to a signed-in account",
                    permission="self")
    who = q.get("user") or p.id
    if who != p.id and not authz.allow(p, "users", a.policy):
        return _err(handler, 403, "permission_denied", "another account's usage needs the "
                    "permission users", permission="users")
    acc = a.store.get(who)
    if acc is None:
        return _err(handler, 404, "not_found", "no such account")
    ids = [r["id"] for r in a.tokens.mine(acc["id"])]
    where = "user = ?" + (f" OR token_id IN ({', '.join('?' * len(ids))})" if ids else "")
    lim = a.token_limits
    try:
        out = usage(lim.reader if lim is not None else None, where, [acc["username"]] + ids, q,
                    lim.clock if lim is not None else _now)
    except dapi.ApiError as exc:
        return _err(handler, exc.status, exc.kind, exc.message)
    out["user"] = {"id": acc["id"], "username": acc["username"], "role": acc["role"]}
    out["tokens"] = len(ids)
    return handler._json(200, out, extra_headers=NOSTORE)


def _now() -> float:
    import time
    return time.time()
