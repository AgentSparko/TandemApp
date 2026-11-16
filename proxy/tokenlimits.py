"""Limits per API token (SRV-873), with TANDEM_AUTH=accounts only.

A token's `limits` (proxy/tokens.py LIMIT_KEYS) hold at each request that reaches a model:

    requests_per_minute   requests let in during the last 60 s          429, Retry-After
    tokens_per_minute     prompt + completion tokens of the requests     429, Retry-After
                          that arrived in the last 60 s
    concurrent            requests of the token in flight now            429, Retry-After: 1
    cost_per_day          dollars since local midnight                   403 spend_limit_reached
    cost_per_month        dollars since the 1st of the local month       403 spend_limit_reached

A spend limit is a 403, because SDKs retry a 429 on their own. "Local" is the controller's own
clock and time zone, as the backends' cost of today (Service.cost_today).

THE LIMIT THAT HOLDS is the lower of the token's own and the one of its owner's role of now
(`api.limits.default.<role>.<key>` in the `api` settings scope, written by an admin; unset or 0:
none), so a token never has more than its owner's role allows, and a lowered role default holds
for the tokens made before it. An imported key without an owner has a viewer's. A service
identity has none. A new token takes its owner's role's values where it names none, and may not
name more (TokenStore.create, check_role_limits).

WHERE (proxy/service.py Service.serve). Once per request, before its hooks, its harness and the
relay: the chat and text completions, embeddings, responses and the Messages API (translated or
passed through). `/v1/messages/count_tokens` and the cache routes generate nothing and are not
counted. A refusal is an answer in the caller's protocol (OpenAI: {"error": {type, code,
message}}; the Messages API's handler turns it into `rate_limit_error` with `retry-after`, or
`permission_error`) and a ledger row with the token and the reason: finish `refused`,
error_type `rate_limited`, `tokens_limited`, `concurrency_limited` or `spend_limit_reached`.

SPEND is the ledger's cost (`cost_micros`: the model's price, a cache write paid once). A model
without a price costs nothing: its requests pass the spend limits (the rate limits still hold),
and the token's usage names it (proxy/tokenusage.py `unpriced`).

MID-STREAM. A request let in runs. The one exception: a stream of a token with a spend limit is
watched while it runs (`watch`, every WATCH_FRAMES frames). When the token's spend of the day or
the month, with the live cost of its streams in flight (their completion frames at the model's
price), passes the limit, the stream ends as the protocol ends one with an error: an OpenAI
error frame then `data: [DONE]`, a Messages API `error` event (permission_error); the backend's
connection is closed, and the row says finish `limit`, error spend_limit_reached, and as its cost
the live figure (the completion frames at the model's price; the stream sent no usage): the one
estimate in the ledger, so a restart still counts the spend. Every other stream's bytes are
untouched.

HOW FAR A LIMIT CAN BE PASSED. Requests a minute and requests at once never: a request is let in
under one lock. Tokens a minute and spend are known when a request ends, so the requests in
flight can pass them, each by its own size at most (a watched stream by a few frames).

COUNTERS live in memory, under one lock; one replica runs (the chart's rule). The first request
of a token after a start reads its counters from the ledger once, outside the lock (the index on
token_id, time): the day's and the month's cost as two sums, the last minute's rows. From then on they are
kept here, never read again: the ledger's writer is queued, so a re-read could miss a row or
count one twice. Day and month roll over at local midnight; a request's cost counts in the
period it arrived in, as its ledger row does.

    TokenLimits(policy, reader=None, clock=time.time)
      caps(role) -> {key: value}               the role's limits (the settings)
      effective(limits, role) -> {key: value}  the lower of the token's and the role's
      admit(p, priced) -> Ticket | Refusal     p: a token's Principal (kind token)
      done(ticket, tokens, cost_micros)        the request ended
      watch(ticket, live_micros) -> Refusal | None
      state(token_id, limits, role) -> dict    what is used and what is left, per limit
      near() -> (near, over)                   the findings' counts
"""

from __future__ import annotations

import collections
import datetime as _dt
import itertools
import math
import threading
import time
from dataclasses import dataclass, field

from proxy import tokens as tokens_mod

