"""Accounts, sessions and the request gate (SEC-22, SEC-24): the sign-in of the server.

Since SEC-28 there is no other: no shared admin token, no open default, no dashboard login
switch, no loopback exemption. proxy/authlegacy.py imports what the old sign-in left once.

THE TABLES (in the config database, new; no existing table or column changes):

    accounts     id, provider, subject, username UNIQUE, display_name, role, enabled,
                 pw (JSON of proxy/adminpw.py make_doc; NULL for a provider that keeps its own),
                 must_change, created_at, last_login_at                       (ms since the epoch)
    group_roles  provider, group_name, role                (an external provider's groups, later)
    sessions     id_hash PRIMARY KEY, account_id, csrf_hash, created_at, last_seen_at,
                 idle_until, expires_at, address, agent, revoked_at

THE SESSION. Signing in makes a row; the cookie holds a random id (32 bytes) and the table holds
its SHA-256, never the id. The CSRF token is an HMAC of the id, so it is never stored either (its
hash is). A session ends when it is revoked, when it was idle for `auth.session.idle_days` (30), or
`auth.session.absolute_days` (180) after it began, whichever comes first; both are settings, at
most 400 days, and a shorter value applies to sessions already open. A request renews the idle
time at most once a day (twice with an idle time of one day), and the cookie with it. The table
is the truth: a restart ends nothing, and an end needs no restart. A stream or a WebSocket of a
session that ends is closed: at once when this process ends it, within STREAM_CHECK_S otherwise.

THE COOKIE. `__Host-tandem_session` over https, `tandem_session` over http (each read only on
its own scheme); HttpOnly, SameSite=Strict, Path=/, no Domain, Secure over https, Max-Age the
idle time. Https is known from the listener's own TLS, or from X-Forwarded-Proto sent by a
loopback peer or a TANDEM_TRUSTED_PROXY peer (an address or a CIDR range, tandemapp/auth.py
TrustedProxies); from any other peer the header counts for nothing.

THE GATE (one rule for every route, AccountsAuth.gate):
  1. a path with no entry in proxy/authz.py's table is refused (403 `unmapped_route`), logged once;
  2. the caller: a request with an API credential (Authorization, x-api-key) is that credential
     and nothing else, its cookie ignored and never checked for CSRF; a /s/<token>/ request is
     its session address; else the session cookie. No credential: 401 on every route that is not
     open;
  3. every cookie request that writes (not GET or HEAD) comes from this origin (no
     `Sec-Fetch-Site: cross-site`, an `Origin` naming this host when sent), declares a JSON body
     when it sends one (a file upload excepted), and carries `X-Tandem-CSRF` equal to the
     session's token (GET /v1/auth/session returns it). The sign-in POST passes the first two;
  4. a cookie WebSocket upgrade sends an `Origin` naming this host; a cookie event stream is
     refused cross-site or from another origin;
  5. the permission the route needs (403 `permission_denied`, the permission named).

SIGN-IN LIMITS. Per address: five failures a minute, then 429 for a minute (tandemapp/auth.py's
rule and key). Per name tried: after five failures each further try waits twice as long as the
one before, 2 s up to 300 s, never a lock for good; the name tried is the key whether or not it
exists, so the answer does not tell. Over every address: proxy/identity.py's cap.

SEAMS: `service_resolver` (SRV-869: `(headers, peer) -> Principal | None`, asked before the token
store for a request with a bearer: the plugin relay's in-process secret, relay_principal),
`exposure` (SRV-871, proxy/exposure.py: `(principal, method, path, peer, headers) -> (status,
code, message) | None`, the protocol's exposure switch for a token), `on_account_removed`.

SRV-869: being on the same machine opens nothing outside local mode: the one this-machine test
(proxy/authlocal.py trusted_local) has two callers, local mode and the exposure rule. The
controller's own callers have identities: chatd's svc:chatd, the CLI's svc:cli
(<state>/run/cli.token, write_cli_token at each start), a backend's collect identity, the plugin
relay's secret. A revoked token's open stream closes as an ended
session's does (Streams).

SRV-867 adds API tokens (proxy/tokens.py): `token_resolver` is the token store's. A request with
an API credential is that token and nothing else: unknown, revoked, expired or of a disabled owner
is a 401 saying which (an open route lets it through as anonymous; in local mode, SEC-28, an
unknown one is as no credential, so a local tool's made-up key acts as the owner `local`, while a
known token stays itself and a revoked or expired one stays a 401); the route must take one of
the token's scopes (403 naming the scope); the token acts with its scopes cut to its owner's role
of now. After the check `Authorization` and `x-api-key` are removed from the request, so no
handler and no backend sees them. A disable or a removal revokes the account's tokens here in the
store, so the CLI's changes do too.

SEC-26 adds local mode and the setup code (proxy/authlocal.py), the tables `setup_codes` and
`auth_state`, and one-time codes: `users add` and `users reset-password` (proxy/userscli.py)
give an account a code of 20 characters in place of a password, valid 24 hours; signed in with
it, the account may only set its password (403 `password_change_required` elsewhere). A
local-mode write passes the same-origin rule of a cookie write, without the CSRF header.
SRV-865 adds the first owner (`auth_state.first_owner`): admin.json follows its password in the
old format (proxy/authlegacy.py).
"""

from __future__ import annotations

import collections
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import threading
import time
from dataclasses import dataclass

from proxy import adminpw
from proxy import authz
from proxy import identity as identity_mod
from proxy import tokens as tokens_mod
from tandemapp import auth as auth_mod

COOKIE_HTTP = "tandem_session"
COOKIE_HTTPS = "__Host-tandem_session"
CSRF_HEADER = "X-Tandem-CSRF"
DAY_MS = 86_400_000
TOUCH_MS = 60_000                       # last_seen_at is written at most once a minute
STREAM_CHECK_S = 2.0
TOKEN_STREAM = "t:"                     # SRV-869: a token's key in the stream registry
EXPIRING_MS = 7 * 86_400_000            # SRV-871: finding token_expiring within this
ACCOUNT_FAILS = 5
ACCOUNT_WAIT_S = (2.0, 300.0)
ACCOUNT_FORGET_S = 3600.0               # a name's failures are forgotten after an hour of quiet
MAX_KEYS = 10_000
UNMAPPED_LOGGED = 1000
USERNAME = re.compile(r"^[a-z0-9][a-z0-9._@-]{0,63}$")
BINARY_WRITES = ("/v1/fleet/plugins/upload", "/v1/chat-app")
SESSION_CONTRACT = "auth-v1"
# what an account signed in with a one-time code may do: see itself, set its password, sign out
MUST_CHANGE_OK = {("GET", "/v1/auth/session"), ("DELETE", "/v1/auth/session"),
                  ("PUT", "/v1/auth/password")}

