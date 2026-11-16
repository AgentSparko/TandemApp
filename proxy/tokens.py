"""API tokens (SRV-867), with TANDEM_AUTH=accounts only.

A token has a label, an owner, scopes, a model list, an expiry and optional limits. It is shown
once and stored as the SHA-256 of the whole token (config.key_hash), with its id and its last four
characters. The format is `tdm-<8 hex id>-<43 characters>`.

    TokenStore(account_store)
      create(owner, label=, scopes=, models=, days=, limits=, actor=) -> (view, token)
      resolve(headers) -> Principal | Refused | None      the request gate's token_resolver
      revoke(id, actor=, why=)  rotate(id, grace_s=, actor=) -> (view, token)
      confirm(id, actor=)  update(id, changes, actor=)  revoke_owner(account_id, actor=, why=)
      view(row)  mine(account_id)  all()  get(id)
      import_legacy(env, first_owner=) -> dict                     at each start (authlegacy)
    scope_for(method, path) -> tuple | None      the scopes a route takes from a token
    effective(scopes, role, kind, policy) -> frozenset           the permissions a token acts with
    refusal(path, status, code, message, **facts) -> dict       the error in the route's protocol

THE TABLE `api_tokens` (in the config database, new; the old `users` table keeps its schema):

    id, kind (user | legacy | service), owner_id (an account; NULL for an imported key and a
    service identity), label (unique per owner among live tokens), hash (UNIQUE), last4,
    scopes, models (JSON lists), limits (JSON), memory_user (sent upstream as X-Tandem-User and
    written to the ledger's `user`: the owner's username, an imported key's old label), source
    (created | users | env | users_from | admin_token), legacy_user_id (the `users` row an import
    came from), needs_review, created_at, created_by, expires_at (NULL: never), revoked_at,
    revoked_by, revoke_why, last_used_at, prev_hash and prev_until (a rotated secret's grace)

A revoked row stays: its hash keeps an import from bringing the key back, and its usage stays
named. Rows are never deleted here.

SCOPES. `inference:openai` and `inference:anthropic` (each protocol's routes, `/v1/models` for
both), `usage:read` (the ledger's summary, usage and requests views), `metrics:read`, `settings:read`, `settings:write`, `fleet:read`,
`fleet:write`, `trust` (admin-owned tokens only, 30 days at most), and the service scopes
`collect:write` and `chat-engine`, which no route can put on a token. Never on a token: users,
sessions, the identity provider and the exposure switches (the `/v1/auth/*` routes refuse every
token).

AT EACH REQUEST (the gate, proxy/accounts.py). The token comes from `Authorization: Bearer` or
`x-api-key` (both, when they differ: refused). Its hash is looked up (a rotated secret's too,
until its grace ends): not known, revoked, expired, or an owner disabled or gone is a 401 that
says which. The route takes the scopes `scope_for` names, else 403 naming the scope. The token
then acts with `effective`: what its scopes imply, cut to its owner's role of now, so a token
never has more than its owner, and a demoted owner's token loses the rights at once (403). The
model of a completion matches one of the token's globs (Service.resolve), else 403 naming it.
After the check both headers are removed from the request, so no backend ever sees the token.
`last_used_at` is written at most once a minute per token.

WHO MAKES WHAT (proxy/authz.py). Inference scopes need `tokens:inference` (operator and up; a
viewer while `api.tokens.viewers_may_create` is on), the others `tokens:management` (operator and
up), `trust` an admin or owner. Every scope's rights are within the owner's role. Expiry defaults
to `api.tokens.default_days` (90); "never" needs an admin; `api.tokens.max_days` (0: none) caps it.

THE IMPORT (once per source, at the start; a second run changes nothing: an existing hash, live
or revoked, is never imported again):
  - rows of `users`: one token each, the same hash, label and memory owner, scope
    `inference:openai`, no expiry, `needs_review`; a disabled row comes in revoked. The row
    `chatd` becomes the service identity `svc:chatd` (inference:openai, chat-engine), not a key
    under review. The row `rollback-guard` is skipped;
  - TANDEM_API_KEYS (source env) and TANDEM_API_USERS_FROM's users (source users_from): the same;
  - the admin token, once an owner exists: a management token (settings:read, settings:write,
    fleet:read, fleet:write, metrics:read; never trust) of the first owner, `needs_review`, 30
    days. It never signs in to the page. Health finding `legacy_admin_token` until it is revoked
    or expires.
After it no legacy variable and no row of `users` is read to let a request in.

THE WAY BACK (the release before reads `users`). A revoked import is also set `enabled = 0` in
`users`. Whenever a token exists and `users` has no enabled row, a row `rollback-guard` with the
hash of a secret nobody holds is written (or enabled again): the release before then needs a key
and fails closed, never open.

SERVICE IDENTITIES (SRV-869): rows of kind `service` with no owner, made by the controller and
listed read-only. `mint_service(label, scopes)` gives a fresh secret under the same row (the
one before stops at once): svc:chatd (inference:openai, chat-engine) at each start of chatd, into
its environment only, or `tandem-app tokens chatd-key FILE` for a chatd started elsewhere; svc:cli
(an operator's scopes, never trust) at each start of the controller into <state>/run/cli.token;
svc:collect:<backend> (collect:write, for that backend only) at each `tandem-app launch`. The
imports above add svc:metrics (QSE_METRICS_TOKEN) and svc:collect (the shared collect token, any
backend), both under Review. The plugin relay's identity lives in memory only
(proxy/accounts.py relay_principal). A service identity's scopes are not cut to a role; its routes
are scope_for's: chat-engine opens /chat-engine/* alone (with the reads and the conductor writes
chatd makes there), plugin-backend /plugin-backends/* alone.

A token never exceeds its owner's role of now (`effective`, at each request): an inference scope
counts only while the role may make inference tokens; the other scopes lose what the role lost.

LIMITS (SRV-873, proxy/tokenlimits.py): requests a minute, tokens a minute, requests at once,
dollars a day and a month. A new token takes its owner's role's values (`api.limits.default.<role>.
<key>`) where it names none, and names none above them (400 limit_over_role); its owner changes
them with PATCH under the same rule. At each request the lower of the token's and the role's of
now holds. Its usage: proxy/tokenusage.py.

SEAMS. `exposure` (SRV-871, proxy/exposure.py), `/v1/auth/tokens/{id}/usage` (SRV-873). The Messages API's routes (/v1/messages and below, SRV-879)
take `inference:anthropic` and refuse in their own shape.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field

from proxy import authz

PREFIX = "tdm-"
TOKEN_RE = re.compile(r"^tdm-([0-9a-f]{8})-[A-Za-z0-9_-]{43}$")
DAY_MS = 86_400_000
SEEN_FLUSH_S = 60.0
ADMIN_TOKEN_DAYS = 30
TRUST_MAX_DAYS = 30
GUARD_LABEL = "rollback-guard"
CHATD_LABEL = "chatd"
CHATD_SERVICE = "svc:chatd"
ADMIN_LABEL = "admin token"
LABEL_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,64}$")
MAX_MODELS = 64

INFERENCE = ("inference:openai", "inference:anthropic")
MANAGEMENT = ("usage:read", "metrics:read", "settings:read", "settings:write", "fleet:read",
              "fleet:write")
TRUST = "trust"
SERVICE_SCOPES = ("collect:write", "chat-engine", "plugin-backend")
#: what a person may put on a token (the service scopes are the controller's own)
USER_SCOPES = INFERENCE + MANAGEMENT + (TRUST,)
SCOPES = USER_SCOPES + SERVICE_SCOPES
ADMIN_TOKEN_SCOPES = ("settings:read", "settings:write", "fleet:read", "fleet:write",
                      "metrics:read")
CHATD_SCOPES = ("inference:openai", "chat-engine")
# SRV-869: the controller's own service identities (kind service, no owner, read-only on the page)
SERVICE_PREFIX = "svc:"
CLI_SERVICE = "svc:cli"                 # <state>/run/cli.token: this machine's CLI and scripts
CLI_SCOPES = INFERENCE + MANAGEMENT     # an operator's reach, never trust
COLLECT_SERVICE = "svc:collect"         # the imported shared collect token: every backend
METRICS_SERVICE = "svc:metrics"         # the imported QSE_METRICS_TOKEN
PLUGIN_SERVICE = "svc:plugin-host"      # the relay to /plugin-backends (in memory only)
CLI_TOKEN = ("run", "cli.token")        # under the state dir: the file, 0600; its folder 0700
COLLECT_SCOPES = ("collect:write",)
METRICS_SCOPES = ("metrics:read",)
#: imported service keys an admin confirms or revokes (a minted identity is TandemApp's own)
SERVICE_IMPORTS = ("collect_token", "metrics_token")
LIMIT_KEYS = ("requests_per_minute", "tokens_per_minute", "concurrent", "cost_per_day",
              "cost_per_month")

#: the permissions of proxy/authz.py a scope gives, before the owner's role cuts them
IMPLIES = {
    "inference:openai": {"inference"}, "inference:anthropic": {"inference"},
    "usage:read": {"read"}, "metrics:read": {"metrics:read", "read"},
    "settings:read": {"read"}, "settings:write": {"read", "settings:write"},
    "fleet:read": {"read", "fleet:read"}, "fleet:write": {"read", "fleet:read", "fleet:write"},
    # SRV-869: trust takes the move's reads with it (GET /v1/fleet/controller before a take);
    # chatd's identity reads the chat engine's facts and writes its user skills to the conductor,
    # on /chat-engine/* only (scope_for keeps every other route from it)
    "trust": {"trust", "read", "fleet:read"}, "collect:write": {"collect:write"},
    "chat-engine": {"chat-engine", "read", "settings:write"},
    "plugin-backend": {"plugin-backend"},
}
#: SRV-869: what the owner's role of now must hold for a scope of the token to count at all (an
#: inference scope: the right to make one; the others are cut to the role's permissions)
CREATE_RIGHT = {s: "tokens:inference" for s in INFERENCE}

MESSAGES = ("/v1/messages", "/v1/messages/count_tokens")
OPENAI_EXTRA = ("/v1/library", "/v1/laya")
SETTINGS_FAMILIES = ("/v1/settings", "/v1/config", "/v1/conductor")
#: the ledger's views (usage:read); the page's other reads (logs, live, system) take no token
LEDGER_VIEWS = ("summary", "usage", "requests")

DDL = """
CREATE TABLE IF NOT EXISTS api_tokens (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, owner_id TEXT, label TEXT NOT NULL,
  hash TEXT NOT NULL UNIQUE, last4 TEXT, scopes TEXT NOT NULL, models TEXT NOT NULL DEFAULT '[]',
  limits TEXT NOT NULL DEFAULT '{}', memory_user TEXT NOT NULL, source TEXT NOT NULL,
  legacy_user_id INTEGER, needs_review INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL,
  created_by TEXT, expires_at INTEGER, revoked_at INTEGER, revoked_by TEXT, revoke_why TEXT,
  last_used_at INTEGER, prev_hash TEXT, prev_until INTEGER);
