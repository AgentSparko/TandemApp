"""Who may do what with accounts on (SEC-22): four roles, one permission table, one route table.

Used only while TANDEM_AUTH=accounts (proxy/accounts.py); with it off tandemapp/auth.py decides as
before and nothing here runs.

    allow(principal, permission, policy) -> bool
    permission_for(method, path) -> str | None     None: the route has no entry and is refused
    KIND_PERMISSION                                tandemapp/auth.py's route kinds -> a permission
    role_permissions(role, policy) -> frozenset

ROLES, lowest first. Each role holds the permissions of the roles below it.

    viewer     read, metrics:read, fleet:read, inference, self
               + chat while `auth.chat_min_role` is viewer
               + tokens:inference while `api.tokens.viewers_may_create` is on (default off)
    operator   + settings:write, fleet:write, chat, tokens:inference, tokens:management
    admin      + trust, users
    owner      + owner

`self` is a signed-in person's own sessions and password. `users` is every account but the
owners, every session and (later) every token. `owner` is the owners, the identity provider, the
exposure switches and the `auth` and `api` settings.

Service permissions (collect:write, chat-engine, plugin-backend) belong to no role: only a service
identity holds them (SRV-869, proxy/tokens.py): chatd's svc:chatd, a backend's
svc:collect:<id>, and the plugin relay's in-process secret (proxy/accounts.py relay_principal).
Being on the same machine opens none of them; outside local mode it opens nothing at all.

SRV-867: an API token acts with its `perms` (proxy/tokens.py `effective`: what its scopes imply,
cut to its owner's role at each request); which routes take which scope is tokens.scope_for. The
token routes (/v1/auth/tokens) need `self` and a signed-in account; their handler checks
`tokens:inference`, `tokens:management` and `users`.

THE ROUTE TABLE (ROUTES) names every route family the server dispatches: an exact path or a
prefix (the path itself, or anything under it), the methods, and the permission the caller needs
at least. OPEN routes need no credential (the page shell, /health's status, the sign-in, the
first-run setup, which checks its own setup code). A family
whose handler checks a finer route kind (tandemapp/auth.py's `decide`: the fleet, the settings,
the configuration, the conductor) is listed with `read` here; the kind then asks for its own
permission through KIND_PERMISSION. A path with no entry is refused with 403 and logged once;
tests/test_auth_accounts.py walks the dispatch code and fails on a route without one.
"""

from __future__ import annotations

from proxy.service import PROXY_PATHS

ROLES = ("viewer", "operator", "admin", "owner")
OPEN = "open"
SESSION_ADDRESS = "session-address"

VIEWER = frozenset({"read", "metrics:read", "fleet:read", "inference", "self"})
OPERATOR = VIEWER | {"settings:write", "fleet:write", "chat", "tokens:inference",
                     "tokens:management"}
ADMIN = OPERATOR | {"trust", "users"}
OWNER = ADMIN | {"owner"}
BY_ROLE = {"viewer": VIEWER, "operator": OPERATOR, "admin": ADMIN, "owner": OWNER}
SERVICE = frozenset({"collect:write", "chat-engine", "plugin-backend"})
PERMISSIONS = OWNER | SERVICE | {SESSION_ADDRESS}

# tandemapp/auth.py's route kinds, one to one
KIND_PERMISSION = {
    "dashboard": "read", "health_full": "read",
    "metrics": "metrics:read",
    "cache_read": "fleet:read",
    "conductor_write": "settings:write", "decider": "settings:write",
    "setup": "fleet:write", "cache_clear": "fleet:write",
    "fleet_trust": "trust",
}

GATEWAY = "/v1/fleet/gateway/chat/completions"
CACHE_PATHS = ("/v1/cache/peek", "/v1/cache/prewarm", "/v1/cache/release")
ANY = ("GET", "POST", "PUT", "PATCH", "DELETE")
GET = ("GET",)
POST = ("POST",)