DDL = """
CREATE TABLE IF NOT EXISTS accounts (
  id TEXT PRIMARY KEY, provider TEXT NOT NULL, subject TEXT NOT NULL,
  username TEXT NOT NULL UNIQUE, display_name TEXT, role TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1, pw TEXT, must_change INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL, last_login_at INTEGER, UNIQUE (provider, subject));
CREATE TABLE IF NOT EXISTS group_roles (
  provider TEXT NOT NULL, group_name TEXT NOT NULL, role TEXT NOT NULL,
  PRIMARY KEY (provider, group_name));
CREATE TABLE IF NOT EXISTS sessions (
  id_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  csrf_hash TEXT NOT NULL, created_at INTEGER NOT NULL, last_seen_at INTEGER NOT NULL,
  idle_until INTEGER NOT NULL, expires_at INTEGER NOT NULL, address TEXT, agent TEXT,
  revoked_at INTEGER);
CREATE INDEX IF NOT EXISTS sessions_account ON sessions(account_id);
CREATE TABLE IF NOT EXISTS setup_codes (
  hash TEXT PRIMARY KEY, source TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER,
  tries INTEGER NOT NULL DEFAULT 0, spent_at INTEGER);
CREATE TABLE IF NOT EXISTS auth_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
ACCOUNT_COLS = ("id", "provider", "subject", "username", "display_name", "role", "enabled", "pw",
                "must_change", "created_at", "last_login_at")
SESSION_COLS = ("id_hash", "account_id", "csrf_hash", "created_at", "last_seen_at", "idle_until",
                "expires_at", "address", "agent", "revoked_at")


class AccountError(Exception):
    def __init__(self, status: int, code: str, message: str, **facts):
        super().__init__(message)
        self.status, self.code, self.message, self.facts = status, code, message, facts

    def body(self) -> dict:
        return {"error": {"type": self.code, "code": self.code, "message": self.message,
                          **self.facts}}


@dataclass(frozen=True)
class Principal:
    kind: str                       # account | token | service | session-address | local
    id: str
    name: str
    role: str | None = None
    via: str = "cookie"             # cookie | token | address | local
    scopes: frozenset | None = None
    session: str | None = None      # the session's id_hash (cookie)
    csrf: str | None = None         # the session's CSRF token (cookie)
    expires_at: int | None = None   # ms
    must_change: bool = False       # signed in with a one-time code: only a new password
    perms: frozenset | None = None  # SRV-867: a token's scopes cut to its owner's role of now
    models: tuple = ()              # SRV-867: a token's model globs (none: every model)
    client: str | None = None       # SRV-867: the ledger's client id (a prefix of the hash)
    limits: tuple = ()              # SRV-873: a token's own limits, (key, value) pairs


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def csrf_of(sid: str) -> str:
    return hmac.new(sid.encode(), b"tandem-csrf-v1", hashlib.sha256).hexdigest()


def _clean(text, n: int = 200) -> str:
    """A header or a name as a log, an audit row and an answer may hold it."""
    s = "".join(c if 32 <= ord(c) < 127 or ord(c) > 159 else "?" for c in str(text or ""))
    return s[:n]


def public_id(id_hash: str) -> str:
    """A session's name in the lists and the routes: a prefix of its hash (never the id)."""
    return id_hash[:16]