CREATE UNIQUE INDEX IF NOT EXISTS api_tokens_label ON api_tokens(owner_id, label)
  WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS api_tokens_prev ON api_tokens(prev_hash) WHERE prev_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS api_tokens_owner ON api_tokens(owner_id);
"""
COLS = ("id", "kind", "owner_id", "label", "hash", "last4", "scopes", "models", "limits",
        "memory_user", "source", "legacy_user_id", "needs_review", "created_at", "created_by",
        "expires_at", "revoked_at", "revoked_by", "revoke_why", "last_used_at", "prev_hash",
        "prev_until")


def cli_token_path(state_dir: str) -> str:
    """SRV-869: where the controller leaves svc:cli's secret at each start."""
    return os.path.join(os.path.expanduser(state_dir), *CLI_TOKEN)


def read_cli_token(state_dir: str) -> str | None:
    """The CLI's token when this user may read it (the owner of the state dir), else None."""
    try:
        with open(cli_token_path(state_dir)) as f:
            got = f.read(512).strip()
    except OSError:
        return None
    return got if TOKEN_RE.match(got) else None


def key_hash(token: str) -> str:
    """config.key_hash: the SHA-256 of the whole token, hex."""
    return hashlib.sha256(token.encode()).hexdigest()


def _norm(path: str) -> str:
    return path.split("?", 1)[0].rstrip("/") or "/"