# (methods, "exact" | "prefix", path, permission); the first entry that matches wins
ROUTES: tuple = (
    # open: the page shell, the status, the sign-in and the old sign-in routes (each answers
    # without a credential and checks its own writes)
    (GET, "exact", "/", OPEN),
    (GET, "prefix", "/dashboard", OPEN),
    (GET, "exact", "/health", OPEN), (GET, "exact", "/healthz", OPEN),
    (GET, "exact", "/v1/health", OPEN),
    (("GET", "POST", "DELETE"), "exact", "/v1/auth/session", OPEN),
    (POST, "exact", "/v1/auth/setup", OPEN),
    (("GET", "POST", "DELETE"), "exact", "/v1/dashboard/session", OPEN),
    (("GET", "POST", "DELETE"), "exact", "/v1/fleet/session", OPEN),
    (("GET", "POST", "DELETE"), "exact", "/v1/chat-app/session", OPEN),
    # accounts and sessions
    (("GET", "DELETE"), "prefix", "/v1/auth/sessions", "self"),
    (("PUT",), "exact", "/v1/auth/password", "self"),
    (("GET", "POST", "PATCH", "DELETE"), "prefix", "/v1/auth/tokens", "self"),
    (("GET", "POST", "PATCH", "DELETE"), "prefix", "/v1/auth/users", "users"),
    # the session address: its token is the credential, for the library and Laya only
    (("GET", "POST"), "prefix", "/s", SESSION_ADDRESS),
    # services
    (POST, "prefix", "/v1/collect", "collect:write"),
    (ANY, "prefix", "/plugin-backends", "plugin-backend"),
    (GET, "exact", "/chat-engine/health", OPEN), (GET, "exact", "/chat-engine/healthz", OPEN),
    (GET, "exact", "/chat-engine/v1/health", OPEN),
    (GET, "exact", "/chat-engine/v1/dashboard/system", "read"),
    (GET, "exact", "/chat-engine/v1/dashboard/live", "read"),
    (ANY, "prefix", "/chat-engine", "chat-engine"),
    # the Chat tab
    (GET, "exact", "/v1/chat-app/capabilities", "read"),
    (ANY, "prefix", "/v1/chat-app", "chat"),
    # the page's reads
    (GET, "prefix", "/plugin-panels", "read"),
    (GET, "prefix", "/v1/dashboard", "read"),
    (GET, "exact", "/metrics", "metrics:read"),
    # inference, through a session now and a token later
    (POST, "exact", GATEWAY, "inference"),
    (GET, "prefix", "/v1/models", "inference"),
    *((POST, "exact", p, "inference") for p in PROXY_PATHS),
    *((POST, "exact", p, "inference") for p in CACHE_PATHS),
    # the Messages API (SRV-879): a message, count_tokens; its handler answers any other path
    # under it with a 404 in its own shape (proxy/messages.py)
    (POST, "prefix", "/v1/messages", "inference"),
    (GET, "prefix", "/v1/library", "inference"),
    (("GET", "POST"), "prefix", "/v1/laya", "inference"),
    # families whose handlers check a route kind of their own (KIND_PERMISSION)
    (ANY, "prefix", "/v1/fleet", "read"),
    (ANY, "prefix", "/v1/settings", "read"),
    (ANY, "prefix", "/v1/config", "read"),
    (ANY, "prefix", "/v1/conductor", "read"),
)


def _norm(path: str) -> str:
    return path.split("?", 1)[0].rstrip("/") or "/"


def permission_for(method: str, path: str) -> str | None:
    """The permission a request needs at least, OPEN, SESSION_ADDRESS, or None (no entry)."""
    p = _norm(path)
    for methods, how, route, perm in ROUTES:
        if method not in methods:
            continue
        if p == route or (how == "prefix" and (route == "/" or p.startswith(route + "/"))):
            return perm
    return None


class Policy:
    """The settings the table reads (plain `settings` keys of the config database until the
    `auth` and `api` settings scopes exist): who may chat, whether viewers make inference tokens,
    the session timeouts and the password length."""

    MAX_DAYS = 400

    def __init__(self, cfg=None, values: dict | None = None):
        self.cfg = cfg
        self.values = dict(values or {})

    def get(self, key: str, default=None):
        if key in self.values:
            return self.values[key]
        if self.cfg is None:
            return default
        try:
            return self.cfg.setting(key, default)
        except Exception:                                          # noqa: BLE001
            return default

    @property
    def chat_min_role(self) -> str:
        return "viewer" if self.get("auth.chat_min_role") == "viewer" else "operator"

    @property
    def viewers_may_create(self) -> bool:
        v = self.get("api.tokens.viewers_may_create", False)
        return v is True or v == "on"

    def _days(self, key: str, default: int) -> int:
        v = self.get(key, default)
        try:
            v = int(v)
        except (TypeError, ValueError):
            return default
        return min(max(v, 1), self.MAX_DAYS)

    @property
    def idle_days(self) -> int:
        return self._days("auth.session.idle_days", 30)

    @property
    def absolute_days(self) -> int:
        return self._days("auth.session.absolute_days", 180)

    @property
    def password_min_length(self) -> int:
        from proxy import adminpw
        v = self.get("auth.password.min_length", adminpw.MIN_LEN)
        try:
            v = int(v)
        except (TypeError, ValueError):
            v = adminpw.MIN_LEN
        return min(max(v, adminpw.MIN_LEN), adminpw.MAX_LEN)


def role_permissions(role: str | None, policy: Policy | None = None) -> frozenset:
    got = BY_ROLE.get(role or "")
    if got is None:
        return frozenset()
    policy = policy or Policy()
    if role == "viewer":
        extra = set()
        if policy.chat_min_role == "viewer":
            extra.add("chat")
        if policy.viewers_may_create:
            extra.add("tokens:inference")
        got = got | extra
    return got


def rank(role: str | None) -> int:
    return ROLES.index(role) if role in ROLES else -1


def allow(principal, permission: str | None, policy: Policy | None = None) -> bool:
    """May `principal` (proxy/accounts.py Principal, or None) use `permission`?"""
    if permission == OPEN:
        return True
    if principal is None or permission is None:
        return False
    if principal.kind in ("account", "local"):
        # SEC-26: local mode's request acts as the owner `local` (proxy/authlocal.py)
        return permission in role_permissions(principal.role, policy)
    if principal.kind == SESSION_ADDRESS:
        return permission == SESSION_ADDRESS
    # a token or a service identity: its scopes cut to its owner's role (proxy/tokens.py)
    perms = getattr(principal, "perms", None)
    return permission in (perms if perms is not None else (principal.scopes or ()))