# ======================================================================================== store
class AccountStore:
    """The tables above over proxy/config.py's ConfigDB (its connection and its lock)."""

    def __init__(self, cfg, *, clock=time.time, policy: authz.Policy | None = None):
        self.cfg = cfg
        self.clock = clock
        self.policy = policy or authz.Policy(cfg)
        with cfg._lock:
            cfg._conn.executescript(DDL)
            cfg._conn.commit()
        self.tokens = tokens_mod.TokenStore(self)       # SRV-867

    def now_ms(self) -> int:
        return int(self.clock() * 1000)

    @property
    def _c(self):
        return self.cfg._conn

    # ------------------------------------------------------------------ accounts
    @staticmethod
    def _row(r) -> dict | None:
        if r is None:
            return None
        d = dict(zip(ACCOUNT_COLS, r))
        d["enabled"] = bool(d["enabled"])
        d["must_change"] = bool(d["must_change"])
        try:
            d["pw"] = json.loads(d["pw"]) if d["pw"] else None
        except ValueError:
            d["pw"] = None                     # a hash that does not read: nobody signs in
        return d

    def by_username(self, name: str) -> dict | None:
        with self.cfg._lock:
            r = self._c.execute(f"SELECT {', '.join(ACCOUNT_COLS)} FROM accounts WHERE username "
                                f"= ?", ((name or "").strip().lower(),)).fetchone()
        return self._row(r)

    def get(self, account_id: str) -> dict | None:
        with self.cfg._lock:
            r = self._c.execute(f"SELECT {', '.join(ACCOUNT_COLS)} FROM accounts WHERE id = ?",
                                (account_id,)).fetchone()
        return self._row(r)

    def all(self) -> list[dict]:
        with self.cfg._lock:
            rows = self._c.execute(f"SELECT {', '.join(ACCOUNT_COLS)} FROM accounts ORDER BY "
                                   f"created_at, username").fetchall()
        return [self._row(r) for r in rows]

    def owners_enabled(self) -> list[str]:
        with self.cfg._lock:
            return [r[0] for r in self._c.execute(
                "SELECT id FROM accounts WHERE role = 'owner' AND enabled = 1")]

    def setup_required(self) -> bool:
        """No enabled owner with a password (the first-run setup comes with a later change)."""
        with self.cfg._lock:
            return self._c.execute("SELECT COUNT(*) FROM accounts WHERE role = 'owner' AND "
                                   "enabled = 1 AND provider = 'local' AND pw IS NOT NULL"
                                   ).fetchone()[0] == 0

    @staticmethod
    def view(row: dict) -> dict:
        """An account as an answer holds it: never its hash or salt."""
        return {"id": row["id"], "username": row["username"], "display_name": row["display_name"],
                "role": row["role"], "enabled": row["enabled"], "provider": row["provider"],
                "has_password": row["pw"] is not None, "must_change": row["must_change"],
                "created_at": row["created_at"], "last_login_at": row["last_login_at"]}

    def check_password(self, password) -> None:
        bad = adminpw.check_new(password)
        if bad is None and len(password) < self.policy.password_min_length:
            bad = f"the password is at least {self.policy.password_min_length} characters"
        if bad:
            raise AccountError(400, "bad_password", bad)

    def create(self, username, role, password=None, *, display_name=None, actor: str,
               must_change: bool = False, provider: str = "local") -> dict:
        name = (username or "").strip().lower() if isinstance(username, str) else ""
        if not USERNAME.match(name):
            raise AccountError(400, "bad_request", "a username is 1 to 64 characters: letters, "
                                                   "digits and . _ @ -, starting with a letter or "
                                                   "a digit")
        if role not in authz.ROLES:
            raise AccountError(400, "bad_request", f"the role is one of {', '.join(authz.ROLES)}")
        if display_name is not None and not isinstance(display_name, str):
            raise AccountError(400, "bad_request", "display_name is a string")
        doc = None
        if password is not None:
            self.check_password(password)
            doc = adminpw.make_doc(password, self.clock())
        aid = secrets.token_hex(6)
        now = self.now_ms()
        with self.cfg._lock:
            if self._c.execute("SELECT 1 FROM accounts WHERE username = ?", (name,)).fetchone():
                raise AccountError(409, "conflict", f"the username {name!r} is taken")
            self._c.execute("INSERT INTO accounts(id, provider, subject, username, display_name, "
                            "role, enabled, pw, must_change, created_at) VALUES (?, ?, ?, ?, ?, "
                            "?, 1, ?, ?, ?)",
                            (aid, provider, aid, name, _clean(display_name, 120) or None, role,
                             json.dumps(doc) if doc else None, int(bool(must_change)), now))
            self.cfg._audit(actor, "create", "account", aid,
                            detail={"username": name, "role": role})
            self._c.commit()
        return self.get(aid)

    def _last_owner(self, row: dict) -> bool:
        return row["role"] == "owner" and row["enabled"] and \
            self.owners_enabled() == [row["id"]]

    def update(self, account_id: str, changes: dict, *, actor: str) -> tuple[dict, bool]:
        """(the row, whether its sessions end: a disable or a lower role). The last-owner check
        and the write hold the database's lock together: two at once never leave none."""
        with self.cfg._lock:
            row, ends = self._update_locked(account_id, changes, actor)
        if not row["enabled"] or row["role"] != "owner":
            self._first_owner_left(account_id)
        if not row["enabled"]:
            # SRV-867: a disabled account's tokens stop for good (the page's and the CLI's)
            self.tokens.revoke_owner(account_id, actor=actor, why="owner disabled")
        if ends:
            self.revoke_account(account_id, actor=actor,
                                why="role" if row["enabled"] else "disabled")
        return row, bool(ends)

    def _update_locked(self, account_id: str, changes: dict, actor: str):
        row = self.get(account_id)
        if row is None:
            raise AccountError(404, "not_found", "no such account")
        if not isinstance(changes, dict) or not changes or \
                set(changes) - {"role", "enabled", "display_name"}:
            raise AccountError(400, "bad_request", "the body is {role?, enabled?, display_name?}")
        role = changes.get("role", row["role"])
        enabled = changes.get("enabled", row["enabled"])
        if role not in authz.ROLES or not isinstance(enabled, bool):
            raise AccountError(400, "bad_request", f"the role is one of "
                                                   f"{', '.join(authz.ROLES)}; enabled a boolean")
        if self._last_owner(row) and (role != "owner" or not enabled):
            raise AccountError(409, "last_owner", "this is the last enabled owner: make another "
                                                  "owner first")
        name = changes.get("display_name", row["display_name"])
        if name is not None and not isinstance(name, str):
            raise AccountError(400, "bad_request", "display_name is a string")
        ends = (not enabled and row["enabled"]) or authz.rank(role) < authz.rank(row["role"])
        self._c.execute("UPDATE accounts SET role = ?, enabled = ?, display_name = ? WHERE "
                        "id = ?", (role, int(enabled), _clean(name, 120) or None, account_id))
        for what, old, new in (("role", row["role"], role),
                               ("enabled", row["enabled"], enabled)):
            if old != new:
                self.cfg._audit(actor, what if what == "role" else
                                ("enable" if new else "disable"), "account", account_id,
                                detail={"username": row["username"], "from": old, "to": new})
        if row["display_name"] != name:
            self.cfg._audit(actor, "update", "account", account_id, fields=["display_name"],
                            detail={"username": row["username"]})
        self._c.commit()
        return self.get(account_id), ends

    def remove(self, account_id: str, *, actor: str) -> dict:
        with self.cfg._lock:                 # the last-owner check and the delete: one hold
            row = self.get(account_id)
            if row is None:
                raise AccountError(404, "not_found", "no such account")
            if self._last_owner(row):
                raise AccountError(409, "last_owner", "this is the last enabled owner: make "
                                                      "another owner first")
            self.revoke_account(account_id, actor=actor, why="removed")
            self.tokens.revoke_owner(account_id, actor=actor, why="owner removed")
            self._c.execute("DELETE FROM sessions WHERE account_id = ?", (account_id,))
            self._c.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            self.cfg._audit(actor, "delete", "account", account_id,
                            detail={"username": row["username"], "role": row["role"]})
            self._c.commit()
        self._first_owner_left(account_id)
        return row

    def set_password(self, account_id: str, password: str, *, actor: str,
                     action: str = "password") -> None:
        self.check_password(password)
        doc = adminpw.make_doc(password, self.clock())
        with self.cfg._lock:
            self._c.execute("UPDATE accounts SET pw = ?, must_change = 0 WHERE id = ?",
                            (json.dumps(doc), account_id))
            self.cfg._audit(actor, action, "account", account_id,
                            detail={"text": "password changed"})
            self._c.commit()
        self._password_changed(account_id, doc)

    # ------------------------------------------------------------------ one-time codes (SEC-26)
    def set_code(self, account_id: str, *, actor: str, action: str = "reset") -> str:
        """A one-time code in place of the password (valid CODE_TTL_MS): the old password stops
        at once and every session ends. The code is returned once; only its hash is kept."""
        from proxy import authlocal
        code = authlocal.new_code()
        doc = adminpw.make_doc(code, self.clock())
        doc["expires_at"] = self.now_ms() + authlocal.CODE_TTL_MS
        with self.cfg._lock:
            if self.get(account_id) is None:
                raise AccountError(404, "not_found", "no such account")
            self._c.execute("UPDATE accounts SET pw = ?, must_change = 1 WHERE id = ?",
                            (json.dumps(doc), account_id))
            self.cfg._audit(actor, action, "account", account_id,
                            detail={"text": "one-time code made"})
            self._c.commit()
        self.revoke_account(account_id, actor=actor, why="password reset")
        self._password_changed(account_id, doc)
        return code

    # ------------------------------------------------------------------ auth_state
    def state(self, key: str):
        with self.cfg._lock:
            r = self._c.execute("SELECT value FROM auth_state WHERE key = ?", (key,)).fetchone()
        return json.loads(r[0]) if r else None

    def set_state(self, key: str, value) -> None:
        with self.cfg._lock:
            self._c.execute("INSERT OR REPLACE INTO auth_state(key, value) VALUES (?, ?)",
                            (key, json.dumps(value)))
            self._c.commit()

    def first_owner(self) -> dict | None:
        """The owner admin.json follows (SRV-865): made from it, or by the first-run setup."""
        aid = self.state("first_owner")
        return self.get(aid) if isinstance(aid, str) else None

    on_password = None                      # (account_id, pw doc) -> None (proxy/authlegacy.py)

    def _first_owner_left(self, account_id: str) -> None:
        """SRV-865: the first owner was removed, disabled or demoted: admin.json follows the
        oldest other enabled owner with a password from now on (the last one cannot leave), so
        a rollback never takes the password of someone who left."""
        if self.state("first_owner") != account_id:
            return
        nxt = next((r for r in self.all() if r["id"] != account_id and r["role"] == "owner" and
                    r["enabled"] and isinstance(r["pw"], dict)), None)
        if nxt is None:
            return
        self.set_state("first_owner", nxt["id"])
        self._password_changed(nxt["id"], nxt["pw"])

    def _password_changed(self, account_id: str, doc: dict) -> None:
        if self.on_password is not None:
            self.on_password(account_id, doc)

    def mark_login(self, account_id: str) -> None:
        with self.cfg._lock:
            self._c.execute("UPDATE accounts SET last_login_at = ? WHERE id = ?",
                            (self.now_ms(), account_id))
            self._c.commit()

    # ------------------------------------------------------------------ sessions
    def open_session(self, account_id: str, address: str, agent: str) -> tuple[str, dict]:
        """A new session: (its id for the cookie, its row). The id is never stored."""
        sid = secrets.token_urlsafe(32)
        now = self.now_ms()
        if now - self._pruned > 3_600_000:
            self.prune()
        p = self.policy
        row = {"id_hash": _sha(sid), "account_id": account_id, "csrf_hash": _sha(csrf_of(sid)),
               "created_at": now, "last_seen_at": now, "idle_until": now + p.idle_days * DAY_MS,
               "expires_at": now + p.absolute_days * DAY_MS, "address": _clean(address, 64),
               "agent": _clean(agent, 200), "revoked_at": None}
        with self.cfg._lock:
            self._c.execute(f"INSERT INTO sessions({', '.join(SESSION_COLS)}) VALUES "
                            f"({', '.join('?' * len(SESSION_COLS))})",
                            [row[k] for k in SESSION_COLS])
            self._c.commit()
        return sid, row

    def _live(self, row: dict | None, now: int) -> bool:
        if row is None or row["revoked_at"] is not None:
            return False
        p = self.policy
        return (now < row["idle_until"] and now < row["expires_at"]
                and now < row["created_at"] + p.absolute_days * DAY_MS
                and now < row["last_seen_at"] + p.idle_days * DAY_MS)

    def session_row(self, id_hash: str) -> dict | None:
        with self.cfg._lock:
            r = self._c.execute(f"SELECT {', '.join(SESSION_COLS)} FROM sessions WHERE id_hash "
                                f"= ?", (id_hash,)).fetchone()
        return dict(zip(SESSION_COLS, r)) if r else None

    def lookup(self, sid: str) -> tuple[dict, dict] | None:
        """(session, account) for a live session of an enabled account, else None."""
        if not sid or len(sid) > 128:
            return None
        row = self.session_row(_sha(sid))
        if not self._live(row, self.now_ms()):
            return None
        acc = self.get(row["account_id"])
        if acc is None or not acc["enabled"]:
            return None
        if hmac.compare_digest(row["csrf_hash"], _sha(csrf_of(sid))) is False:
            return None
        return row, acc

    def valid(self, id_hash: str) -> bool:
        row = self.session_row(id_hash)
        if not self._live(row, self.now_ms()):
            return False
        acc = self.get(row["account_id"])
        return acc is not None and acc["enabled"]

    def touch(self, row: dict, address: str) -> bool:
        """Note the request; True when the idle time was renewed (at most once a day: the cookie
        is sent again)."""
        now = self.now_ms()
        idle = self.policy.idle_days * DAY_MS
        renew = row["idle_until"] - now < idle - min(DAY_MS, idle // 2)
        if not renew and now - row["last_seen_at"] < TOUCH_MS:
            return False
        with self.cfg._lock:
            if renew:
                self._c.execute("UPDATE sessions SET last_seen_at = ?, address = ?, idle_until = "
                                "? WHERE id_hash = ?", (now, _clean(address, 64), now + idle,
                                                        row["id_hash"]))
            else:
                self._c.execute("UPDATE sessions SET last_seen_at = ?, address = ? WHERE id_hash "
                                "= ?", (now, _clean(address, 64), row["id_hash"]))
            self._c.commit()
        return renew

    def sessions(self, account_id: str | None = None) -> list[dict]:
        """The live sessions, newest first: of one account, or of every account."""
        now = self.now_ms()
        sql = (f"SELECT {', '.join('s.' + c for c in SESSION_COLS)}, a.username FROM sessions s "
               f"JOIN accounts a ON a.id = s.account_id WHERE s.revoked_at IS NULL"
               + (" AND s.account_id = ?" if account_id else "") + " ORDER BY s.created_at DESC")
        with self.cfg._lock:
            rows = self._c.execute(sql, (account_id,) if account_id else ()).fetchall()
        out = []
        for r in rows:
            row = dict(zip(SESSION_COLS, r[:-1]))
            if self._live(row, now):
                out.append({"id": public_id(row["id_hash"]), "account_id": row["account_id"],
                            "username": r[-1], "created_at": row["created_at"],
                            "last_seen_at": row["last_seen_at"],
                            "idle_until": min(row["idle_until"],
                                              row["last_seen_at"] + self.policy.idle_days * DAY_MS),
                            "expires_at": min(row["expires_at"], row["created_at"]
                                              + self.policy.absolute_days * DAY_MS),
                            "address": row["address"], "agent": row["agent"],
                            "_id_hash": row["id_hash"]})
        return out

    def find_session(self, public: str) -> dict | None:
        if not isinstance(public, str) or not re.fullmatch(r"[0-9a-f]{16}", public):
            return None
        with self.cfg._lock:
            r = self._c.execute(f"SELECT {', '.join(SESSION_COLS)} FROM sessions WHERE "
                                f"substr(id_hash, 1, 16) = ? AND revoked_at IS NULL",
                                (public,)).fetchall()
        return dict(zip(SESSION_COLS, r[0])) if len(r) == 1 else None

    def revoke(self, id_hashes, *, actor: str, why: str) -> list[str]:
        ids = [h for h in id_hashes if h]
        if not ids:
            return []
        now = self.now_ms()
        with self.cfg._lock:
            done = []
            for h in ids:
                cur = self._c.execute("UPDATE sessions SET revoked_at = ? WHERE id_hash = ? AND "
                                      "revoked_at IS NULL", (now, h))
                if cur.rowcount:
                    done.append(h)
                    self.cfg._audit(actor, "revoke", "session", public_id(h),
                                    detail={"why": why})
            self._c.commit()
        if done and self.on_revoked is not None:
            self.on_revoked(done)
        return done

    on_revoked = None                       # the stream registry's close (AccountsAuth sets it)

    def revoke_account(self, account_id: str, *, actor: str, why: str,
                       keep: str | None = None) -> list[str]:
        with self.cfg._lock:
            ids = [r[0] for r in self._c.execute("SELECT id_hash FROM sessions WHERE account_id "
                                                 "= ? AND revoked_at IS NULL", (account_id,))]
        return self.revoke([h for h in ids if h != keep], actor=actor, why=why)

    def prune(self) -> int:
        """Rows of sessions that ended more than a day ago go (a sign-in runs it, at most once
        an hour)."""
        now = self.now_ms()
        cut = now - DAY_MS
        p = self.policy
        with self.cfg._lock:
            n = self._c.execute("DELETE FROM sessions WHERE (revoked_at IS NOT NULL AND "
                                "revoked_at < ?) OR expires_at < ? OR idle_until < ? OR "
                                "created_at < ? OR last_seen_at < ?",
                                (cut, cut, cut, cut - p.absolute_days * DAY_MS,
                                 cut - p.idle_days * DAY_MS)).rowcount
            self._c.commit()
        self._pruned = now
        return n

    _pruned = 0


# ======================================================================================== streams
class Streams:
    """The connections each session holds open now. An end closes them: at once from this
    process, within STREAM_CHECK_S from anywhere (a check of the table).

    SRV-869: an API token's open stream too, under the key `t:<token id>`: a revoke closes it at
    once from this process (TokenStore.on_revoked), and the watcher closes it within
    STREAM_CHECK_S when the token was revoked elsewhere (the CLI), expired, or its owner was
    disabled."""

    def __init__(self, store: AccountStore, *, check_s: float = STREAM_CHECK_S):
        self.store = store
        self.check_s = check_s
        self._open: dict[str, set] = collections.defaultdict(set)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def add(self, id_hash: str, conn) -> None:
        with self._lock:
            self._open[id_hash].add(conn)
            if self._thread is None:
                self._thread = threading.Thread(target=self._watch, name="session-streams",
                                                daemon=True)
                self._thread.start()

    def remove(self, id_hash: str, conn) -> None:
        with self._lock:
            s = self._open.get(id_hash)
            if s is not None:
                s.discard(conn)
                if not s:
                    del self._open[id_hash]

    def close(self, id_hashes, keep=None) -> int:
        n = 0
        with self._lock:
            conns = [c for h in id_hashes for c in self._open.get(h, ()) if c is not keep]
        for c in conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
                n += 1
            except OSError:
                pass
        return n

    def _watch(self) -> None:
        while True:
            time.sleep(self.check_s)
            with self._lock:
                keys = list(self._open)
                if not keys:
                    self._thread = None          # the next open stream starts another
                    return
            dead = []
            for h in keys:
                try:
                    ok = self.store.tokens.live(h[2:]) if h.startswith(TOKEN_STREAM) else \
                        self.store.valid(h)
                    if not ok:
                        dead.append(h)
                except Exception:                                  # noqa: BLE001
                    pass
            if dead:
                self.close(dead)
                with self._lock:
                    for h in dead:
                        self._open.pop(h, None)


# ======================================================================================== limits
class SignInLimits:
    """Per address (tandemapp/auth.py's rule) and per name tried (a doubling wait)."""

    def __init__(self, trusted_proxies=(), *, clock=time.time):
        self.clock = clock
        self.by_address = auth_mod.LoginLimit(trusted_proxies, clock=clock)
        self._names: dict[str, list] = {}           # name -> [failures, wait until, last failure]
        self._lock = threading.Lock()

    def key(self, peer: str, headers) -> str:
        return self.by_address.login_key(peer, headers)

    def wait(self, address: str, name: str) -> int:
        """Seconds to wait before a try may run (0: go)."""
        if self.by_address.login_blocked(address):
            return int(auth_mod.LOGIN_WINDOW_S)
        now = self.clock()
        with self._lock:
            got = self._names.get(name)
            if got is not None and now - got[2] > ACCOUNT_FORGET_S:
                del self._names[name]
                return 0
            if got is not None and got[1] > now:
                return max(1, int(got[1] - now + 0.999))
        return 0

    def failed(self, address: str, name: str) -> None:
        self.by_address.login_failed(address)
        now = self.clock()
        with self._lock:
            got = self._names.setdefault(name, [0, 0.0, now])
            got[0] += 1
            got[2] = now
            if got[0] >= ACCOUNT_FAILS:
                lo, hi = ACCOUNT_WAIT_S
                got[1] = now + min(hi, lo * 2 ** (got[0] - ACCOUNT_FAILS))
            if len(self._names) > MAX_KEYS:
                for k, v in sorted(self._names.items(), key=lambda kv: kv[1][2]):
                    if len(self._names) <= MAX_KEYS - MAX_KEYS // 10:
                        break
                    if v[1] <= now:
                        del self._names[k]

    def succeeded(self, name: str) -> None:
        with self._lock:
            self._names.pop(name, None)


# ======================================================================================== the gate
CREDENTIAL_HEADERS = ("Authorization", "x-api-key")


def strip_credentials(headers) -> None:
    """SRV-867: after the check the request holds no token: no handler, adapter or backend sees
    it (proxy/adapters/generic.py also refuses to pass either on once this ran). The header that
    carried it stays known (`tandem_credential`: x-api-key alone, else authorization), so the
    model list still answers a Messages API client in its shape (proxy/messages.py)."""
    try:
        key = (headers.get("x-api-key") or "").strip()
        auth = (headers.get("Authorization") or "").strip()
        headers.tandem_credential = "x-api-key" if key and not auth else "authorization"
    except Exception:                                              # noqa: BLE001
        pass
    for name in CREDENTIAL_HEADERS:
        try:
            del headers[name]
        except Exception:                                          # noqa: BLE001
            pass
    try:
        headers.tandem_credentials_checked = True
    except Exception:                                              # noqa: BLE001
        pass


def _messages_shape(handler, raw: str) -> bool:
    """A refusal the client reads in the Messages API's shape: on its routes, and on the model
    list when that list would answer in its shape (proxy/messages.py wants_models)."""
    if tokens_mod.anthropic(raw):
        return True
    p = raw.rstrip("/")
    if p == "/v1/models" or p.startswith("/v1/models/"):
        from proxy import messages as messages_mod
        return messages_mod.wants_models(handler.headers)
    return False


class AccountsAuth:
    """The sign-in of the server (SEC-28: the only one): a route kind is a permission
    (proxy/authz.py), the caller is the request's principal. The secrets of before (the admin
    token, the `qse_dash` cookie, admin.json, a loopback peer) open nothing here; only
    proxy/authlegacy.py reads them, to import them once."""

    accounts = True

    def __init__(self, cfg, *, trusted_proxies=(), clock=time.time, listener_tls: bool = False,
                 log=None, env=None):
        self.cfg = cfg
        self.clock = clock
        self.policy = authz.Policy(cfg)
        self.store = AccountStore(cfg, clock=clock, policy=self.policy)
        self.provider = identity_mod.LocalProvider(self.store, clock=clock)
        # OPS-298: addresses and CIDR ranges, one parser for both auth modes
        self.trusted_proxies = auth_mod.TrustedProxies(trusted_proxies)
        self.trusted_proxy_file = None      # where TANDEM_TRUSTED_PROXY is set
        self.limits = SignInLimits(self.trusted_proxies, clock=clock)
        self.streams = Streams(self.store)
        self.store.on_revoked = lambda ids: self.streams.close(ids, keep=self._tls_conn())
        self.store.tokens.on_revoked = lambda ids: self.streams.close(
            [TOKEN_STREAM + i for i in ids], keep=self._tls_conn())
        self.listener_tls = listener_tls
        self.log = log or (lambda m: None)
        self._unmapped: set = set()
        self._tls = threading.local()
        # the seams of the later changes
        self.tokens = self.store.tokens
        self.token_resolver = self.tokens.resolve   # SRV-867: (headers) -> Principal | Refused
        self.exposure = None                # SRV-871: (p, method, path, peer, headers) -> refusal
        # SRV-869: the plugin relay's secret, made at each start and kept in this process only;
        # the relay sends it as the bearer of a plugin's backend (proxy/config.py service_key)
        self.relay_secret = secrets.token_urlsafe(32)
        self.service_resolver = self.relay_principal    # (headers, peer) -> Principal | None
        self.local_mode = None              # (peer, headers) -> Principal | None
        self.on_account_removed = None      # (account_id) -> None: its tokens
        self.token_limits = None            # SRV-873: proxy/tokenlimits.py (proxy/app.py sets it)
        # SEC-26: local mode and the setup codes; SRV-865: the legacy variables' findings
        from proxy import authlocal
        self.env = dict(env) if env is not None else {}
        self.local = authlocal.LocalMode(self.store, self.env, clock=clock, log=self.log)
        self.local_mode = self.local
        self.codes = authlocal.SetupCodes(self.store, self.env)
        self.legacy: list[dict] = []        # proxy/authlegacy.py scan(): {code, variable, file}
        # SRV-871: each protocol's exposure switch narrows a valid token by where it comes from
        from proxy import exposure as exposure_mod
        self.exposure = exposure_mod.Exposure(self, clock=clock)

    def listen(self, host: str, port=None, say=None) -> str | None:
        """SEC-26: where the process listens decides local mode (never the peer). Without local
        mode and with no owner, a fresh setup code goes to the log (returned for the caller)."""
        from proxy import authlocal
        say = say or self.log
        self.local.bind = host
        try:
            self.write_cli_token()                   # SRV-869: every start, a fresh one
        except OSError as exc:
            say(f"[auth] the CLI's token was not written: {type(exc).__name__}: {exc}")
        if not self.store.setup_required():
            return None
        if self.local.on():
            say("[auth] local mode: this machine's browser acts as the owner until a password is "
                "set; every other device needs a sign-in")
            return None
        self.codes.sync_secret()
        if self.codes.secret_text():
            say(f"[auth] no owner yet: the setup code is in {self.codes.secret_path()} (single "
                f"use; it opens only the first-run setup)")
            return None
        code = self.codes.new("log")
        where = f"http://{'<this host>' if not authlocal.loopback_bind(host) else host}" \
                f"{':' + str(port) if port else ''}/"
        say(f"[auth] no owner yet. Setup code: {code} (single use, {authlocal.TRIES} tries, valid "
            f"24 h or until the next start; it opens only the first-run setup). Open {where} and "
            f"enter it; `tandem-app users setup-code` prints a new one")
        return code

    def write_cli_token(self, state_dir: str | None = None) -> str:
        """SRV-869: the CLI's service identity (svc:cli, an operator's reach, never trust),
        minted at each start into <state>/run/cli.token: the file 0600, its folder 0700. Reading
        the file is the proof; another local user cannot. Returns the file's path."""
        path = tokens_mod.cli_token_path(state_dir or os.path.dirname(self.cfg.path))
        d = os.path.dirname(path)
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
        token = self.tokens.mint_service(tokens_mod.CLI_SERVICE, tokens_mod.CLI_SCOPES,
                                         memory_user="cli")
        tmp = f"{path}.tmp-{os.getpid()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, (token + "\n").encode())
        finally:
            os.close(fd)
        os.replace(tmp, path)
        return path

    def findings(self) -> list[dict]:
        """The health page's controller findings of the sign-in (codes of proxy/enginecodes.py):
        {code, detail?, facts}."""
        out: list[dict] = []
        need = self.store.setup_required()
        local = self.local.on()
        if need and not local:
            out.append({"code": "auth_setup_pending", "facts": {}})
        if local:
            out.append({"code": "auth_owner_no_password", "facts": {}})
            if self.local.seen is not None:
                out.append({"code": "local_mode_proxy_seen", "facts": {}})
        if not need and self.codes.secret_text():
            out.append({"code": "bootstrap_secret_left",
                        "facts": {"path": self.codes.secret_path()}})
        for f in self.legacy:
            out.append({"code": f["code"], "facts": {"variable": f["variable"],
                                                     "file": f["file"]}})
        # OPS-298: an entry of TANDEM_TRUSTED_PROXY that is neither an address nor a range
        bad = self.trusted_proxies.bad
        if bad:
            out.append({"code": "trusted_proxy_invalid", "detail": ", ".join(bad)[:300],
                        "facts": {"variable": auth_mod.TRUSTED_PROXY_ENV,
                                  "file": self.trusted_proxy_file or "the environment"}})
        # SRV-867: imported keys to confirm, and the admin token while it still works
        n = self.tokens.need_review()
        if n:
            out.append({"code": "tokens_need_review", "facts": {"n": n}})
        if self.tokens.admin_token_live() is not None:
            out.append({"code": "legacy_admin_token", "facts": {}})
        # SRV-871: the exposure switches against what is reachable, and tokens about to end
        out += self.exposure.findings()
        n = self.tokens.expiring(EXPIRING_MS)
        if n:
            out.append({"code": "token_expiring", "facts": {"n": n}})
        # SRV-873: tokens near or at a spend limit
        if self.token_limits is not None:
            near, over = self.token_limits.near(self.tokens.limited())
            if over:
                out.append({"code": "token_limit_reached", "facts": {"n": over}})
            if near:
                out.append({"code": "token_limit_near", "facts": {"n": near}})
        return out

    def describe(self) -> str:
        accts = self.store.all()
        owners = sum(1 for a in accts if a["role"] == "owner" and a["enabled"])
        return (f"auth: accounts ({len(accts)} accounts, {owners} owners"
                f"{', setup needed' if self.store.setup_required() else ''})")

    # ------------------------------------------------------------------ who is asking
    def _tls_conn(self):
        return getattr(self._tls, "conn", None)

    @staticmethod
    def _h(headers, name: str) -> str:
        try:
            return (headers.get(name) or "").strip()
        except Exception:                                          # noqa: BLE001
            return ""

    def has_token(self, headers) -> bool:
        return bool(self._h(headers, "Authorization") or self._h(headers, "x-api-key"))

    def cookie_value(self, headers, peer: str = "") -> str | None:
        """The session cookie of the request's scheme: over https only __Host-tandem_session,
        over http only tandem_session (a cookie set over http never opens an https session)."""
        name = COOKIE_HTTPS if self.secure(peer, headers) else COOKIE_HTTP
        for part in self._h(headers, "Cookie").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name and v:
                return v
        return None

    def relay_principal(self, headers, peer: str = "") -> Principal | None:
        """SRV-869: the relay to /plugin-backends/* (proxy/pluginweb.py): this process's own
        secret, from a loopback peer. Its identity holds `plugin-backend` and nothing else."""
        if peer not in auth_mod.LOOPBACK:
            return None
        value, bad = tokens_mod.credential(headers)
        if bad is not None or not value or \
                not hmac.compare_digest(value.encode(), self.relay_secret.encode()):
            return None
        scopes = frozenset({"plugin-backend"})
        return Principal(kind="service", id=tokens_mod.PLUGIN_SERVICE, name="plugin-host",
                         via="token", scopes=scopes, perms=scopes)

    def relay_key(self, url: str, own_url: str | None) -> str | None:
        """The bearer the relay sends to a backend at `url`: the secret above for this
        controller's own /plugin-backends/*, else None (proxy/config.py key_for)."""
        if not own_url or not isinstance(url, str):
            return None
        return self.relay_secret if url.startswith(own_url.rstrip("/") + "/plugin-backends/") \
            else None

    def resolve(self, headers, peer: str = "", path: str = "") -> Principal | None:
        if self.has_token(headers):
            if self.service_resolver is not None:
                got = self.service_resolver(headers, peer)
                if got is not None:
                    return got
            got = self.token_resolver(headers) if self.token_resolver is not None else None
            if isinstance(got, tokens_mod.Refused) and not got.known and \
                    self.local_mode is not None:
                # SEC-28: in local mode a key no token has (most local tools send one, made up)
                # is as no key: this machine is the owner `local`. A known token stays itself,
                # and a revoked or expired one stays refused: the caller meant that token
                loc = self.local_mode(peer, headers)
                if loc is not None:
                    return loc
            return got
        if path.startswith("/s/"):
            from tandemapp import session_tokens as st_mod
            _rest, tok = st_mod.strip(path)
            reg = self._session_tokens() if tok else None
            key = reg.key_for(tok) if reg is not None else None
            return (Principal(kind=authz.SESSION_ADDRESS, id=key, name="session address",
                              via="address") if key else None)
        sid = self.cookie_value(headers, peer)
        if sid:
            got = self.store.lookup(sid)
            if got is not None:
                row, acc = got
                return Principal(kind="account", id=acc["id"], name=acc["username"],
                                 role=acc["role"], via="cookie", session=row["id_hash"],
                                 csrf=csrf_of(sid),
                                 expires_at=min(row["expires_at"], row["idle_until"]),
                                 must_change=bool(acc["must_change"]))
        if self.local_mode is not None:
            return self.local_mode(peer, headers)
        return None

    session_tokens = None                   # () -> the session address registry (app.py sets it)

    def _session_tokens(self):
        return self.session_tokens() if self.session_tokens is not None else None

    def current(self, headers) -> Principal | None:
        if getattr(self._tls, "headers", None) is headers:
            return self._tls.principal
        p = self.resolve(headers)
        return None if isinstance(p, tokens_mod.Refused) else p

    def request_principal(self) -> Principal | None:
        """The principal the gate found for the request this thread serves (a handler that
        rebuilt the request's headers, as the Messages API's does, asks this way)."""
        return getattr(self._tls, "principal", None)

    def who_of(self, handler) -> str:
        p = getattr(handler, "_principal", None) or self.current(handler.headers)
        if p is None:
            return "anonymous"
        if p.kind == "local":
            return "local"                  # this machine in local mode, as the audit said before
        return p.name if p.kind == "account" else f"{p.kind}:{p.name}"

    # ------------------------------------------------------------------ a route kind's check
    def decide(self, route: str, peer: str, headers) -> str:
        """`ok`, `unauthorized` (no principal) or `forbidden` for a route kind of the handlers
        (proxy/authz.py KIND_PERMISSION): the writes of the fleet, settings, config and
        conductor families are checked here, after the gate's read."""
        perm = authz.KIND_PERMISSION.get(route)
        p = self.current(headers)
        if perm is None:
            return "forbidden"
        if p is None:
            return "unauthorized"
        return "ok" if authz.allow(p, perm, self.policy) else "forbidden"

    def signed_in(self, headers) -> bool:
        return self.current(headers) is not None

    def session(self, headers, now=None) -> int | None:
        p = self.current(headers)
        return int(p.expires_at // 1000) if p is not None and p.via == "cookie" else None

    def revoke(self, headers) -> None:
        p = self.current(headers)
        if p is not None and p.session:
            self.store.revoke([p.session], actor=p.name, why="signed out")

    def login_key(self, peer: str, headers) -> str:
        return self.limits.key(peer, headers)

    def login_blocked(self, who: str) -> bool:
        return self.limits.by_address.login_blocked(who)

    def login_failed(self, who: str) -> None:
        self.limits.by_address.login_failed(who)

    # ------------------------------------------------------------------ the cookie
    def secure(self, peer: str, headers) -> bool:
        if self.listener_tls:
            return True
        if self._h(headers, "X-Forwarded-Proto").lower() != "https":
            return False
        return peer in auth_mod.LOOPBACK or peer in self.trusted_proxies

    def cookie(self, sid: str | None, peer: str, headers) -> str:
        secure = self.secure(peer, headers)
        name = COOKIE_HTTPS if secure else COOKIE_HTTP
        age = self.policy.idle_days * 86400 if sid else 0
        return (f"{name}={sid or ''}; HttpOnly; SameSite=Strict; Path=/; Max-Age={age}"
                + ("; Secure" if secure else ""))

    # ------------------------------------------------------------------ the gate
    def _refuse(self, handler, method: str, status: int, code: str, message: str,
                extra: tuple = (), **facts) -> bool:
        if method not in ("GET", "HEAD"):
            handler.close_connection = True          # its body stays unread
        raw = handler.path.split("?", 1)[0]
        if _messages_shape(handler, raw):
            # the Messages API's routes refuse in its own shape, as its handler does: the same
            # request id in the body and the header
            rid = tokens_mod.request_id()
            body = tokens_mod.refusal(raw, status, code, message, shape="messages")
            handler._json(status, {**body, "request_id": rid},
                          extra_headers=tuple(extra) + (("request-id", rid),))
            return True
        handler._json(status, {"error": {"type": "unauthorized" if status == 401 else
                                         "forbidden" if status == 403 else code,
                                         "code": code, "message": message, **facts}},
                      extra_headers=extra)
        return True

    def _csrf(self, handler, method: str, path: str, p: Principal):
        h = handler.headers
        if method in ("GET", "HEAD"):
            if self._h(h, "Upgrade").lower() == "websocket":
                if not self._h(h, "Origin"):
                    return 403, "cross_origin", "a WebSocket needs an Origin naming this server"
                bad = auth_mod.same_origin(h)
                return bad[:2] + ("a WebSocket from another origin is refused",) if bad else None
            if "text/event-stream" in self._h(h, "Accept").lower():
                bad = auth_mod.same_origin(h)
                return bad[:2] + ("a stream from another site or origin is refused",) if bad \
                    else None
            return None
        if method == "POST" and path == "/v1/auth/session":
            return None                              # the sign-in: its own same-origin check
        bad = auth_mod.same_origin(h)
        if bad is not None:
            return bad[0], bad[1], "a write from another site or origin is refused"
        has_body = (self._h(h, "Content-Length") not in ("", "0")
                    or "chunked" in self._h(h, "Transfer-Encoding").lower())
        if has_body and not path.startswith(BINARY_WRITES):
            ctype = self._h(h, "Content-Type").split(";")[0].strip().lower()
            if ctype != "application/json":
                return 415, "json_only", "a write takes a JSON body (Content-Type: " \
                                         "application/json)"
        got = self._h(h, CSRF_HEADER)
        if not got or not hmac.compare_digest(got.encode(), (p.csrf or "").encode()):
            return 403, "csrf", f"a write needs the {CSRF_HEADER} header of this session"
        return None

    def gate(self, handler, method: str) -> bool:
        """True when the request was refused (answered here)."""
        raw = handler.path.split("?", 1)[0]
        path = raw.rstrip("/") or "/"
        # an answer written without end_headers (a relayed one) never sent the renewal; it must
        # not ride on the next request of the connection, whoever sends that
        handler.__dict__.pop("_set_cookie", None)
        handler._principal = None
        self._tls.headers, self._tls.principal, self._tls.conn = handler.headers, None, None
        perm = authz.permission_for(method, raw)
        if perm is None:
            key = (method, path[:200])
            if key not in self._unmapped and len(self._unmapped) < UNMAPPED_LOGGED:
                self._unmapped.add(key)
                self.log(f"[auth] {method} {_clean(path, 200)}: no permission is defined for "
                         f"this route; refused")
            return self._refuse(handler, method, 403, "unmapped_route",
                                "no permission is defined for this route")
        peer = handler._peer()
        token_call = self.has_token(handler.headers)
        p = self.resolve(handler.headers, peer, raw)
        if token_call:
            strip_credentials(handler.headers)       # SRV-867: checked; never passed on
        if isinstance(p, tokens_mod.Refused):
            if perm != authz.OPEN:
                return self._token_refuse(handler, method, raw, p.status, p.code, p.message,
                                          **p.facts)
            p = None                                 # an open route: as without a token
        handler._principal = p
        self._tls.principal, self._tls.conn = p, getattr(handler, "connection", None)
        if p is not None and p.via == "local":
            bad = self._local_csrf(handler, method, path)
            if bad is not None:
                return self._refuse(handler, method, *bad)
        if p is not None and p.via == "cookie":
            bad = self._csrf(handler, method, path, p)
            if bad is not None:
                return self._refuse(handler, method, *bad)
            row = self.store.session_row(p.session)
            if row is not None and self.store.touch(row, self.limits.key(peer, handler.headers)):
                sid = self.cookie_value(handler.headers, peer)
                handler._set_cookie = self.cookie(sid, peer, handler.headers)
            conn = getattr(handler, "connection", None)
            if conn is not None:
                self.streams.add(p.session, conn)
                handler._stream_key = p.session
        if perm == authz.OPEN:
            return False
        if p is None:
            return self._refuse(handler, method, 401, "sign_in_required",
                                "sign in, or send an API token",
                                extra=(("WWW-Authenticate", 'Bearer realm="TandemLLM"'),))
        if p.must_change and (method, path) not in MUST_CHANGE_OK:
            return self._refuse(handler, method, 403, "password_change_required",
                                "set a new password first (PUT /v1/auth/password)")
        if p.via == "token":
            if self._token_gate(handler, method, raw, peer, perm, p):
                return True
            conn = getattr(handler, "connection", None)
            if conn is not None and p.id != tokens_mod.PLUGIN_SERVICE:
                # SRV-869: a revoked token's open stream is closed as an ended session's is
                key = TOKEN_STREAM + p.id
                self.streams.add(key, conn)
                handler._stream_key = key
            return False
        if not authz.allow(p, perm, self.policy):
            return self._refuse(handler, method, 403, "permission_denied",
                                f"this needs the permission {perm}", permission=perm)
        return False

    def _token_gate(self, handler, method: str, raw: str, peer: str, perm: str, p) -> bool:
        """SRV-867: a token's route: one of its scopes, the exposure switch, its owner's role."""
        need = tokens_mod.scope_for(method, raw)
        if need is None:
            return self._token_refuse(handler, method, raw, 403, "permission_denied",
                                      "an API token cannot use this route; sign in")
        if not set(need) & set(p.scopes or ()):
            return self._token_refuse(handler, method, raw, 403, "permission_denied",
                                      f"this token lacks the scope {' or '.join(need)}",
                                      scope=need[0])
        if self.exposure is not None:
            bad = self.exposure(p, method, raw, peer, handler.headers)
            if bad is not None:
                return self._token_refuse(handler, method, raw, *bad)
        if not authz.allow(p, perm, self.policy):
            return self._token_refuse(handler, method, raw, 403, "permission_denied",
                                      f"the token's owner may not use {perm} now",
                                      permission=perm)
        return False

    def _token_refuse(self, handler, method: str, raw: str, status: int, code: str,
                      message: str, **facts) -> bool:
        if method not in ("GET", "HEAD"):
            handler.close_connection = True
        extra = (("WWW-Authenticate", 'Bearer realm="TandemLLM"'),) if status == 401 else ()
        shape, rid = None, None
        if _messages_shape(handler, raw):
            shape, rid = "messages", tokens_mod.request_id()
            extra += (("request-id", rid),)
        body = tokens_mod.refusal(raw, status, code, message, shape=shape, **facts)
        handler._json(status, {**body, "request_id": rid} if rid else body, extra_headers=extra)
        return True

    def _local_csrf(self, handler, method: str, path: str):
        """SEC-26: a local-mode request has no cookie and no token, so a page of another site
        open in this machine's browser could post as the owner: every write comes from this
        origin and declares JSON (a file upload excepted), and a WebSocket names this host."""
        h = handler.headers
        if method in ("GET", "HEAD"):
            if self._h(h, "Upgrade").lower() == "websocket":
                if not self._h(h, "Origin"):
                    return 403, "cross_origin", "a WebSocket needs an Origin naming this server"
                bad = auth_mod.same_origin(h)
                return bad[:2] + ("a WebSocket from another origin is refused",) if bad else None
            return None
        bad = auth_mod.same_origin(h)
        if bad is not None:
            return bad[0], bad[1], "a write from another site or origin is refused"
        has_body = (self._h(h, "Content-Length") not in ("", "0")
                    or "chunked" in self._h(h, "Transfer-Encoding").lower())
        if has_body and not path.startswith(BINARY_WRITES):
            ctype = self._h(h, "Content-Type").split(";")[0].strip().lower()
            if ctype != "application/json":
                return 415, "json_only", "a write takes a JSON body (Content-Type: " \
                                         "application/json)"
        return None

    def done(self, handler) -> None:
        """The request's end: its connection no longer counts as the session's stream."""
        key = handler.__dict__.pop("_stream_key", None)
        if key is not None:
            self.streams.remove(key, getattr(handler, "connection", None))

    def permissions(self, p: Principal | None) -> list[str]:
        if p is None:
            return []
        if p.kind == "account":
            return sorted(authz.role_permissions(p.role, self.policy))
        return sorted(p.scopes or ())