def _under(p: str, family: str) -> bool:
    return p == family or p.startswith(family + "/")


def scope_for(method: str, path: str) -> tuple | None:
    """The scopes (any one will do) a route takes from a token; None: no token may call it."""
    from proxy import deployments
    from proxy.service import PROXY_PATHS
    p = _norm(path)
    read = method in ("GET", "HEAD")
    if p in PROXY_PATHS or p in deployments.CACHE_PATHS or p == authz.GATEWAY:
        return ("inference:openai",)
    if _under(p, MESSAGES[0]):
        return ("inference:anthropic",)
    if _under(p, "/v1/models"):
        return INFERENCE
    if any(_under(p, f) for f in OPENAI_EXTRA):
        return ("inference:openai",)
    if p == "/metrics":
        return ("metrics:read",)
    if any(_under(p, f) for f in SETTINGS_FAMILIES):
        return ("settings:read",) if read else ("settings:write",)
    if read and any(p == f"/v1/fleet/ledger/{v}" for v in LEDGER_VIEWS):
        return ("usage:read", "fleet:read")
    if _under(p, "/v1/fleet"):
        # SRV-869: a trust token pairs, releases, installs a plugin and moves the controller; the
        # handler's route kind then asks for the permission itself (fleet_trust: trust)
        return ("fleet:read", TRUST) if read else ("fleet:write", TRUST)
    if read and any(p == f"/v1/dashboard/{v}" for v in LEDGER_VIEWS):
        return ("usage:read",)
    if _under(p, "/v1/collect"):
        return ("collect:write",)
    if _under(p, "/chat-engine"):
        return ("chat-engine",)
    if _under(p, "/plugin-backends"):
        return ("plugin-backend",)
    return None


def anthropic(path: str) -> bool:
    """A route of the Messages API (/v1/messages and every path under it)."""
    return _under(_norm(path), MESSAGES[0])


def request_id() -> str:
    """The `request-id` header of a refusal on the Messages API's routes (its handler's form)."""
    return "req_" + secrets.token_hex(12)


def refusal(path: str, status: int, code: str, message: str, shape: str | None = None,
            **facts) -> dict:
    """A token's refusal in the protocol of the route: the Messages API's shape on its routes
    (and with `shape` "messages", the model list a Messages API client asked for), else the
    OpenAI shape (`invalid_api_key`, `permission_denied`, `api_not_exposed`)."""
    if anthropic(path) or shape == "messages":
        from proxy.translate import anthropic as tr
        return {"type": "error", "error": {"type": tr.error_type(status), "message": message}}
    return {"error": {"type": "invalid_request_error" if status == 401 else code, "code": code,
                      "message": message, **facts}}


def effective(scopes, role: str | None, kind: str, policy=None, owned: bool = True) -> frozenset:
    """What a token may do now: its scopes' permissions, cut to its owner's role (a service
    identity: its scopes; an imported key without an owner: at most a viewer's).

    SRV-869: a token never exceeds its owner's role of now, checked at each request. An inference
    scope counts only while that role may make inference tokens (CREATE_RIGHT: an operator's
    inference token stops when the owner becomes a viewer while `api.tokens.viewers_may_create`
    is off); a management scope loses what the role lost (a settings:write token of an operator
    made a viewer keeps the viewer's reads); trust needs an admin. Nothing is revoked: a promotion
    back gives the rights back at the next request."""
    got: set = set()
    if kind == "service":
        for s in scopes or ():
            got |= IMPLIES.get(s, set())
        return frozenset(got)
    cap = authz.role_permissions(role or "viewer", policy)
    for s in scopes or ():
        need = CREATE_RIGHT.get(s)
        if owned and need is not None and need not in cap:
            continue
        got |= IMPLIES.get(s, set())
    return frozenset(got & cap)


@dataclass
class Refused:
    """A token the gate answers with 401: unknown, revoked, expired, or its owner disabled."""
    status: int
    code: str
    message: str
    facts: dict = field(default_factory=dict)


class TokenError(Exception):
    def __init__(self, status: int, code: str, message: str, **facts):
        super().__init__(message)
        self.status, self.code, self.message, self.facts = status, code, message, facts

    def body(self) -> dict:
        return {"error": {"type": self.code, "code": self.code, "message": self.message,
                          **self.facts}}


def credential(headers) -> tuple[str | None, Refused | None]:
    """The token a request carries: Authorization's Bearer, or x-api-key."""
    def h(name):
        try:
            return (headers.get(name) or "").strip()
        except Exception:                                          # noqa: BLE001
            return ""
    auth, key = h("Authorization"), h("x-api-key")
    bearer = None
    if auth:
        if auth[:7].lower() != "bearer ":
            return None, Refused(401, "invalid_api_key", "send the token as Authorization: "
                                                         "Bearer <token> or in x-api-key")
        bearer = auth[7:].strip()
    if bearer and key and bearer != key:
        return None, Refused(401, "invalid_api_key", "Authorization and x-api-key name "
                                                     "different tokens")
    value = bearer or key
    if not value or len(value) > 512:
        return None, Refused(401, "invalid_api_key", "the token is not known")
    return value, None


def _iso(ms) -> str | None:
    if not ms:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(ms) // 1000))