ROLES = ("viewer", "operator", "admin", "owner")
KEYS = tokens_mod.LIMIT_KEYS
RATE_KEYS = ("requests_per_minute", "tokens_per_minute", "concurrent")
SPEND_KEYS = ("cost_per_day", "cost_per_month")
SETTING = "api.limits.default."
WINDOW_S = 60.0
WATCH_FRAMES = 8
NEAR = 0.8                      # finding token_limit_near: this share of a spend limit used
CODES = {"requests_per_minute": "rate_limited", "tokens_per_minute": "tokens_limited",
         "concurrent": "concurrency_limited", "cost_per_day": "spend_limit_reached",
         "cost_per_month": "spend_limit_reached"}
WORDS = {"requests_per_minute": "requests a minute", "tokens_per_minute": "tokens a minute",
         "concurrent": "requests at once", "cost_per_day": "dollars a day",
         "cost_per_month": "dollars a month"}


def setting_key(role: str, key: str) -> str:
    return f"{SETTING}{role}.{key}"


def micros(dollars) -> int:
    return int(round(float(dollars) * 1_000_000))


@dataclass
class Refusal:
    """A request a limit refuses: its status (429 or 403), the ledger's code, the limit's key,
    the message, and for a 429 the seconds after which it may be tried again."""
    status: int
    code: str
    key: str
    message: str
    retry_after: int | None = None

    def headers(self) -> tuple:
        return (("Retry-After", str(self.retry_after)),) if self.retry_after else ()

    def body(self) -> dict:
        return {"error": {"message": self.message,
                          "type": "rate_limit_error" if self.status == 429 else "permission_error",
                          "code": self.code, "limit": self.key}}


@dataclass
class Ticket:
    """A request let in: its token, its arrival and the periods it counts in."""
    token_id: str
    n: int
    at: float
    day: str
    month: str
    limits: dict
    frames: int = 0
    live: int = 0


@dataclass
class _State:
    reqs: collections.deque = field(default_factory=collections.deque)   # arrival times
    toks: collections.deque = field(default_factory=collections.deque)   # (arrival, tokens)
    inflight: dict = field(default_factory=dict)                          # ticket n -> Ticket
    day: str = ""
    day_micros: int = 0
    month: str = ""
    month_micros: int = 0
    refused_at: float | None = None


