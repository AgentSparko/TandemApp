"""Who is this (SEC-22): the identity provider interface and the local provider.

A provider only proves who someone is; roles, sessions and tokens are TandemApp's own tables
(proxy/accounts.py), whatever the provider.

    class IdentityProvider
        kind                                   "local" (later "ldap", "oidc", "trusted-header")
        capabilities() -> dict                 password, redirect, header, groups, manage_users,
                                               change_password
        authenticate(username, password, ctx) -> Identity | Denied | Challenge
        begin(ctx), complete(params, ctx)      a redirect sign-in (oidc, later)
        from_request(headers, peer)            a header sign-in (trusted-header, later)
        refresh(identity) -> Identity | Gone   its groups again; a removed person is Gone
        health() -> [finding]

    Identity = {provider, subject, username, display_name, email, groups}

LOCAL. The account's hash sits in its row (`accounts.pw`, the JSON of proxy/adminpw.py's
make_doc: scrypt, or PBKDF2 where hashlib has no scrypt); either verifies on any Python. Every
sign-in costs one derive: an unknown name, a disabled account and an account without a password
are checked against a hash nobody knows, so the answer and its time do not tell which names exist.
The limits of adminpw.py hold over every address: past GLOBAL_FAILS wrong passwords in
GLOBAL_WINDOW_S no check runs (Denied "held"), and at most AT_ONCE checks run at once (the rest
Denied "busy", not queued). A password is never written to a log, an answer or an audit row.
"""

from __future__ import annotations

import collections
import secrets
import threading
import time
from dataclasses import dataclass, field

from proxy import adminpw


@dataclass(frozen=True)
class Identity:
    provider: str
    subject: str
    username: str
    display_name: str | None = None
    email: str | None = None
    groups: tuple = field(default_factory=tuple)


@dataclass(frozen=True)
class Denied:
    reason: str           # "wrong" (name or password), "held" (the cap), "busy" (checks in flight)


@dataclass(frozen=True)
class Challenge:
    kind: str             # a second step (a later change: "totp")


@dataclass(frozen=True)
class Gone:
    reason: str = "removed"


class IdentityProvider:
    kind = "none"

    def capabilities(self) -> dict:
        return {"password": False, "redirect": False, "header": False, "groups": False,
                "manage_users": False, "change_password": False}

    def authenticate(self, username: str, password: str, ctx: dict | None = None):
        return Denied("wrong")

    def begin(self, ctx: dict | None = None):
        raise NotImplementedError(f"{self.kind} has no redirect sign-in")

    def complete(self, params: dict, ctx: dict | None = None):
        raise NotImplementedError(f"{self.kind} has no redirect sign-in")

    def from_request(self, headers, peer: str):
        return None

    def refresh(self, identity: Identity):
        return identity

    def health(self) -> list:
        return []


class LocalProvider(IdentityProvider):
    """Usernames and scrypt hashes in the `accounts` table (`store`: proxy/accounts.py's
    AccountStore, or anything with `by_username(name) -> row | None`)."""

    kind = "local"

    def __init__(self, store, *, clock=time.time):
        self.store = store
        self.clock = clock
        self._fails: collections.deque = collections.deque()
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(adminpw.AT_ONCE)
        # a hash of a password nobody knows, with the parameters a new one gets: the check an
        # unknown name costs
        self._nobody = adminpw.make_doc(secrets.token_urlsafe(24), 0)
        self.derives = 0                  # how many hashes ran (the tests count them)

    def capabilities(self) -> dict:
        return {"password": True, "redirect": False, "header": False, "groups": False,
                "manage_users": True, "change_password": True}

    def _held_locked(self, now: float) -> bool:
        while self._fails and now - self._fails[0] > adminpw.GLOBAL_WINDOW_S:
            self._fails.popleft()
        return len(self._fails) >= adminpw.GLOBAL_FAILS

    def held(self) -> bool:
        with self._lock:
            return self._held_locked(self.clock())

    @staticmethod
    def hash_doc(password: str, now: float) -> dict:
        return adminpw.make_doc(password, now)

    def authenticate(self, username, password, ctx: dict | None = None):
        if not isinstance(password, str) or len(password) > adminpw.MAX_LEN:
            password = ""
        row = self.store.by_username(username) if isinstance(username, str) else None
        doc = row.get("pw") if row is not None else None
        usable = row is not None and bool(row.get("enabled")) and isinstance(doc, dict)
        with self._lock:
            if self._held_locked(self.clock()):
                return Denied("held")
        if not self._slots.acquire(blocking=False):
            return Denied("busy")
        try:
            self.derives += 1
            good = adminpw.verify_doc(password, doc if usable else self._nobody) and usable
        finally:
            self._slots.release()
        if not good:
            with self._lock:
                self._fails.append(self.clock())
            return Denied("wrong")
        return Identity(provider=self.kind, subject=row["subject"], username=row["username"],
                        display_name=row.get("display_name"))