class TokenStore:
    """The table above over proxy/accounts.py's AccountStore (its database, its lock, its clock,
    its policy)."""

    def __init__(self, store, *, policy=None):
        self.store = store
        self.cfg = store.cfg
        self.policy = policy or store.policy
        self._seen: dict[str, int] = {}
        self._flushed = 0.0
        with self.cfg._lock:
            self.cfg._conn.executescript(DDL)
            self.cfg._conn.commit()

    on_revoked = None               # SRV-869: (token ids) -> None, the open streams' close

    @property
    def _c(self):
        return self.cfg._conn

    def now_ms(self) -> int:
        return self.store.now_ms()

    # ------------------------------------------------------------------ rows
    @staticmethod
    def _row(r) -> dict | None:
        if r is None:
            return None
        d = dict(zip(COLS, r))
        for k in ("scopes", "models"):
            d[k] = json.loads(d[k] or "[]")
        d["limits"] = json.loads(d["limits"] or "{}")
        d["needs_review"] = bool(d["needs_review"])
        return d

    def get(self, tid: str) -> dict | None:
        with self.cfg._lock:
            r = self._c.execute(f"SELECT {', '.join(COLS)} FROM api_tokens WHERE id = ?",
                                (tid,)).fetchone()
        return self._row(r)

    def rows(self, where: str = "", args: tuple = ()) -> list[dict]:
        with self.cfg._lock:
            got = self._c.execute(f"SELECT {', '.join(COLS)} FROM api_tokens {where} ORDER BY "
                                  f"created_at, id", args).fetchall()
        return [self._row(r) for r in got]

    def mine(self, account_id: str) -> list[dict]:
        return self.rows("WHERE owner_id = ?", (account_id,))

    def all(self) -> list[dict]:
        return self.rows()

    def state(self, row: dict, now: int | None = None) -> str:
        now = self.now_ms() if now is None else now
        if row["revoked_at"] is not None:
            return "revoked"
        if row["expires_at"] is not None and now >= row["expires_at"]:
            return "expired"
        return "active"

    def view(self, row: dict) -> dict:
        """A token as an answer holds it: never the token, never its hash."""
        owner = self.store.get(row["owner_id"]) if row["owner_id"] else None
        return {"id": row["id"], "kind": row["kind"], "label": row["label"],
                "owner": owner["username"] if owner else None, "owner_id": row["owner_id"],
                "hint": f"{PREFIX}{row['id']}-...{row['last4']}" if row["source"] == "created"
                else (f"...{row['last4']}" if row["last4"] else None),
                "scopes": row["scopes"], "models": row["models"], "limits": row["limits"],
                "memory_user": row["memory_user"], "source": row["source"],
                "needs_review": row["needs_review"], "state": self.state(row),
                "created_at": _iso(row["created_at"]), "expires_at": _iso(row["expires_at"]),
                "revoked_at": _iso(row["revoked_at"]),
                "last_used_at": _iso(self._seen.get(row["id"]) or row["last_used_at"])}

    # ------------------------------------------------------------------ making one
    def _days(self, days, owner_role: str, trust: bool) -> int | None:
        p = self.policy
        admin = authz.rank(owner_role) >= authz.rank("admin")
        if days in (None, ""):
            try:
                days = int(p.get("api.tokens.default_days", 90))
            except (TypeError, ValueError):
                days = 90
        if days == "never" or days == 0:
            if trust:
                raise TokenError(400, "bad_request", f"a token with trust lives at most "
                                                     f"{TRUST_MAX_DAYS} days")
            if not admin:
                raise TokenError(403, "permission_denied", "a token that never expires needs "
                                                           "an admin", permission="users")
            return None
        if not isinstance(days, int) or isinstance(days, bool) or days < 1:
            raise TokenError(400, "bad_request", "days is a whole number of 1 or more, or "
                                                 "\"never\"")
        try:
            cap = int(p.get("api.tokens.max_days", 0) or 0)
        except (TypeError, ValueError):
            cap = 0
        if cap and days > cap:
            raise TokenError(400, "bad_request", f"a token lives at most {cap} days here "
                                                 f"(api.tokens.max_days)")
        if trust and days > TRUST_MAX_DAYS:
            raise TokenError(400, "bad_request", f"a token with trust lives at most "
                                                 f"{TRUST_MAX_DAYS} days")
        return days

    def check_scopes(self, scopes, role: str) -> list[str]:
        """The scopes a person of `role` may put on a token (403 names what is missing)."""
        if not isinstance(scopes, list) or not scopes or \
                not all(isinstance(s, str) for s in scopes):
            raise TokenError(400, "bad_request", f"scopes is a list of: "
                                                 f"{', '.join(USER_SCOPES)}")
        out = list(dict.fromkeys(scopes))
        unknown = [s for s in out if s not in SCOPES]
        if unknown:
            raise TokenError(400, "bad_request", f"no scope {unknown[0]!r}; the scopes are "
                                                 f"{', '.join(USER_SCOPES)}")
        service = [s for s in out if s in SERVICE_SCOPES]
        if service:
            raise TokenError(403, "permission_denied", f"{service[0]} belongs to service "
                                                       f"identities only", scope=service[0])
        perms = authz.role_permissions(role, self.policy)
        if any(s in INFERENCE for s in out) and "tokens:inference" not in perms:
            raise TokenError(403, "permission_denied",
                             "an admin must allow viewers to make inference tokens "
                             "(api.tokens.viewers_may_create)" if role == "viewer" else
                             "this role makes no inference tokens", permission="tokens:inference")
        if any(s in MANAGEMENT for s in out) and "tokens:management" not in perms:
            raise TokenError(403, "permission_denied", "this role makes no management tokens",
                             permission="tokens:management")
        if TRUST in out and authz.rank(role) < authz.rank("admin"):
            raise TokenError(403, "permission_denied", "only an admin's token holds trust",
                             permission="trust")
        for s in out:
            over = IMPLIES[s] - perms
            if over:
                raise TokenError(403, "permission_denied", f"the scope {s} needs more than "
                                                           f"this role has", scope=s)
        return out

    @staticmethod
    def check_models(models) -> list[str]:
        if models is None:
            return []
        if not isinstance(models, list) or len(models) > MAX_MODELS or \
                not all(isinstance(m, str) and 0 < len(m) <= 200 and m.strip() == m
                        for m in models):
            raise TokenError(400, "bad_request", f"models is a list of at most {MAX_MODELS} "
                                                 f"model ids or globs (\"small-*\")")
        return list(dict.fromkeys(models))

    @staticmethod
    def check_limits(limits) -> dict:
        if limits is None:
            return {}
        if not isinstance(limits, dict) or set(limits) - set(LIMIT_KEYS) or \
                not all(v is None or (isinstance(v, (int, float)) and not isinstance(v, bool)
                                      and 0 <= v <= 1e12) for v in limits.values()):
            raise TokenError(400, "bad_request", f"limits takes {', '.join(LIMIT_KEYS)}, each "
                                                 f"a number of 0 or more")
        return {k: v for k, v in limits.items() if v is not None}

    def role_limits(self, limits: dict, role: str | None) -> dict:
        """SRV-873: a token's limits under its owner's role: the role's value where the token
        names none (or 0), and none above it (400 limit_over_role)."""
        from proxy import tokenlimits
        caps = tokenlimits.role_caps(self.policy, role)
        out = {k: v for k, v in limits.items() if v}
        for k, cap in caps.items():
            if k not in out:
                out[k] = cap
            elif out[k] > cap:
                raise TokenError(400, "limit_over_role", f"{tokenlimits.WORDS[k]}: at most "
                                 f"{cap} for a token of a {role or 'viewer'}", limit=k, most=cap)
        return {k: out[k] for k in LIMIT_KEYS if k in out}

    @staticmethod
    def check_label(label) -> str:
        if not isinstance(label, str) or not LABEL_RE.match(label.strip()):
            raise TokenError(400, "bad_request", "a label is 1 to 64 characters")
        return label.strip()

    def create(self, owner: dict, *, label, scopes, models=None, days=None, limits=None,
               actor: str) -> tuple[dict, str]:
        """A token of `owner` (an account row): its view and the token, which is never shown
        again."""
        if owner is None or not owner.get("enabled"):
            raise TokenError(403, "permission_denied", "a disabled account makes no tokens")
        label = self.check_label(label)
        scopes = self.check_scopes(scopes, owner["role"])
        models = self.check_models(models)
        limits = self.role_limits(self.check_limits(limits), owner["role"])
        days = self._days(days, owner["role"], TRUST in scopes)
        tid, token = self._new_secret()
        now = self.now_ms()
        with self.cfg._lock:
            if self._c.execute("SELECT 1 FROM api_tokens WHERE owner_id = ? AND label = ? AND "
                               "revoked_at IS NULL", (owner["id"], label)).fetchone():
                raise TokenError(409, "conflict", f"you have a token {label!r} already")
            self._c.execute(
                f"INSERT INTO api_tokens({', '.join(COLS)}) VALUES "
                f"({', '.join('?' * len(COLS))})",
                (tid, "user", owner["id"], label, key_hash(token), token[-4:],
                 json.dumps(scopes), json.dumps(models), json.dumps(limits),
                 owner["username"], "created", None, 0, now, actor,
                 now + days * DAY_MS if days else None, None, None, None, None, None, None))
            self.cfg._audit(actor, "create", "api_token", tid,
                            detail={"label": label, "owner": owner["username"],
                                    "scopes": scopes, "days": days})
            self._c.commit()
            self.ensure_guard()
        return self.view(self.get(tid)), token

    def _new_secret(self) -> tuple[str, str]:
        while True:
            tid = secrets.token_hex(4)
            if self.get(tid) is None:
                break
        return tid, f"{PREFIX}{tid}-{secrets.token_urlsafe(32)}"

    # ------------------------------------------------------------------ changing one
    def rotate(self, tid: str, *, grace_s: int = 0, actor: str) -> tuple[dict, str]:
        """A new secret under the same id and label; the old one works `grace_s` more seconds."""
        if not isinstance(grace_s, int) or isinstance(grace_s, bool) or \
                not 0 <= grace_s <= 7 * 86400:
            raise TokenError(400, "bad_request", "grace_s is 0 to 604800 seconds")
        row = self.get(tid)
        if row is None:
            raise TokenError(404, "not_found", "no such token")
        if row["kind"] != "user" or self.state(row) != "active":
            raise TokenError(409, "conflict", "only an active token made here rotates; make a "
                                              "new one")
        token = f"{PREFIX}{tid}-{secrets.token_urlsafe(32)}"
        now = self.now_ms()
        with self.cfg._lock:
            self._c.execute("UPDATE api_tokens SET hash = ?, last4 = ?, prev_hash = ?, "
                            "prev_until = ? WHERE id = ?",
                            (key_hash(token), token[-4:], row["hash"] if grace_s else None,
                             now + grace_s * 1000 if grace_s else None, tid))
            self.cfg._audit(actor, "rotate", "api_token", tid, detail={"grace_s": grace_s})
            self._c.commit()
        return self.view(self.get(tid)), token

    def update(self, tid: str, changes: dict, *, actor: str) -> dict:
        row = self.get(tid)
        if row is None:
            raise TokenError(404, "not_found", "no such token")
        if not isinstance(changes, dict) or not changes or \
                set(changes) - {"label", "models", "limits"}:
            raise TokenError(400, "bad_request", "the body is {label?, models?, limits?}")
        label = self.check_label(changes.get("label", row["label"]))
        models = self.check_models(changes.get("models", row["models"]))
        limits = row["limits"]
        if "limits" in changes:
            # SRV-873: the whole set, under the owner's role of now
            owner = self.store.get(row["owner_id"]) if row["owner_id"] else None
            limits = self.role_limits(self.check_limits(changes["limits"]),
                                      owner["role"] if owner else None)
        with self.cfg._lock:
            if label != row["label"] and self._c.execute(
                    "SELECT 1 FROM api_tokens WHERE owner_id IS ? AND label = ? AND revoked_at "
                    "IS NULL AND id != ?", (row["owner_id"], label, tid)).fetchone():
                raise TokenError(409, "conflict", f"a token {label!r} exists already")
            self._c.execute("UPDATE api_tokens SET label = ?, models = ?, limits = ? WHERE id = ?",
                            (label, json.dumps(models), json.dumps(limits), tid))
            self.cfg._audit(actor, "update", "api_token", tid, fields=sorted(changes))
            self._c.commit()
        return self.view(self.get(tid))

    def confirm(self, tid: str, *, actor: str) -> dict:
        """An imported key reviewed and kept: it leaves the Review list."""
        row = self.get(tid)
        if row is None:
            raise TokenError(404, "not_found", "no such token")
        with self.cfg._lock:
            self._c.execute("UPDATE api_tokens SET needs_review = 0 WHERE id = ?", (tid,))
            self.cfg._audit(actor, "confirm", "api_token", tid)
            self._c.commit()
        return self.view(self.get(tid))

    def revoke(self, tid: str, *, actor: str, why: str = "revoked") -> dict:
        row = self.get(tid)
        if row is None:
            raise TokenError(404, "not_found", "no such token")
        self._revoke_rows([row], actor, why)
        return self.view(self.get(tid))

    def revoke_owner(self, account_id: str, *, actor: str, why: str) -> int:
        """Every live token of an account (it was disabled or removed)."""
        rows = [r for r in self.mine(account_id) if r["revoked_at"] is None]
        self._revoke_rows(rows, actor, why)
        return len(rows)

    def _revoke_rows(self, rows: list, actor: str, why: str) -> None:
        if not rows:
            return
        now = self.now_ms()
        done: list[str] = []
        with self.cfg._lock:
            for row in rows:
                cur = self._c.execute("UPDATE api_tokens SET revoked_at = ?, revoked_by = ?, "
                                      "revoke_why = ?, needs_review = 0, prev_hash = NULL, "
                                      "prev_until = NULL WHERE id = ? AND revoked_at IS NULL",
                                      (now, actor, why[:120], row["id"]))
                if not cur.rowcount:
                    continue
                if row["legacy_user_id"] is not None:
                    # the way back: the release before reads `users`; a revoked key stays off
                    self._c.execute("UPDATE users SET enabled = 0 WHERE id = ? AND key_hash = ?",
                                    (row["legacy_user_id"], row["hash"]))
                self.cfg._audit(actor, "revoke", "api_token", row["id"],
                                detail={"label": row["label"], "why": why[:120]})
                done.append(row["id"])
            self._c.commit()
            self.ensure_guard()
        if done and self.on_revoked is not None:
            self.on_revoked(done)

    def live(self, tid: str) -> bool:
        """SRV-869: may the token `tid` still be used (its open stream stays)? Not revoked, not
        expired, and its owner, when it has one, enabled."""
        with self.cfg._lock:
            r = self._c.execute("SELECT t.revoked_at, t.expires_at, t.owner_id, a.enabled FROM "
                                "api_tokens t LEFT JOIN accounts a ON a.id = t.owner_id WHERE "
                                "t.id = ?", (tid,)).fetchone()
        if r is None or r[0] is not None:
            return False
        if r[1] is not None and self.now_ms() >= r[1]:
            return False
        return r[2] is None or bool(r[3])

    # ------------------------------------------------------------------ service identities
    def mint_service(self, label: str, scopes, *, memory_user: str | None = None,
                     actor: str = "controller") -> str:
        """SRV-869: a fresh secret for the service identity `label` (kind service, no owner):
        its live row keeps its id and gets the new hash in place, so the secret before stops at
        once; none yet: a new row. The secret is returned once and never stored."""
        if not label.startswith(SERVICE_PREFIX) or not scopes:
            raise ValueError("a service identity is svc:<name> with scopes")
        now = self.now_ms()
        with self.cfg._lock:
            r = self._c.execute("SELECT id FROM api_tokens WHERE kind = 'service' AND label = ? "
                                "AND owner_id IS NULL AND revoked_at IS NULL ORDER BY created_at "
                                "LIMIT 1", (label,)).fetchone()
        if r is not None:
            tid = r[0]
            token = f"{PREFIX}{tid}-{secrets.token_urlsafe(32)}"
            with self.cfg._lock:
                self._c.execute("UPDATE api_tokens SET hash = ?, last4 = ?, scopes = ?, "
                                "prev_hash = NULL, prev_until = NULL, expires_at = NULL, "
                                "needs_review = 0 WHERE id = ?",
                                (key_hash(token), token[-4:], json.dumps(list(scopes)), tid))
                self.cfg._audit(actor, "rotate", "api_token", tid, detail={"label": label})
                self._c.commit()
            return token
        tid, token = self._new_secret()
        name = memory_user or label[len(SERVICE_PREFIX):]
        with self.cfg._lock:
            self._c.execute(
                f"INSERT INTO api_tokens({', '.join(COLS)}) VALUES "
                f"({', '.join('?' * len(COLS))})",
                (tid, "service", None, label, key_hash(token), token[-4:],
                 json.dumps(list(scopes)), "[]", "{}", name, "service", None, 0, now, actor,
                 None, None, None, None, None, None, None))
            self.cfg._audit(actor, "create", "api_token", tid,
                            detail={"label": label, "kind": "service"})
            self._c.commit()
            self.ensure_guard()
        return token

    def services(self) -> list[dict]:
        return self.rows("WHERE kind = 'service'")

    # ------------------------------------------------------------------ the way back
    def ensure_guard(self) -> bool:
        """While a token exists and `users` has no enabled row, the row `rollback-guard` (a hash
        nobody holds) keeps the release before closed. True when it wrote or enabled it."""
        with self.cfg._lock:
            if not self._c.execute("SELECT 1 FROM api_tokens LIMIT 1").fetchone():
                return False
            if self._c.execute("SELECT 1 FROM users WHERE enabled = 1 LIMIT 1").fetchone():
                return False
            h = key_hash(secrets.token_urlsafe(32))         # its secret is dropped at once
            if self._c.execute("SELECT 1 FROM users WHERE label = ?", (GUARD_LABEL,)).fetchone():
                self._c.execute("UPDATE users SET enabled = 1, key_hash = ? WHERE label = ?",
                                (h, GUARD_LABEL))
            else:
                self._c.execute("INSERT INTO users(label, key_hash, key_hint, role, enabled, "
                                "created_at) VALUES (?, ?, NULL, 'user', 1, ?)",
                                (GUARD_LABEL, h, self.now_ms()))
            self.cfg._audit("controller", "create", "user", GUARD_LABEL,
                            detail={"why": "the release before stays closed"})
            self._c.commit()
        return True

    # ------------------------------------------------------------------ the request
    def resolve(self, headers):
        """The gate's token_resolver: a Principal, or Refused (401, saying why)."""
        value, bad = credential(headers)
        if bad is not None:
            return bad
        h = key_hash(value)
        now = self.now_ms()
        sql = (f"SELECT {', '.join('t.' + c for c in COLS)}, a.username, a.role, a.enabled "
               f"FROM api_tokens t LEFT JOIN accounts a ON a.id = t.owner_id WHERE ")
        with self.cfg._lock:
            r = self._c.execute(sql + "t.hash = ?", (h,)).fetchone()
            if r is None:
                r = self._c.execute(sql + "t.prev_hash = ? AND t.prev_until > ?",
                                    (h, now)).fetchone()
        if r is None:
            return Refused(401, "invalid_api_key", "the token is not known")
        row = self._row(r[:len(COLS)])
        username, role, enabled = r[len(COLS):]
        st = self.state(row, now)
        if st == "revoked":
            return Refused(401, "invalid_api_key", "the token was revoked",
                           {"state": "revoked"})
        if st == "expired":
            return Refused(401, "invalid_api_key", f"the token expired at "
                                                   f"{_iso(row['expires_at'])}", {"state": "expired"})
        if row["owner_id"] is not None and (username is None or not enabled):
            return Refused(401, "invalid_api_key", "the token's owner is disabled",
                           {"state": "owner_disabled"})
        self._note(row["id"], now)
        from proxy.accounts import Principal
        kind = "service" if row["kind"] == "service" else "token"
        return Principal(kind=kind, id=row["id"], name=row["memory_user"], role=role,
                         via="token", scopes=frozenset(row["scopes"]),
                         perms=effective(row["scopes"], role, row["kind"], self.policy,
                                         owned=row["owner_id"] is not None),
                         models=tuple(row["models"]), client="k:" + h[:12],
                         expires_at=row["expires_at"],
                         limits=tuple(sorted(row["limits"].items())))

    def _note(self, tid: str, now: int) -> None:
        """last_used_at: in memory, written at most once a minute (as config.flush_seen)."""
        self._seen[tid] = now
        mono = time.monotonic()
        if mono - self._flushed < SEEN_FLUSH_S:
            return
        self.flush()

    def flush(self) -> int:
        seen, self._seen = self._seen, {}
        self._flushed = time.monotonic()
        if not seen:
            return 0
        with self.cfg._lock:
            if self.cfg._conn is None:
                return 0
            self._c.executemany("UPDATE api_tokens SET last_used_at = ? WHERE id = ?",
                                [(t, i) for i, t in seen.items()])
            self._c.commit()
        return len(seen)

    # ------------------------------------------------------------------ findings
    def need_review(self) -> int:
        now = self.now_ms()
        with self.cfg._lock:
            return self._c.execute("SELECT COUNT(*) FROM api_tokens WHERE needs_review = 1 AND "
                                   "revoked_at IS NULL AND (expires_at IS NULL OR expires_at > "
                                   "?)", (now,)).fetchone()[0]

    def expiring(self, within_ms: int) -> int:
        """SRV-871: live tokens of a person or an import (not a service identity) that end
        within `within_ms`: the finding token_expiring."""
        now = self.now_ms()
        with self.cfg._lock:
            return self._c.execute("SELECT COUNT(*) FROM api_tokens WHERE kind != 'service' AND "
                                   "revoked_at IS NULL AND expires_at IS NOT NULL AND expires_at "
                                   "> ? AND expires_at <= ?", (now, now + within_ms)).fetchone()[0]

    def limited(self) -> list[tuple]:
        """SRV-873: (id, limits, owner's role) of every live token of a person or an import:
        the findings token_limit_near and token_limit_reached weigh their spend limits."""
        now = self.now_ms()
        with self.cfg._lock:
            got = self._c.execute("SELECT t.id, t.limits, a.role FROM api_tokens t LEFT JOIN "
                                  "accounts a ON a.id = t.owner_id WHERE t.kind != 'service' AND "
                                  "t.revoked_at IS NULL AND (t.expires_at IS NULL OR "
                                  "t.expires_at > ?)", (now,)).fetchall()
        return [(i, json.loads(lim or "{}"), role) for i, lim, role in got]

    def labels(self) -> dict:
        """SRV-873: {id: {label, owner}} of every token, for the Usage view's token dimension."""
        with self.cfg._lock:
            got = self._c.execute("SELECT t.id, t.label, t.kind, a.username FROM api_tokens t "
                                  "LEFT JOIN accounts a ON a.id = t.owner_id").fetchall()
        return {i: {"label": label, "owner": user, "kind": kind}
                for i, label, kind, user in got}

    def admin_token_live(self) -> dict | None:
        now = self.now_ms()
        for row in self.rows("WHERE source = 'admin_token' AND revoked_at IS NULL"):
            if row["expires_at"] is None or row["expires_at"] > now:
                return row
        return None

    # ------------------------------------------------------------------ the import
    def _known(self, h: str) -> bool:
        with self.cfg._lock:
            return self._c.execute("SELECT 1 FROM api_tokens WHERE hash = ? OR prev_hash = ?",
                                   (h, h)).fetchone() is not None

    def _import(self, *, kind: str, owner_id, label: str, h: str, last4, scopes, source: str,
                legacy_user_id=None, review: bool = True, expires_at=None, enabled=True,
                memory_user: str | None = None) -> bool:
        if self._known(h):
            return False
        tid, _ = self._new_secret()
        now = self.now_ms()
        with self.cfg._lock:
            taken = self._c.execute("SELECT 1 FROM api_tokens WHERE owner_id IS ? AND label = ? "
                                    "AND revoked_at IS NULL", (owner_id, label)).fetchone()
            name = f"{label} ({source})" if taken else label
            self._c.execute(
                f"INSERT INTO api_tokens({', '.join(COLS)}) VALUES "
                f"({', '.join('?' * len(COLS))})",
                (tid, kind, owner_id, name, h, last4, json.dumps(list(scopes)), "[]", "{}",
                 memory_user or label, source, legacy_user_id, int(review and enabled), now,
                 "migration", expires_at, None if enabled else now,
                 None if enabled else "migration",
                 None if enabled else "disabled before the upgrade", None, None, None))
            self.cfg._audit("migration", "create", "api_token", tid,
                            detail={"label": name, "source": source, "kind": kind})
            self._c.commit()
        return True

    def import_legacy(self, env, *, keys_env: dict | None = None, users_from: str | None = None,
                      first_owner: dict | None = None) -> dict:
        """The legacy keys as tokens with the same hash (THE IMPORT above). `keys_env`: the
        config's TANDEM_API_KEYS ({hash: label}); `users_from`: TANDEM_API_USERS_FROM's file.
        Each source runs once (auth_state `tokens_imported`); a source that could not be read is
        tried again at the next start. Returns what it did, per source."""
        done = self.store.state("tokens_imported") or {}
        did: dict = {}
        if "users" not in done:
            did["users"] = self._import_users()
            done["users"] = self.now_ms()
        if "env" not in done:
            hints = {}
            for item in (env.get("TANDEM_API_KEYS") or "").split(","):
                label, sep, key = item.strip().partition(":")
                if sep and len(key.strip()) >= 16:
                    hints[key_hash(key.strip())] = key.strip()[-4:]
            n = 0
            for h, label in sorted((keys_env or {}).items(), key=lambda kv: kv[1]):
                n += self._import(kind="legacy", owner_id=None, label=label, h=h,
                                  last4=hints.get(h), scopes=("inference:openai",), source="env")
            did["env"] = n
            done["env"] = self.now_ms()
        if users_from and "users_from" not in done:
            got = self._read_users_from(users_from)
            if got is not None:
                n = 0
                for label, h, hint, enabled in got:
                    if label == GUARD_LABEL:
                        continue
                    n += self._import(kind="legacy", owner_id=None, label=label, h=h,
                                      last4=hint, scopes=("inference:openai",),
                                      source="users_from", enabled=bool(enabled))
                did["users_from"] = n
                done["users_from"] = self.now_ms()
        # SRV-869: the metrics token and the shared collect token as service identities under
        # Review; a minted one per backend (svc:collect:<id>) replaces the shared one over time
        for src, label, scopes, value in (
                ("metrics_token", METRICS_SERVICE, METRICS_SCOPES,
                 (env.get("QSE_METRICS_TOKEN") or "").strip()),
                ("collect_token", COLLECT_SERVICE, COLLECT_SCOPES, self._collect_token(env))):
            if src in done:
                continue
            if len(value) >= 16:
                did[src] = self._import(kind="service", owner_id=None, label=label,
                                        h=key_hash(value), last4=value[-4:], scopes=scopes,
                                        source=src, memory_user=label[len(SERVICE_PREFIX):])
            done[src] = self.now_ms()
        token = (env.get("TANDEM_ADMIN_TOKEN") or env.get("QSE_ADMIN_TOKEN") or "").strip()
        if len(token) >= 32 and first_owner is not None and "admin_token" not in done:
            did["admin_token"] = self._import(
                kind="legacy", owner_id=first_owner["id"], label=ADMIN_LABEL,
                h=key_hash(token), last4=token[-4:], scopes=ADMIN_TOKEN_SCOPES,
                source="admin_token",
                expires_at=self.now_ms() + ADMIN_TOKEN_DAYS * DAY_MS)
            done["admin_token"] = self.now_ms()
        self.store.set_state("tokens_imported", done)
        if did.get("users") or did.get("env") or did.get("users_from"):
            self.store.set_state("had_legacy_keys", True)       # SRV-871: the first exposure
        self.ensure_guard()
        return did

    def _collect_token(self, env) -> str:
        """The shared collect token of the release before: TANDEM_COLLECT_TOKEN, else the one
        proxy/collect_api.py kept in the settings."""
        t = (env.get("TANDEM_COLLECT_TOKEN") or "").strip()
        if t:
            return t
        try:
            got = self.cfg.setting("collect_token")
        except Exception:                                          # noqa: BLE001
            got = None
        return got.strip() if isinstance(got, str) else ""

    def _import_users(self) -> int:
        with self.cfg._lock:
            rows = self._c.execute("SELECT id, label, key_hash, key_hint, enabled FROM users "
                                   "ORDER BY id").fetchall()
        n = 0
        for uid, label, h, hint, enabled in rows:
            if label == GUARD_LABEL:
                continue
            if label == CHATD_LABEL:
                n += self._import(kind="service", owner_id=None, label=CHATD_SERVICE, h=h,
                                  last4=hint, scopes=CHATD_SCOPES, source="users",
                                  legacy_user_id=uid, review=False, enabled=bool(enabled),
                                  memory_user=CHATD_LABEL)
                continue
            n += self._import(kind="legacy", owner_id=None, label=label, h=h, last4=hint,
                              scopes=("inference:openai",), source="users",
                              legacy_user_id=uid, enabled=bool(enabled))
        return n

    @staticmethod
    def _read_users_from(path: str):
        import sqlite3
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
            try:
                return con.execute("SELECT label, key_hash, key_hint, enabled FROM users "
                                   "ORDER BY id").fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            return None


def model_allowed(models, model) -> bool:
    """A token's globs over public model ids; none: every model its owner may use."""
    if not models:
        return True
    if not isinstance(model, str) or not model:
        return False
    return any(fnmatch.fnmatchcase(model, g) for g in models)
