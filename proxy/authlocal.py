"""Local mode and the first-run setup code (SEC-26).

LOCAL MODE. A bare process bound to loopback only, with no owner that has a password and
`auth.local_mode` auto: a request that passes trusted-local (a loopback peer, no forwarding
header, a loopback Host) acts as the owner `local`, with no prompt and no token. Every other
request needs a session or a token. The address the process is bound to decides, never the peer:
a reverse proxy or a tunnel on the same host makes every client a loopback peer.

    on when    bound to 127.0.0.0/8, ::1 or localhost (proxy/app.py serve() says where), and
               a bare process: TANDEM_DEPLOY unset or `bare`, no KUBERNETES_SERVICE_HOST, no
               /.dockerenv or /run/.containerenv, and
               no enabled local owner with a password, and
               `auth.local_mode` (the setting) and TANDEM_LOCAL_MODE are not `off`, and
               no legacy admin secret (TANDEM_ADMIN_TOKEN, QSE_ADMIN_TOKEN): an install that was
               guarded is never opened
    off        anything else. Setting the owner's password ends it at once, with no restart.

The installer writes TANDEM_LOCAL_MODE=off for --listen beyond loopback and for --behind-proxy.
While local mode is on, a loopback request with a forwarding header (X-Forwarded-For, X-Real-IP,
Forwarded, X-Forwarded-Host) or a Host that is not loopback gets 401 and raises the finding
`local_mode_proxy_seen`. Every local-mode write passes the same-origin rule (proxy/accounts.py).

THE SETUP CODE. Without local mode and with no owner, the first owner is made with a one-time
code at POST /v1/auth/setup {code, username, password}; the route answers 404 once an owner
exists. A code is 20 characters (no 0, 1, I, L, O, U; case, spaces and dashes do not count), is
stored as its SHA-256 only, and opens nothing else. Each start without an owner logs a fresh
one (the one of the start before stops); `tandem-app users setup-code` prints another. Both live
24 hours. On Kubernetes TANDEM_SETUP_CODE_FILE names a mounted Secret whose content is a code
until the first owner exists; with an owner, the finding `bootstrap_secret_left` asks to delete
it. A wrong code counts as a try for every live code: after 5 a code is spent for good.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import secrets
import time

from tandemapp import auth as auth_mod

ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
CODE_LEN = 20
TRIES = 5
CODE_TTL_MS = 86_400_000
FORWARDED = ("X-Forwarded-For", "X-Real-IP", "Forwarded", "X-Forwarded-Host")
DEPLOY_ENV = "TANDEM_DEPLOY"
LOCAL_ENV = "TANDEM_LOCAL_MODE"
SECRET_ENV = "TANDEM_SETUP_CODE_FILE"
SOURCES = ("log", "cli", "secret", "admin_token")


def new_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))


def normal(code) -> str:
    return "".join(str(code).split()).replace("-", "").upper() if isinstance(code, str) else ""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def trusted_local(peer: str, headers) -> bool:
    """The one test of "a caller on this machine" (SRV-869, SEC-28): a loopback peer, a loopback
    Host, no forwarding header. Two callers, each named in tests/test_auth_services.py: local
    mode (LocalMode below, the only place it lets a request in) and the exposure rule
    (proxy/exposure.py, which only narrows a token that is valid already)."""
    return peer in auth_mod.LOOPBACK and not LocalMode.proxied(headers)


def loopback_bind(host: str | None) -> bool:
    h = (host or "").strip().strip("[]").lower()
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def deploy_kind(env, exists=os.path.exists) -> str:
    """`bare`, `container` or `kubernetes`: where this process runs."""
    got = (env.get(DEPLOY_ENV) or "").strip().lower()
    if got and got != "bare":
        return "kubernetes" if got in ("kubernetes", "k8s", "helm") else "container"
    if env.get("KUBERNETES_SERVICE_HOST"):
        return "kubernetes"
    if exists("/.dockerenv") or exists("/run/.containerenv"):
        return "container"
    return "bare"


# ======================================================================================== codes
class SetupCodes:
    """The live setup codes, in the config database (table setup_codes, hashes only), so a code
    the CLI prints works in the running server."""

    def __init__(self, store, env=None, *, exists=os.path.exists):
        self.store = store
        self.env = env if env is not None else {}
        self.exists = exists

    @property
    def _c(self):
        return self.store.cfg._conn

    @property
    def _lock(self):
        return self.store.cfg._lock

    def secret_path(self) -> str | None:
        return (self.env.get(SECRET_ENV) or "").strip() or None

    def secret_text(self) -> str | None:
        path = self.secret_path()
        if not path:
            return None
        try:
            with open(path) as f:
                text = f.read(4096).strip()
        except OSError:
            return None
        return text or None

    def _add(self, digest: str, source: str, ttl_ms: int | None) -> None:
        now = self.store.now_ms()
        self._c.execute("INSERT OR IGNORE INTO setup_codes(hash, source, created_at, expires_at, "
                        "tries) VALUES (?, ?, ?, ?, 0)",
                        (digest, source, now, now + ttl_ms if ttl_ms else None))

    def new(self, source: str) -> str:
        """A fresh code (returned once; only its hash is kept). A start's code ends the one of the
        start before."""
        code = new_code()
        now = self.store.now_ms()
        with self._lock:
            if source == "log":
                self._c.execute("UPDATE setup_codes SET spent_at = ? WHERE source = 'log' AND "
                                "spent_at IS NULL", (now,))
            self._add(_sha(code), source, CODE_TTL_MS)
            self.store.cfg._audit("setup", "create", "setup_code", None, detail={"source": source})
            self._c.commit()
        return code

    def add_secret(self, text: str, source: str) -> None:
        """A code someone else holds (the Secret's file, the old admin token), until it is spent;
        a spent one stays spent."""
        with self._lock:
            self._add(_sha(text.strip()), source, None)
            self._c.commit()

    def sync_secret(self) -> None:
        text = self.secret_text()
        if text:
            self.add_secret(text, "secret")

    def live(self) -> list[dict]:
        now = self.store.now_ms()
        with self._lock:
            rows = self._c.execute("SELECT hash, source, tries, expires_at FROM setup_codes WHERE "
                                   "spent_at IS NULL AND tries < ? AND (expires_at IS NULL OR "
                                   "expires_at > ?)", (TRIES, now)).fetchall()
        return [{"hash": r[0], "source": r[1], "tries": r[2], "expires_at": r[3]} for r in rows]

    def use(self, code) -> tuple[str, int]:
        """("ok", 0) and the code spent; ("wrong", tries left); ("none", 0): no code is live."""
        self.sync_secret()
        text = code if isinstance(code, str) and len(code) <= 256 else ""
        want = {_sha(normal(text)), _sha(text.strip())}
        now = self.store.now_ms()
        with self._lock:
            live = self.live()
            if not live:
                return "none", 0
            hit = next((r for r in live if r["hash"] in want), None) if text.strip() else None
            if hit is not None:
                self._c.execute("UPDATE setup_codes SET spent_at = ? WHERE hash = ?",
                                (now, hit["hash"]))
                self.store.cfg._audit("setup", "use", "setup_code", None,
                                      detail={"source": hit["source"]})
                self._c.commit()
                return "ok", 0
            self._c.execute("UPDATE setup_codes SET tries = tries + 1 WHERE spent_at IS NULL AND "
                            "tries < ? AND (expires_at IS NULL OR expires_at > ?)", (TRIES, now))
            self._c.execute("UPDATE setup_codes SET spent_at = ? WHERE spent_at IS NULL AND "
                            "tries >= ?", (now, TRIES))
            self._c.commit()
            left = self.live()
        return "wrong", max((TRIES - r["tries"] for r in left), default=0)

    def spend_all(self) -> None:
        with self._lock:
            self._c.execute("UPDATE setup_codes SET spent_at = ? WHERE spent_at IS NULL",
                            (self.store.now_ms(),))
            self._c.commit()


# ======================================================================================== local mode
class LocalMode:
    """AccountsAuth.local_mode: (peer, headers) -> the owner `local`, or None."""

    def __init__(self, store, env=None, *, clock=time.time, exists=os.path.exists, log=None):
        self.store = store
        self.env = env if env is not None else {}
        self.clock = clock
        self.exists = exists
        self.log = log or (lambda m: None)
        self.bind: str | None = None             # where the process listens (serve() sets it)
        self.seen: float | None = None           # the last proxied request while on
        from proxy import authlegacy
        self.legacy_guard = authlegacy.admin_token_set(self.env)

    def why_not(self) -> str | None:
        """Why local mode cannot be on here, whatever the accounts; None: it can."""
        if self.bind is None:
            return "not listening"
        if not loopback_bind(self.bind):
            return "bound beyond loopback"
        kind = deploy_kind(self.env, self.exists)
        if kind != "bare":
            return f"runs in a {kind}" if kind == "container" else "runs on Kubernetes"
        if (self.env.get(LOCAL_ENV) or "").strip().lower() == "off" or \
                str(self.store.policy.get("auth.local_mode", "auto")).lower() == "off":
            return "auth.local_mode is off"
        if self.legacy_guard:
            return "an admin token is set"
        return None

    def on(self) -> bool:
        return self.why_not() is None and self.store.setup_required()

    @staticmethod
    def proxied(headers) -> bool:
        try:
            if any((headers.get(h) or "").strip() for h in FORWARDED):
                return True
            return not auth_mod.loopback_host(headers.get("Host") or "")
        except Exception:                                          # noqa: BLE001
            return True

    def __call__(self, peer: str, headers):
        if peer not in auth_mod.LOOPBACK or not self.on():
            return None
        if not trusted_local(peer, headers):
            if self.seen is None:
                self.log("[auth] a request came through a proxy while TandemApp trusts this "
                         "machine: it was refused; set a password")
            self.seen = self.clock()
            return None
        from proxy import accounts as acc_mod
        return acc_mod.Principal(kind="local", id="local", name="local", role="owner",
                                 via="local")


def needs_password(principal) -> bool:
    """SEC-26: a change that lets other devices in (a second user, the exposure, the listen
    address, a provider) first needs the owner's password: never from local mode."""
    return principal is not None and principal.kind == "local"