class TokenLimits:
    def __init__(self, policy, reader=None, clock=time.time):
        self.policy = policy
        self.reader = reader            # () -> the ledger's read-only connection, or None
        self.clock = clock
        self._lock = threading.Lock()
        self._state: dict[str, _State] = {}
        self._seq = itertools.count(1)

    # ------------------------------------------------------------------ what holds
    def caps(self, role: str | None) -> dict:
        return role_caps(self.policy, role)

    def effective(self, limits, role: str | None) -> dict:
        return effective(self.policy, limits, role)

    # ------------------------------------------------------------------ periods
    def _periods(self, now: float) -> tuple[str, str, float, float]:
        t = _dt.datetime.fromtimestamp(now)
        day0 = t.replace(hour=0, minute=0, second=0, microsecond=0)
        month0 = day0.replace(day=1)
        return day0.strftime("%Y-%m-%d"), day0.strftime("%Y-%m"), day0.timestamp(), \
            month0.timestamp()

    def _ensure(self, token_id: str, now: float) -> None:
        """The token's counters from the ledger at the first sight, read outside the lock (two
        threads may both read; the first to store wins, the rows are the same)."""
        if token_id in self._state:
            return
        day, month, day0, month0 = self._periods(now)
        st = self._seed(token_id, now, day0, month0)
        st.day, st.month = day, month
        with self._lock:
            self._state.setdefault(token_id, st)

    def _get(self, token_id: str, now: float) -> _State:
        """The token's counters (seeded by _ensure; empty when it was not), rolled at midnight."""
        day, month, _, _ = self._periods(now)
        st = self._state.get(token_id)
        if st is None:
            st = self._state[token_id] = _State(day=day, month=month)
        if st.month != month:
            st.month, st.month_micros = month, 0
        if st.day != day:
            st.day, st.day_micros = day, 0
        while st.reqs and st.reqs[0] <= now - WINDOW_S:
            st.reqs.popleft()
        while st.toks and st.toks[0][0] <= now - WINDOW_S:
            st.toks.popleft()
        return st

    def _seed(self, token_id: str, now: float, day0: float, month0: float) -> _State:
        st = _State()
        db = None
        try:
            db = self.reader() if self.reader is not None else None
        except Exception:                                          # noqa: BLE001
            db = None
        if db is None:
            return st
        try:
            month, day = db.execute(
                "SELECT COALESCE(SUM(cost_micros), 0), COALESCE(SUM(CASE WHEN ts_ms >= ? THEN "
                "cost_micros END), 0) FROM requests WHERE token_id = ? AND ts_ms >= ?",
                (int(day0 * 1000), token_id, int(month0 * 1000))).fetchone()
            rows = db.execute(
                "SELECT ts_ms, COALESCE(prompt_tokens, 0) + COALESCE(completion_tokens, 0) FROM "
                "requests WHERE token_id = ? AND ts_ms > ? AND COALESCE(finish_reason, '') != "
                "'refused' ORDER BY ts_ms", (token_id, int((now - WINDOW_S) * 1000))).fetchall()
        except Exception:                                          # noqa: BLE001
            return st
        st.month_micros, st.day_micros = int(month), int(day)
        for ts_ms, toks in rows:
            st.reqs.append(ts_ms / 1000.0)
            if toks:
                st.toks.append((ts_ms / 1000.0, int(toks)))
        return st

    # ------------------------------------------------------------------ the request
    def admit(self, p, priced: bool = True):
        """A Ticket, or the Refusal of the first limit the request would pass. `priced`: the
        model has a price (an unpriced one passes the spend limits)."""
        limits = self.effective(dict(getattr(p, "limits", ()) or ()), getattr(p, "role", None))
        now = self.clock()
        self._ensure(p.id, now)
        with self._lock:
            st = self._get(p.id, now)
            bad = self._check(st, limits, priced, now)
            if bad is not None:
                st.refused_at = now
                return bad
            t = Ticket(p.id, next(self._seq), now, st.day, st.month, limits)
            st.reqs.append(now)
            st.inflight[t.n] = t
            return t

    def _check(self, st: _State, limits: dict, priced: bool, now: float):
        n = limits.get("concurrent")
        if n is not None and len(st.inflight) >= n:
            return Refusal(429, CODES["concurrent"], "concurrent",
                           f"this token runs {len(st.inflight)} requests already; its limit is "
                           f"{_show(n)} at once", 1)
        n = limits.get("requests_per_minute")
        if n is not None and len(st.reqs) >= n:
            k = len(st.reqs) - int(n)               # the oldest that must leave the window
            wait = st.reqs[k] + WINDOW_S - now if n >= 1 else WINDOW_S
            return Refusal(429, CODES["requests_per_minute"], "requests_per_minute",
                           f"this token's limit is {_show(n)} requests a minute", _secs(wait))
        n = limits.get("tokens_per_minute")
        if n is not None:
            used = sum(x for _, x in st.toks)
            if used >= n:
                left, wait = used, WINDOW_S
                for ts, x in st.toks:
                    left -= x
                    if left < n:
                        wait = ts + WINDOW_S - now
                        break
                return Refusal(429, CODES["tokens_per_minute"], "tokens_per_minute",
                               f"this token's limit is {_show(n)} tokens a minute; "
                               f"{used} were used in the last minute", _secs(wait))
        if priced:
            for key, spent in (("cost_per_day", st.day_micros),
                               ("cost_per_month", st.month_micros)):
                n = limits.get(key)
                if n is not None and spent + _live(st) >= micros(n):
                    return Refusal(403, CODES[key], key,
                                   f"this token's spend limit of {_dollars(micros(n))} "
                                   f"{'a day' if key == 'cost_per_day' else 'a month'} is "
                                   f"reached ({_dollars(spent)} spent)")
        return None

    def done(self, t: Ticket, tokens: int | None, cost_micros: int | None) -> None:
        now = self.clock()
        with self._lock:
            st = self._get(t.token_id, now)
            st.inflight.pop(t.n, None)
            if tokens:
                st.toks.append((t.at, int(tokens)))
                while st.toks and st.toks[0][0] <= now - WINDOW_S:
                    st.toks.popleft()
            if cost_micros is None:
                cost_micros = t.live            # a stream cut short has no usage: its live cost
            if cost_micros:
                if t.day == st.day:
                    st.day_micros += int(cost_micros)
                if t.month == st.month:
                    st.month_micros += int(cost_micros)

    def watched(self, t: Ticket | None) -> bool:
        """Is a stream of this request watched (its token has a spend limit)?"""
        return t is not None and any(k in t.limits for k in SPEND_KEYS)

    def watch(self, t: Ticket, live_micros: int | None) -> Refusal | None:
        """A stream's cost so far: a Refusal when the token's spend, its streams in flight
        included, passes a spend limit."""
        now = self.clock()
        with self._lock:
            st = self._get(t.token_id, now)
            t.live = int(live_micros or 0)
            for key, spent in (("cost_per_day", st.day_micros),
                               ("cost_per_month", st.month_micros)):
                n = t.limits.get(key)
                if n is not None and spent + _live(st) > micros(n):
                    st.refused_at = now
                    return Refusal(403, CODES[key], key,
                                   f"this token's spend limit of {_dollars(micros(n))} "
                                   f"{'a day' if key == 'cost_per_day' else 'a month'} was "
                                   f"reached during the answer")
        return None

    # ------------------------------------------------------------------ what the page reads
    def state(self, token_id: str, limits, role: str | None) -> dict:
        """Per limit that holds: {limit, used, left, source} (cost in micro-dollars), and
        whether the token is over one now."""
        eff = self.effective(limits, role)
        own = dict(limits or {})
        now = self.clock()
        self._ensure(token_id, now)
        with self._lock:
            st = self._get(token_id, now)
            used = {"requests_per_minute": len(st.reqs),
                    "tokens_per_minute": sum(x for _, x in st.toks),
                    "concurrent": len(st.inflight),
                    "cost_per_day": st.day_micros, "cost_per_month": st.month_micros}
            refused = st.refused_at
        out = {}
        for k, v in eff.items():
            cap = micros(v) if k in SPEND_KEYS else v
            out[k] = {"limit": v, "used": used[k], "left": max(0, cap - used[k]),
                      "reached": used[k] >= cap,
                      "from": "role" if own.get(k) is None or own.get(k) > v else "token"}
        return {"limits": out, "over": any(x["reached"] for x in out.values()),
                "refused_at": _iso(refused)}

    def near(self, tokens) -> tuple[int, int]:
        """(tokens past NEAR of a spend limit, tokens at or over one): the findings
        token_limit_near and token_limit_reached. `tokens`: (id, limits, role) of the live
        tokens with limits or a role with them."""
        n_near = n_over = 0
        for tid, limits, role in tokens:
            eff = self.effective(limits, role)
            if not any(k in eff for k in SPEND_KEYS):
                continue
            self._ensure(tid, self.clock())
            with self._lock:
                st = self._get(tid, self.clock())
                spent = {"cost_per_day": st.day_micros, "cost_per_month": st.month_micros}
            shares = [spent[k] / micros(eff[k]) if micros(eff[k]) else math.inf
                      for k in SPEND_KEYS if k in eff]
            top = max(shares) if shares else 0
            if top >= 1:
                n_over += 1
            elif top >= NEAR:
                n_near += 1
        return n_near, n_over


