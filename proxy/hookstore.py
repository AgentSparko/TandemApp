"""The hook registry (SRV-849, SEC-16): two tables of their own in the config database.

    hooks          one row per hook: id, name, kind (http, builtin, plugin; mcp and inproc are
                   refused until they exist), stages (JSON list), role (transform, guard, decider,
                   observer), priority, mode (off, shadow, on), scope (JSON), failure policy,
                   timeout, payload level (meta, redacted, full), endpoint (http) or target
                   (builtin: its name; plugin: `<plugin>/<hook>`, a hook its manifest declares,
                   at its stages, role and payload or below), options (JSON), the secret's ref,
                   the full payload's grant (JSON),
                   enabled, created and updated (when, by whom), version
    hook_secrets   a hook's HMAC secret as AES-256-GCM ciphertext under the master key
                   (proxy/secrets.py) with `hook-secret:<ref>` as associated data, its last 4
                   characters and a sha256 prefix for the page; during a rotation's grace the
                   previous secret too, with its end

Every write is audited (field names, never a secret) and bumps `gen`, which the runner reads.

THE FULL PAYLOAD (SEC-16). `payload: full` is granted per hook: the grant records who and when,
the destination's host and whether it is on the LAN (proxy/hookcall.py `host_is_lan`). A
destination outside the LAN needs a second confirm: the write answers 409 `confirm_needed` with a
`confirm` token bound to the hook, the host and the hook's version, and applies only when the same
write comes back with it; the grant then records who confirmed and when. Changing a full hook's
endpoint asks again for a destination outside the LAN. Leaving `full` drops the grant.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets as _rand
import time

from proxy import hookcall
from proxy import hookstages as S
from proxy import secrets as vault_mod

DDL = """
CREATE TABLE IF NOT EXISTS hooks (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, stages TEXT NOT NULL,
  role TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 100, mode TEXT NOT NULL DEFAULT 'on',
  scope TEXT NOT NULL DEFAULT '{}', policy TEXT, timeout_ms INTEGER,
  payload TEXT NOT NULL DEFAULT 'meta', endpoint TEXT, target TEXT,
  options TEXT NOT NULL DEFAULT '{}', secret_ref TEXT, grant_doc TEXT,
  enabled INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
  updated_by TEXT, version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS hook_secrets (
  ref TEXT PRIMARY KEY, hook_id TEXT NOT NULL, ciphertext BLOB NOT NULL, nonce BLOB NOT NULL,
  master_key_id TEXT NOT NULL, last4 TEXT, sha256_6 TEXT, created_at INTEGER NOT NULL,
  expires_at INTEGER);
"""
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,47}$")
#: ids the hooks' API answers routes of its own at (/v1/fleet/hooks/stages, /calls)
RESERVED = ("stages", "calls")
SCOPE_KEYS = ("models", "backends", "profiles", "users", "sessions", "client_kinds")
FIELDS = ("name", "kind", "stages", "role", "priority", "mode", "scope", "policy", "timeout_ms",
          "payload", "endpoint", "target", "options", "enabled")
PRIORITY_MIN, PRIORITY_MAX = -100000, 100000
OPTIONS_MAX = 16 * 1024
GRACE_MAX_S = 86400


class HookError(Exception):
    def __init__(self, status: int, code: str, message: str, field: str | None = None,
                 extra: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.field = status, code, message, field
        self.extra = dict(extra or {})

    def body(self) -> dict:
        err = {"type": self.code, "code": self.code, "message": self.message}
        if self.field:
            err["field"] = self.field
        err.update(self.extra)
        return {"error": err}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _bad(message: str, field: str | None = None) -> HookError:
    return HookError(422, "invalid", message, field)


class HookStore:
    """The tables over a `proxy.config.ConfigDB` (its connection and lock)."""

    def __init__(self, cfg, builtins=()):
        self.cfg = cfg
        self.builtins = builtins            # a container of builtin names (the runner's registry)
        self.gen = 0
        self._ready = False
        self._secret = _rand.token_bytes(32)          # the confirm tokens' key, this process's
        # SRV-859: fn(target) -> {stages, role, payload} of a plugin's declared hook, or None
        self.plugin_target = None

    # ------------------------------------------------------------------ lifecycle
    def ready(self) -> bool:
        """The tables exist (made on first use). False when the config database is not open."""
        if self._ready:
            return True
        conn = getattr(self.cfg, "_conn", None)
        if conn is None:
            return False
        # the config database's own lock, and only it: a second lock taken in the other order by
        # a reader under the config lock deadlocked the first requests
        with self.cfg._lock:
            if not self._ready:
                conn.executescript(DDL)
                conn.commit()
                self._ready = True
        return True

    def _c(self):
        if not self.ready():
            raise HookError(503, "unavailable", "the config database is not open")
        return self.cfg._conn

    def data_version(self) -> int | None:
        """SQLite's data_version: it moves when another connection (another process) commits."""
        try:
            with self.cfg._lock:
                return int(self._c().execute("PRAGMA data_version").fetchone()[0])
        except Exception:                                          # noqa: BLE001
            return None

    # ------------------------------------------------------------------ reads
    _COLS = ("id", "name", "kind", "stages", "role", "priority", "mode", "scope", "policy",
             "timeout_ms", "payload", "endpoint", "target", "options", "secret_ref", "grant_doc",
             "enabled", "created_at", "updated_at", "updated_by", "version")

    def _row(self, r) -> dict:
        d = dict(zip(self._COLS, r))
        for k, dflt in (("stages", []), ("scope", {}), ("options", {})):
            try:
                d[k] = json.loads(d[k]) if d[k] else dflt
            except ValueError:
                d[k] = dflt
        try:
            d["grant"] = json.loads(d.pop("grant_doc")) if d.get("grant_doc") else None
        except ValueError:
            d["grant"] = None
        d["enabled"] = bool(d["enabled"])
        return d

    def list(self) -> list[dict]:
        with self.cfg._lock:
            rows = self._c().execute(f"SELECT {', '.join(self._COLS)} FROM hooks "
                                     f"ORDER BY priority, id").fetchall()
        return [self._row(r) for r in rows]

    def get(self, hid: str) -> dict | None:
        with self.cfg._lock:
            r = self._c().execute(f"SELECT {', '.join(self._COLS)} FROM hooks WHERE id = ?",
                                  (hid,)).fetchone()
        return self._row(r) if r else None

    def need(self, hid: str) -> dict:
        h = self.get(hid)
        if h is None:
            raise HookError(404, "not_found", f"no hook {hid!r}")
        return h

    # ------------------------------------------------------------------ validation
    def _check(self, d: dict) -> None:
        """Raise HookError for a hook document that may not be stored (`d` complete)."""
        if not isinstance(d.get("name"), str) or not 1 <= len(d["name"].strip()) <= 80:
            raise _bad("name: 1 to 80 characters", "name")
        if d["kind"] not in S.KINDS:
            raise _bad(f"kind: one of {', '.join(S.KINDS)}", "kind")
        if d["kind"] not in S.KINDS_NOW:
            later = {"mcp": "a later version (a plugin's hooks are kind plugin)",
                     "inproc": "the in-process chain"}[d["kind"]]
            raise HookError(422, "kind_unavailable", f"kind {d['kind']!r} is not available in "
                            f"this version: it arrives with {later}. Use http, builtin or "
                            f"plugin.", "kind")
        if d["role"] not in S.ROLES:
            raise _bad(f"role: one of {', '.join(S.ROLES)}", "role")
        st = d["stages"]
        if not isinstance(st, list) or not st or len(set(st)) != len(st) or \
                not all(isinstance(x, str) for x in st):
            raise _bad("stages: a list of distinct stage names", "stages")
        for x in st:
            if x not in S.STAGES:
                raise _bad(f"stages: no stage {x!r}", "stages")
            if d["role"] not in S.STAGES[x]["roles"]:
                raise _bad(f"stages: a {d['role']} cannot run at {x} (it takes "
                           f"{', '.join(S.STAGES[x]['roles'])})", "stages")
        p = d["priority"]
        if isinstance(p, bool) or not isinstance(p, int) or not PRIORITY_MIN <= p <= PRIORITY_MAX:
            raise _bad(f"priority: an integer {PRIORITY_MIN}..{PRIORITY_MAX}", "priority")
        if d["mode"] not in S.MODES:
            raise _bad(f"mode: one of {', '.join(S.MODES)}", "mode")
        if d["policy"] is not None and d["policy"] not in S.POLICIES:
            raise _bad("policy: open, closed or null (the stage's default)", "policy")
        t = d["timeout_ms"]
        if t is not None and (isinstance(t, bool) or not isinstance(t, int)
                              or not 1 <= t <= S.MAX_TIMEOUT_MS):
            raise _bad(f"timeout_ms: 1..{S.MAX_TIMEOUT_MS} or null (the stage's budget)",
                       "timeout_ms")
        if d["payload"] not in S.PAYLOADS:
            raise _bad(f"payload: one of {', '.join(S.PAYLOADS)}", "payload")
        sc = d["scope"]
        if not isinstance(sc, dict) or set(sc) - set(SCOPE_KEYS):
            raise _bad(f"scope: an object of {', '.join(SCOPE_KEYS)}", "scope")
        for k, v in sc.items():
            if not isinstance(v, list) or len(v) > 64 or not all(
                    isinstance(x, str) and 1 <= len(x) <= 128 for x in v):
                raise _bad(f"scope.{k}: a list of at most 64 patterns", f"scope.{k}")
        if not isinstance(d["options"], dict) or \
                len(json.dumps(d["options"])) > OPTIONS_MAX:
            raise _bad("options: an object of at most 16 KiB", "options")
        if not isinstance(d["enabled"], bool):
            raise _bad("enabled: true or false", "enabled")
        if d["kind"] == "http":
            why = hookcall.check_url(d["endpoint"])
            if why:
                raise _bad(f"endpoint: {why}", "endpoint")
            if d["target"] is not None:
                raise _bad("target: only for a builtin or plugin hook", "target")
        elif d["kind"] == "plugin":
            if d["endpoint"] is not None:
                raise _bad("endpoint: only for an http hook", "endpoint")
            t = d["target"]
            if not isinstance(t, str) or not re.match(
                    r"^[a-z0-9][a-z0-9-]{0,47}/[a-z0-9][a-z0-9_-]{0,47}$", t):
                raise _bad("target: <plugin>/<hook>, a hook a plugin declares", "target")
            want = self.plugin_target(t) if self.plugin_target is not None else None
            if want is None:
                raise _bad(f"target: no installed plugin declares the hook {t}", "target")
            extra = sorted(set(d["stages"]) - set(want["stages"]))
            if extra:
                raise _bad(f"stages: {t} runs at {', '.join(want['stages'])}, not {extra[0]}",
                           "stages")
            if d["role"] != want["role"]:
                raise _bad(f"role: {t} is a {want['role']}", "role")
            if S.PAYLOADS.index(d["payload"]) > S.PAYLOADS.index(want["payload"]):
                raise _bad(f"payload: {t} declares {want['payload']}; at most that",
                           "payload")
        else:
            if d["endpoint"] is not None:
                raise _bad("endpoint: only for an http hook", "endpoint")
            if d["target"] not in self.builtins:
                raise _bad(f"target: one of the builtins ({', '.join(sorted(self.builtins))})",
                           "target")

    # ------------------------------------------------------------------ the full payload
    def destination(self, d: dict) -> dict:
        """{host, lan} of where a hook's payload goes: an in-process builtin and a plugin (on this
        host, its own capabilities decide what it may reach) are on this host."""
        if d["kind"] != "http":
            return {"host": None, "lan": True}
        host = hookcall.destination(d["endpoint"])["host"]
        return {"host": host, "lan": hookcall.host_is_lan(host)}

    def confirm_token(self, hid: str, host: str | None, version: int) -> str:
        msg = f"{hid}\0{host}\0{version}".encode()
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()[:32]

    def _grant(self, old: dict | None, new: dict, who: str, confirm) -> tuple[dict | None, bool]:
        """(the grant the new document carries, whether a grant or a confirm happened now)."""
        if new["payload"] != "full":
            return None, False
        dest = self.destination(new)
        g = (old or {}).get("grant") if old else None
        same = (old is not None and old["payload"] == "full" and g is not None
                and g.get("destination") == dest["host"])
        if same:
            return g, False
        if not dest["lan"]:
            want = self.confirm_token(new["id"], dest["host"], (old or {}).get("version", 0))
            if not (isinstance(confirm, str) and hmac.compare_digest(confirm, want)):
                raise HookError(409, "confirm_needed", (
                    f"the full payload (prompts and answers as they are) would go to "
                    f"{dest['host']}, outside the LAN: send the same request again with this "
                    f"confirm to grant it"), "payload",
                    extra={"confirm": want, "destination": dest["host"], "lan": False})
        now = _now_ms()
        return {"level": "full", "by": who, "at": now, "destination": dest["host"],
                "lan": dest["lan"], "confirmed_by": None if dest["lan"] else who,
                "confirmed_at": None if dest["lan"] else now}, True

    # ------------------------------------------------------------------ writes
    def _free_id(self, name: str) -> str:
        base = re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-._")[:40] or "hook"
        if not ID_RE.match(base):
            base = "hook"
        hid, n = base, 2
        while self.get(hid) is not None or hid in RESERVED:
            hid = f"{base}-{n}"
            n += 1
        return hid

    def create(self, doc: dict, who: str = "gui", confirm=None) -> tuple[dict, str | None]:
        """(the hook, its secret once for an http hook)."""
        if not isinstance(doc, dict):
            raise _bad("the body must be a JSON object")
        unknown = set(doc) - set(FIELDS) - {"id", "confirm"}
        if unknown:
            raise _bad(f"unknown field {sorted(unknown)[0]!r}", sorted(unknown)[0])
        d = {"name": doc.get("name"), "kind": doc.get("kind", "http"),
             "stages": doc.get("stages"), "role": doc.get("role"),
             "priority": doc.get("priority", 100), "mode": doc.get("mode", "on"),
             "scope": doc.get("scope") or {}, "policy": doc.get("policy"),
             "timeout_ms": doc.get("timeout_ms"), "payload": doc.get("payload", "meta"),
             "endpoint": doc.get("endpoint"), "target": doc.get("target"),
             "options": doc.get("options") or {}, "enabled": doc.get("enabled", True)}
        self._check(d)
        hid = doc.get("id")
        if hid is None:
            hid = self._free_id(d["name"])
        elif not isinstance(hid, str) or not ID_RE.match(hid):
            raise _bad("id: [a-z0-9][a-z0-9._-]{0,47}", "id")
        elif hid in RESERVED:
            raise _bad(f"id: {hid!r} is a route of the hooks' API; choose another", "id")
        elif self.get(hid) is not None:
            raise HookError(409, "exists", f"a hook {hid!r} exists", "id")
        d["id"] = hid
        grant, _ = self._grant(None, d, who, confirm if confirm is not None
                               else doc.get("confirm"))
        secret = hookcall.new_secret() if d["kind"] == "http" else None
        now = _now_ms()
        c = self._c()
        with self.cfg._lock:
            if self.get(hid) is not None:
                raise HookError(409, "exists", f"a hook {hid!r} exists", "id")
            ref = None
            if secret is not None:
                ref = self._put_secret(hid, secret, now)
            c.execute("INSERT INTO hooks(id, name, kind, stages, role, priority, mode, scope, "
                      "policy, timeout_ms, payload, endpoint, target, options, secret_ref, "
                      "grant_doc, enabled, created_at, updated_at, updated_by, version) VALUES "
                      "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                      (hid, d["name"].strip(), d["kind"], json.dumps(d["stages"]), d["role"],
                       d["priority"], d["mode"], json.dumps(d["scope"], sort_keys=True),
                       d["policy"], d["timeout_ms"], d["payload"], d["endpoint"], d["target"],
                       json.dumps(d["options"], sort_keys=True), ref,
                       json.dumps(grant, sort_keys=True) if grant else None,
                       int(d["enabled"]), now, now, who))
            self.cfg._audit(who, "create", "hook", hid, [k for k in FIELDS if k in doc],
                            detail=self._grant_detail(grant))
            c.commit()
            self.gen += 1
        return self.get(hid), secret

    def update(self, hid: str, patch: dict, who: str = "gui", confirm=None) -> dict:
        if not isinstance(patch, dict):
            raise _bad("the body must be a JSON object")
        unknown = set(patch) - set(FIELDS) - {"confirm"}
        if unknown:
            raise _bad(f"unknown field {sorted(unknown)[0]!r}", sorted(unknown)[0])
        if "kind" in patch:
            raise _bad("kind: a hook's kind is fixed; make a new hook", "kind")
        confirm = confirm if confirm is not None else patch.get("confirm")
        patch = {k: v for k, v in patch.items() if k != "confirm"}
        old = self.need(hid)
        new = dict(old)
        new.update(patch)
        if "scope" in patch and patch["scope"] is None:
            new["scope"] = {}
        if "options" in patch and patch["options"] is None:
            new["options"] = {}
        self._check(new)
        grant, granted = self._grant(old, new, who, confirm)     # may resolve a name: unlocked
        changed = [k for k in patch if patch[k] != old.get(k)]
        if not changed and not granted:
            return old
        with self.cfg._lock:
            if self.need(hid)["version"] != old["version"]:
                raise HookError(409, "conflict", "the hook changed meanwhile; read it again")
            c = self._c()
            c.execute("UPDATE hooks SET name = ?, stages = ?, role = ?, priority = ?, mode = ?, "
                      "scope = ?, policy = ?, timeout_ms = ?, payload = ?, endpoint = ?, "
                      "target = ?, options = ?, grant_doc = ?, enabled = ?, updated_at = ?, "
                      "updated_by = ?, version = version + 1 WHERE id = ?",
                      (new["name"].strip(), json.dumps(new["stages"]), new["role"],
                       new["priority"], new["mode"], json.dumps(new["scope"], sort_keys=True),
                       new["policy"], new["timeout_ms"], new["payload"], new["endpoint"],
                       new["target"], json.dumps(new["options"], sort_keys=True),
                       json.dumps(grant, sort_keys=True) if grant else None,
                       int(new["enabled"]), _now_ms(), who, hid))
            self.cfg._audit(who, "update", "hook", hid, changed or ["payload"],
                            detail=self._grant_detail(grant) if granted or
                            "payload" in changed else None)
            c.commit()
            self.gen += 1
        return self.get(hid)

    @staticmethod
    def _grant_detail(grant: dict | None) -> dict | None:
        if not grant:
            return None
        return {"payload": "full", "destination": grant.get("destination"),
                "lan": grant.get("lan"), "confirmed": grant.get("confirmed_by") is not None}

    def delete(self, hid: str, who: str = "gui") -> None:
        with self.cfg._lock:
            self.need(hid)
            c = self._c()
            c.execute("DELETE FROM hooks WHERE id = ?", (hid,))
            c.execute("DELETE FROM hook_secrets WHERE hook_id = ?", (hid,))
            self.cfg._audit(who, "delete", "hook", hid)
            c.commit()
            self.gen += 1

    # ------------------------------------------------------------------ secrets
    def _put_secret(self, hid: str, secret: str, now: int) -> str:
        ref = f"{hid}:{_rand.token_hex(6)}"
        ct, nonce, kid = self.cfg.vault().encrypt(f"hook-secret:{ref}", secret)
        h = vault_mod.hint(secret)
        self._c().execute("INSERT INTO hook_secrets(ref, hook_id, ciphertext, nonce, "
                          "master_key_id, last4, sha256_6, created_at, expires_at) VALUES "
                          "(?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                          (ref, hid, ct, nonce, kid, h["last4"], h["sha256_6"], now))
        return ref

    def rotate(self, hid: str, who: str = "gui", grace_s: int = 0) -> str:
        """A new secret, answered once. The previous one still signs beside it for `grace_s`
        seconds (0: at once replaced)."""
        if isinstance(grace_s, bool) or not isinstance(grace_s, int) or \
                not 0 <= grace_s <= GRACE_MAX_S:
            raise _bad(f"grace_s: 0..{GRACE_MAX_S}", "grace_s")
        with self.cfg._lock:
            h = self.need(hid)
            if h["kind"] != "http":
                raise HookError(409, "no_secret", "only an http hook has a secret")
            now = _now_ms()
            c = self._c()
            c.execute("DELETE FROM hook_secrets WHERE hook_id = ? AND ref != ?",
                      (hid, h["secret_ref"] or ""))
            if h["secret_ref"]:
                if grace_s:
                    c.execute("UPDATE hook_secrets SET expires_at = ? WHERE ref = ?",
                              (now + grace_s * 1000, h["secret_ref"]))
                else:
                    c.execute("DELETE FROM hook_secrets WHERE ref = ?", (h["secret_ref"],))
            secret = hookcall.new_secret()
            ref = self._put_secret(hid, secret, now)
            c.execute("UPDATE hooks SET secret_ref = ?, updated_at = ?, updated_by = ?, "
                      "version = version + 1 WHERE id = ?", (ref, now, who, hid))
            self.cfg._audit(who, "rotate", "hook_secret", hid, ["secret"],
                            detail={"grace_s": grace_s})
            c.commit()
            self.gen += 1
        return secret

    def secrets_for(self, hid: str, ref: str | None) -> list[str]:
        """The secrets that sign a call now: the current first, then a previous one in its
        grace. Plain text, for the signature and nothing else; SecretError when unreadable."""
        now = _now_ms()
        with self.cfg._lock:
            rows = self._c().execute("SELECT ref, ciphertext, nonce, master_key_id, expires_at "
                                     "FROM hook_secrets WHERE hook_id = ?", (hid,)).fetchall()
        out = []
        for r, ct, nonce, kid, exp in sorted(rows, key=lambda x: x[0] != ref):
            if r != ref and (exp is None or exp <= now):
                continue
            out.append(self.cfg.vault().decrypt(f"hook-secret:{r}", bytes(ct), bytes(nonce), kid))
        return out

    def secret_hint(self, hid: str, ref: str | None) -> dict:
        now = _now_ms()
        with self.cfg._lock:
            rows = self._c().execute("SELECT ref, last4, sha256_6, created_at, expires_at FROM "
                                     "hook_secrets WHERE hook_id = ?", (hid,)).fetchall()
        cur = next((r for r in rows if r[0] == ref), None)
        prev = [r for r in rows if r[0] != ref and r[4] is not None and r[4] > now]
        return {"set": cur is not None, "last4": cur[1] if cur else None,
                "sha256_6": cur[2] if cur else None, "rotated_at": cur[3] if cur else None,
                "previous_until": max(r[4] for r in prev) if prev else None}