def role_caps(policy, role: str | None) -> dict:
    """The role's limits from the settings (unset or 0: none); one read of the settings."""
    role = role if role in ROLES else "viewer"
    pre = f"{SETTING}{role}."
    got: dict = {}
    cfg = getattr(policy, "cfg", None)
    try:
        if cfg is not None and hasattr(cfg, "settings_under"):
            got = {k[len(pre):]: v for k, v in cfg.settings_under(pre).items()}
    except Exception:                                              # noqa: BLE001
        got = {}
    for k in KEYS:
        v = (getattr(policy, "values", None) or {}).get(pre + k)
        if v is not None:
            got[k] = v
    return {k: got[k] for k in KEYS if _num(got.get(k))}


def effective(policy, limits, role: str | None) -> dict:
    """The lower of the token's own limits and its owner's role's, per key (0: none)."""
    own = {k: v for k, v in dict(limits or {}).items() if k in KEYS and _num(v) is not None}
    cap = role_caps(policy, role)
    out = {}
    for k in KEYS:
        a, b = own.get(k), cap.get(k)
        if a is None and b is None:
            continue
        out[k] = b if a is None else a if b is None else min(a, b)
    return out


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        return None
    return v


def _live(st: _State) -> int:
    return sum(t.live for t in st.inflight.values())


def _secs(wait: float) -> int:
    return max(1, int(math.ceil(wait)))


def _show(n) -> str:
    return str(int(n)) if float(n).is_integer() else str(n)


def _dollars(m: int) -> str:
    return f"${m / 1_000_000:.2f}"


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
