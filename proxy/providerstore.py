"""The decision providers' registry (SRV-851): two tables of their own in the config database.

    decision_providers      one row per provider: id, name, kind (builtin, systemone-http, hook,
                            plugin), endpoint (systemone-http), hook id (hook), target (plugin:
                            `<plugin>/<provider>`, SRV-859), model, timeout, the
                            questions it answers (JSON: question id -> {model?, endpoint?,
                            timeout_ms?}), mode (on, shadow, ab), the A/B share and its sticky
                            key, enabled, created and updated (when, by whom), version
    decision_provider_keys  an API key as AES-256-GCM ciphertext under the master key
                            (proxy/secrets.py), `decision-provider-key:<ref>` as associated data,
                            its last 4 characters and a sha256 prefix for the page; scope `*` is
                            the provider's own key, a question id that question's

THE PAYLOAD (SEC-16). A systemone-http provider is sent the question's text (a digest of the
conversation: what people wrote), so it takes the rules of a hook at payload `full`: making one,
or changing where it or one of its questions answers, is a grant, which the API plans only for a
caller with pairing's rights (403 trust_required). The grant (`grant_doc`) records who and when,
each destination host and whether it is on the LAN; a host outside the LAN needs a second confirm
(409 confirm_needed with a `confirm` bound to the provider, the hosts outside the LAN and its
version; the same write again with it), and the grant then records who confirmed it. A call to a
host the grant does not name, or outside the LAN without its confirm, is not made (`not_granted`:
the built-in answers). A row from before the grant counts as granted for a host on the LAN.

A key is write-only: a create or a change sets it (`api_key`, or `questions.<id>.api_key`; null
clears it) and no answer, log line, audit row or journal row holds it, only its hint. Every write
is audited (field names, never a key) and bumps `gen`, which the runtime reads.

THE QUESTIONS (`QUESTIONS`) are the conductor's question ids that reach its decider stage:
classify, packages, deliverable (the router), memory_intent (the memory notes), memory_gate (the
memory writer) and judge (the tester's questions after a turn). One enabled provider may serve a
question `on` or `ab`; any number may watch it in `shadow`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets as _rand
import time
from urllib.parse import urlsplit

from proxy import hookcall
from proxy import secrets as vault_mod

DDL = """
CREATE TABLE IF NOT EXISTS decision_providers (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, endpoint TEXT, hook_id TEXT,
  model TEXT NOT NULL DEFAULT 'auto', timeout_ms INTEGER NOT NULL DEFAULT 400,
  questions TEXT NOT NULL DEFAULT '{}', mode TEXT NOT NULL DEFAULT 'shadow',
  ab_percent INTEGER NOT NULL DEFAULT 0, sticky TEXT NOT NULL DEFAULT 'session',
  enabled INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
  updated_by TEXT, version INTEGER NOT NULL DEFAULT 1, target TEXT, grant_doc TEXT);
CREATE TABLE IF NOT EXISTS decision_provider_keys (
  ref TEXT PRIMARY KEY, provider_id TEXT NOT NULL, scope TEXT NOT NULL, ciphertext BLOB NOT NULL,
  nonce BLOB NOT NULL, master_key_id TEXT NOT NULL, last4 TEXT, sha256_6 TEXT,
  created_at INTEGER NOT NULL, UNIQUE(provider_id, scope));
"""
#: the conductor's questions a provider may answer, and where each is asked (docs, the API)
QUESTIONS = {
    "classify": "the router: which class a new user message is",
    "packages": "the router and the library: which package fits the turn",
    "deliverable": "the router: what the turn is to deliver",
    "memory_intent": "the memory notes: does the message ask to remember, forget or recall",
    "memory_gate": "the memory writer: is there anything worth a note in the session",
    "judge": "the tester's questions after a turn: a failure in a tool output, a build's end",
}
KINDS = ("builtin", "systemone-http", "hook", "plugin")
MODES = ("on", "shadow", "ab")
STICKY = ("session", "user", "request")
DEFAULT_SCOPE = "*"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,47}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")
FIELDS = ("name", "kind", "endpoint", "hook_id", "target", "model", "timeout_ms", "questions",
          "mode", "ab_percent", "sticky", "enabled", "api_key")
TARGET_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}/[a-z0-9][a-z0-9_-]{0,47}$")
QUESTION_FIELDS = ("model", "endpoint", "timeout_ms", "api_key")
TIMEOUT_MAX_MS = 5000
KEY_MAX = 4096


class ProviderError(Exception):
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


def destinations(d: dict) -> dict:
    """host -> on the LAN, of every endpoint a systemone-http provider's text goes to (its own
    and its questions'); empty for another kind (a hook has its own grant, a plugin and the
    built-in run on this host)."""
    if d.get("kind") != "systemone-http":
        return {}
    urls = [d.get("endpoint")] + [(spec or {}).get("endpoint")
                                  for spec in (d.get("questions") or {}).values()
                                  if isinstance(spec, dict)]
    out = {}
    for u in urls:
        if isinstance(u, str) and u:
            host = hookcall.destination(u)["host"]
            if host and host not in out:
                out[host] = hookcall.host_is_lan(host)
    return out


_LAN_SEEN: dict = {}


def granted(p: dict, url: str | None) -> bool:
    """A call may send the question's text to `url`: its host is in the provider's grant (outside
    the LAN: confirmed); a row from before the grant, for a host on the LAN."""
    if p.get("kind") != "systemone-http" or not url:
        return True
    host = hookcall.destination(url)["host"]
    g = p.get("grant")
    if g:
        if host not in (g.get("destinations") or {}):
            return False
        return bool(g["destinations"][host]) or g.get("confirmed_by") is not None
    now = time.monotonic()
    got = _LAN_SEEN.get(host)
    if got is None or now - got[0] > 60.0:
        _LAN_SEEN[host] = got = (now, hookcall.host_is_lan(host))
    return got[1]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _bad(message: str, field: str | None = None) -> ProviderError:
    return ProviderError(422, "invalid", message, field)


def origin(url: str | None) -> tuple | None:
    """(scheme, host, port) of a URL, the default port filled in."""
    if not url:
        return None
    u = urlsplit(url)
    try:
        port = u.port or (443 if u.scheme == "https" else 80)
    except ValueError:
        return None
    return (u.scheme, (u.hostname or "").lower(), port)


class ProviderStore:
    """The tables over a `proxy.config.ConfigDB` (its connection and lock). `hook(id)` reads a
    hook of the hooks' registry (a `hook` provider names one)."""

    def __init__(self, cfg, hook=None):
        self.cfg = cfg
        self.hook = hook or (lambda hid: None)
        # SRV-859: fn(target) -> {questions} of a plugin's declared provider, or None
        self.plugin_target = None
        self.gen = 0
        self._ready = False
        self._secret = _rand.token_bytes(32)          # the confirm tokens' key, this process's

    # ------------------------------------------------------------------ lifecycle
    def ready(self) -> bool:
        if self._ready:
            return True
        conn = getattr(self.cfg, "_conn", None)
        if conn is None:
            return False
        with self.cfg._lock:            # the config database's lock alone (proxy/hookstore.py)
            if not self._ready:
                conn.executescript(DDL)
                cols = {r[1] for r in conn.execute("PRAGMA table_info(decision_providers)")}
                if "target" not in cols:                     # a table made before SRV-859
                    conn.execute("ALTER TABLE decision_providers ADD COLUMN target TEXT")
                if "grant_doc" not in cols:                  # a table made before SEC-16's
                    conn.execute("ALTER TABLE decision_providers ADD COLUMN grant_doc TEXT")
                conn.commit()
                self._ready = True
        return True

    def _c(self):
        if not self.ready():
            raise ProviderError(503, "unavailable", "the config database is not open")
        return self.cfg._conn

    def data_version(self) -> int | None:
        try:
            with self.cfg._lock:
                return int(self._c().execute("PRAGMA data_version").fetchone()[0])
        except Exception:                                          # noqa: BLE001
            return None

    # ------------------------------------------------------------------ reads
    _COLS = ("id", "name", "kind", "endpoint", "hook_id", "model", "timeout_ms", "questions",
             "mode", "ab_percent", "sticky", "enabled", "created_at", "updated_at", "updated_by",
             "version", "target", "grant_doc")

    def _row(self, r) -> dict:
        d = dict(zip(self._COLS, r))
        try:
            d["questions"] = json.loads(d["questions"]) if d["questions"] else {}
        except ValueError:
            d["questions"] = {}
        try:
            d["grant"] = json.loads(d.pop("grant_doc")) if d.get("grant_doc") else None
        except ValueError:
            d["grant"] = None
        d["enabled"] = bool(d["enabled"])
        return d

    def list(self) -> list[dict]:
        with self.cfg._lock:
            rows = self._c().execute(f"SELECT {', '.join(self._COLS)} FROM decision_providers "
                                     f"ORDER BY id").fetchall()
        return [self._row(r) for r in rows]

    def get(self, pid: str) -> dict | None:
        with self.cfg._lock:
            r = self._c().execute(f"SELECT {', '.join(self._COLS)} FROM decision_providers "
                                  f"WHERE id = ?", (pid,)).fetchone()
        return self._row(r) if r else None

    def need(self, pid: str) -> dict:
        p = self.get(pid)
        if p is None:
            raise ProviderError(404, "not_found", f"no decision provider {pid!r}")
        return p

    # ------------------------------------------------------------------ validation
    def _check(self, d: dict) -> None:
        if not isinstance(d.get("name"), str) or not 1 <= len(d["name"].strip()) <= 80:
            raise _bad("name: 1 to 80 characters", "name")
        if d["kind"] not in KINDS:
            raise _bad(f"kind: one of {', '.join(KINDS)}", "kind")
        if d["kind"] == "systemone-http":
            why = hookcall.check_url(d["endpoint"])
            if why:
                raise _bad(f"endpoint: {why}", "endpoint")
        elif d["endpoint"] is not None:
            raise _bad("endpoint: only for a systemone-http provider (a hook has its own)",
                       "endpoint")
        if d["kind"] == "hook":
            h = self.hook(d["hook_id"]) if isinstance(d["hook_id"], str) else None
            if h is None:
                raise _bad("hook_id: the id of a hook in /v1/fleet/hooks", "hook_id")
            if "decision.ask" not in h.get("stages", ()) or h.get("role") != "decider":
                raise _bad("hook_id: the hook must be a decider at the stage decision.ask",
                           "hook_id")
        elif d["hook_id"] is not None:
            raise _bad("hook_id: only for a hook provider", "hook_id")
        if d["kind"] == "plugin":
            t = d.get("target")
            if not isinstance(t, str) or not TARGET_RE.match(t):
                raise _bad("target: <plugin>/<provider>, a provider a plugin declares", "target")
            want = self.plugin_target(t) if self.plugin_target is not None else None
            if want is None:
                raise _bad(f"target: no installed plugin declares the provider {t}", "target")
            other = sorted(set(d["questions"] or {}) - set(want["questions"]))
            if other:
                raise _bad(f"questions: {t} answers {', '.join(want['questions'])}, not "
                           f"{other[0]}", "questions")
        elif d.get("target") is not None:
            raise _bad("target: only for a plugin provider", "target")
        if not isinstance(d["model"], str) or not MODEL_RE.match(d["model"]):
            raise _bad("model: a model id or auto", "model")
        t = d["timeout_ms"]
        if isinstance(t, bool) or not isinstance(t, int) or not 1 <= t <= TIMEOUT_MAX_MS:
            raise _bad(f"timeout_ms: 1..{TIMEOUT_MAX_MS}", "timeout_ms")
        if d["mode"] not in MODES:
            raise _bad(f"mode: one of {', '.join(MODES)}", "mode")
        a = d["ab_percent"]
        if isinstance(a, bool) or not isinstance(a, int) or not 0 <= a <= 100:
            raise _bad("ab_percent: 0..100, the share of sessions the provider answers in ab",
                       "ab_percent")
        if d["sticky"] not in STICKY:
            raise _bad(f"sticky: one of {', '.join(STICKY)}", "sticky")
        if not isinstance(d["enabled"], bool):
            raise _bad("enabled: true or false", "enabled")
        qs = d["questions"]
        if not isinstance(qs, dict) or not qs:
            raise _bad(f"questions: an object of the question ids it answers "
                       f"({', '.join(QUESTIONS)})", "questions")
        for qid, spec in qs.items():
            f = f"questions.{qid}"
            if qid not in QUESTIONS:
                raise _bad(f"questions: no question {qid!r} (one of {', '.join(QUESTIONS)})", f)
            if not isinstance(spec, dict) or set(spec) - set(QUESTION_FIELDS):
                raise _bad(f"{f}: an object of {', '.join(QUESTION_FIELDS)}", f)
            if spec.get("model") is not None and not (isinstance(spec["model"], str)
                                                      and MODEL_RE.match(spec["model"])):
                raise _bad(f"{f}.model: a model id or auto", f + ".model")
            if spec.get("endpoint") is not None:
                if d["kind"] != "systemone-http":
                    raise _bad(f"{f}.endpoint: only for a systemone-http provider",
                               f + ".endpoint")
                why = hookcall.check_url(spec["endpoint"])
                if why:
                    raise _bad(f"{f}.endpoint: {why}", f + ".endpoint")
            t = spec.get("timeout_ms")
            if t is not None and (isinstance(t, bool) or not isinstance(t, int)
                                  or not 1 <= t <= TIMEOUT_MAX_MS):
                raise _bad(f"{f}.timeout_ms: 1..{TIMEOUT_MAX_MS}", f + ".timeout_ms")
            k = spec.get("api_key")
            if k is not None:
                self._check_key(k, d["kind"], f + ".api_key")
        if d.get("api_key") is not None:
            self._check_key(d["api_key"], d["kind"], "api_key")

    @staticmethod
    def _check_key(k, kind: str, field: str) -> None:
        if kind != "systemone-http":
            raise _bad(f"{field}: only a systemone-http provider sends a key (a hook signs with "
                       f"its own secret)", field)
        if not isinstance(k, str) or not 1 <= len(k) <= KEY_MAX or any(
                c.isspace() or ord(c) < 32 for c in k):
            raise _bad(f"{field}: 1..{KEY_MAX} characters, no spaces", field)

    def _taken(self, d: dict, pid: str) -> None:
        """409 when another enabled provider serves one of `d`'s questions on or ab."""
        if not d["enabled"] or d["mode"] == "shadow":
            return
        for p in self.list():
            if p["id"] == pid or not p["enabled"] or p["mode"] == "shadow":
                continue
            both = sorted(set(p["questions"]) & set(d["questions"]))
            if both:
                raise ProviderError(409, "question_taken", (
                    f"the provider {p['id']!r} serves {', '.join(both)} {p['mode']}: one provider "
                    f"a question on or ab (any number in shadow)"), "questions")

    # ------------------------------------------------------------------ the payload (SEC-16)
    def confirm_token(self, pid: str, hosts, version: int) -> str:
        msg = f"{pid}\0{','.join(sorted(hosts))}\0{version}".encode()
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()[:32]

    @staticmethod
    def needs_grant(old: dict | None, new: dict) -> bool:
        """The write sends the question's text to a host the provider's grant does not name yet
        (a new systemone-http provider, or a new endpoint): pairing's rights."""
        dests = destinations(new)
        if not dests:
            return False
        g = (old or {}).get("grant") or {}
        have = g.get("destinations") or {}
        if old is not None and not g:
            have = destinations(old)        # a row from before the grant: what it reached
        return any(h not in have for h in dests)

    @staticmethod
    def away(old: dict | None, new: dict) -> list[str]:
        """The hosts outside the LAN a write would send the text to that the grant does not hold
        confirmed: the ones its confirm is bound to."""
        g = (old or {}).get("grant") or {}
        dests = destinations(new)
        if g and all(h in (g.get("destinations") or {}) for h in dests):
            return []
        sure = set(g.get("destinations") or {}) if g.get("confirmed_by") else set()
        return sorted(h for h, lan in dests.items() if not lan and h not in sure)

    def _grant(self, old: dict | None, new: dict, who: str, confirm) -> tuple[dict | None, bool]:
        """(the grant the new row carries, whether one was made now). A host outside the LAN
        that the grant does not hold confirmed needs the confirm (409 confirm_needed)."""
        dests = destinations(new)
        if not dests:
            return None, False
        g = (old or {}).get("grant")
        if g and all(h in (g.get("destinations") or {}) for h in dests):
            kept = {h: g["destinations"][h] for h in dests}
            return dict(g, destinations=kept), kept != g.get("destinations")
        away = self.away(old, new)
        pid = new.get("id") or ""
        confirmed = bool(g and g.get("confirmed_by")) and not away
        now = _now_ms()
        if away:
            want = self.confirm_token(pid, away, (old or {}).get("version", 0))
            if not (isinstance(confirm, str) and hmac.compare_digest(confirm, want)):
                raise ProviderError(409, "confirm_needed", (
                    f"the questions' text would go to {', '.join(away)}, outside the LAN: send "
                    f"the same request again with this confirm to grant it"), "endpoint",
                    extra={"confirm": want, "destination": ", ".join(away), "lan": False})
            confirmed = True
        outside = any(not lan for lan in dests.values())
        return {"by": who, "at": now, "destinations": dests,
                "confirmed_by": (who if away else (g or {}).get("confirmed_by"))
                if outside and confirmed else None,
                "confirmed_at": (now if away else (g or {}).get("confirmed_at"))
                if outside and confirmed else None}, True

    # ------------------------------------------------------------------ writes
    @staticmethod
    def _split(questions: dict) -> tuple[dict, dict]:
        """(the mapping without keys, question id -> the key the write gives: a string sets, None
        clears; a question that names no key is absent)."""
        mapping, keys = {}, {}
        for qid, spec in questions.items():
            spec = dict(spec or {})
            if "api_key" in spec:
                keys[qid] = spec.pop("api_key")
            mapping[qid] = {k: v for k, v in spec.items() if v is not None}
        return mapping, keys

    def _free_id(self, name: str) -> str:
        base = re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-._")[:40] or "provider"
        if not ID_RE.match(base):
            base = "provider"
        pid, n = base, 2
        while self.get(pid) is not None:
            pid = f"{base}-{n}"
            n += 1
        return pid

    def create(self, doc: dict, who: str = "gui", confirm=None) -> dict:
        if not isinstance(doc, dict):
            raise _bad("the body must be a JSON object")
        confirm = confirm if confirm is not None else doc.get("confirm")
        doc = {k: v for k, v in doc.items() if k != "confirm"}
        unknown = set(doc) - set(FIELDS) - {"id"}
        if unknown:
            raise _bad(f"unknown field {sorted(unknown)[0]!r}", sorted(unknown)[0])
        d = {"name": doc.get("name"), "kind": doc.get("kind", "systemone-http"),
             "endpoint": doc.get("endpoint"), "hook_id": doc.get("hook_id"),
             "target": doc.get("target"),
             "model": doc.get("model", "auto"), "timeout_ms": doc.get("timeout_ms", 400),
             "questions": doc.get("questions"), "mode": doc.get("mode", "shadow"),
             "ab_percent": doc.get("ab_percent", 0), "sticky": doc.get("sticky", "session"),
             "enabled": doc.get("enabled", True), "api_key": doc.get("api_key")}
        self._check(d)
        pid = doc.get("id")
        if pid is None:
            pid = self._free_id(d["name"])
        elif not isinstance(pid, str) or not ID_RE.match(pid):
            raise _bad("id: [a-z0-9][a-z0-9._-]{0,47}", "id")
        mapping, qkeys = self._split(d["questions"])
        if self.get(pid) is not None:
            raise ProviderError(409, "exists", f"a decision provider {pid!r} exists", "id")
        grant, _ = self._grant(None, dict(d, id=pid, questions=mapping), who, confirm)
        now = _now_ms()
        with self.cfg._lock:
            if self.get(pid) is not None:
                raise ProviderError(409, "exists", f"a decision provider {pid!r} exists", "id")
            self._taken(dict(d, questions=mapping), pid)
            c = self._c()
            c.execute("INSERT INTO decision_providers(id, name, kind, endpoint, hook_id, model, "
                      "timeout_ms, questions, mode, ab_percent, sticky, enabled, created_at, "
                      "updated_at, updated_by, version, target, grant_doc) VALUES "
                      "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                      (pid, d["name"].strip(), d["kind"], d["endpoint"], d["hook_id"],
                       d["model"], d["timeout_ms"], json.dumps(mapping, sort_keys=True),
                       d["mode"], d["ab_percent"], d["sticky"], int(d["enabled"]), now, now,
                       who, d["target"], json.dumps(grant, sort_keys=True) if grant else None))
            keys = dict(qkeys)
            if d["api_key"] is not None:
                keys[DEFAULT_SCOPE] = d["api_key"]
            set_ = self._put_keys(pid, keys, now)
            detail = {k: v for k, v in (("keys_set", set_),
                                        ("grant", self._grant_detail(grant))) if v}
            self.cfg._audit(who, "create", "decision_provider", pid,
                            [k for k in FIELDS if k in doc], detail=detail or None)
            c.commit()
            self.gen += 1
        return self.get(pid)

    @staticmethod
    def _grant_detail(grant: dict | None) -> dict | None:
        if not grant:
            return None
        return {"destinations": grant.get("destinations"),
                "confirmed": grant.get("confirmed_by") is not None}

    def update(self, pid: str, patch: dict, who: str = "gui", confirm=None) -> dict:
        if not isinstance(patch, dict):
            raise _bad("the body must be a JSON object")
        confirm = confirm if confirm is not None else patch.get("confirm")
        patch = {k: v for k, v in patch.items() if k != "confirm"}
        unknown = set(patch) - set(FIELDS)
        if unknown:
            raise _bad(f"unknown field {sorted(unknown)[0]!r}", sorted(unknown)[0])
        if "kind" in patch:
            raise _bad("kind: a provider's kind is fixed; make a new provider", "kind")
        old = self.need(pid)
        new = dict(old)
        new.update({k: v for k, v in patch.items() if k != "api_key"})
        new["api_key"] = patch.get("api_key")
        if "questions" in patch and patch["questions"] is None:
            raise _bad("questions: an object of the question ids it answers", "questions")
        self._check(new)
        mapping, qkeys = self._split(new["questions"])
        keys = {} if "questions" not in patch else dict(qkeys)
        if "api_key" in patch:
            keys[DEFAULT_SCOPE] = patch["api_key"]
        changed = [k for k in patch if k == "api_key" or
                   (k == "questions" and (mapping != old["questions"] or qkeys)) or
                   (k not in ("api_key", "questions") and patch[k] != old.get(k))]
        if not changed:
            return old
        grant, granted = self._grant(old, dict(new, id=pid, questions=mapping), who, confirm)
        now = _now_ms()
        with self.cfg._lock:
            if self.need(pid)["version"] != old["version"]:
                raise ProviderError(409, "conflict", "the provider changed meanwhile; read it "
                                                     "again")
            self._taken(dict(new, questions=mapping), pid)
            c = self._c()
            c.execute("UPDATE decision_providers SET name = ?, endpoint = ?, hook_id = ?, "
                      "model = ?, timeout_ms = ?, questions = ?, mode = ?, ab_percent = ?, "
                      "sticky = ?, enabled = ?, updated_at = ?, updated_by = ?, target = ?, "
                      "grant_doc = ?, version = version + 1 WHERE id = ?",
                      (new["name"].strip(), new["endpoint"], new["hook_id"], new["model"],
                       new["timeout_ms"], json.dumps(mapping, sort_keys=True), new["mode"],
                       new["ab_percent"], new["sticky"], int(new["enabled"]), now, who,
                       new.get("target"), json.dumps(grant, sort_keys=True) if grant else None,
                       pid))
            # a question no longer answered takes its key with it
            gone = [q for q in old["questions"] if q not in mapping]
            for q in gone:
                c.execute("DELETE FROM decision_provider_keys WHERE provider_id = ? AND "
                          "scope = ?", (pid, q))
            set_ = self._put_keys(pid, {k: v for k, v in keys.items() if v is not None}, now)
            cleared = sorted(k for k, v in keys.items() if v is None)
            for scope in cleared:
                c.execute("DELETE FROM decision_provider_keys WHERE provider_id = ? AND "
                          "scope = ?", (pid, scope))
            detail = {k: v for k, v in (("keys_set", set_), ("keys_cleared", cleared + gone),
                                        ("grant", self._grant_detail(grant) if granted
                                         else None))
                      if v}
            self.cfg._audit(who, "update", "decision_provider", pid, changed,
                            detail=detail or None)
            c.commit()
            self.gen += 1
        return self.get(pid)

    def delete(self, pid: str, who: str = "gui") -> None:
        with self.cfg._lock:
            self.need(pid)
            c = self._c()
            c.execute("DELETE FROM decision_providers WHERE id = ?", (pid,))
            c.execute("DELETE FROM decision_provider_keys WHERE provider_id = ?", (pid,))
            self.cfg._audit(who, "delete", "decision_provider", pid)
            c.commit()
            self.gen += 1

    # ------------------------------------------------------------------ keys
    def _put_keys(self, pid: str, keys: dict, now: int) -> list[str]:
        """Seal each scope's key (under the config lock, the caller commits); the scopes set."""
        out = []
        c = self._c()
        for scope, key in sorted(keys.items()):
            if key is None:
                continue
            ref = f"{pid}:{scope}:{_rand.token_hex(6)}"
            ct, nonce, kid = self.cfg.vault().encrypt(f"decision-provider-key:{ref}", key)
            h = vault_mod.hint(key)
            c.execute("DELETE FROM decision_provider_keys WHERE provider_id = ? AND scope = ?",
                      (pid, scope))
            c.execute("INSERT INTO decision_provider_keys(ref, provider_id, scope, ciphertext, "
                      "nonce, master_key_id, last4, sha256_6, created_at) VALUES "
                      "(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (ref, pid, scope, ct, nonce, kid, h["last4"], h["sha256_6"], now))
            out.append(scope)
        return out

    def key_for(self, pid: str, scope: str) -> str | None:
        """The key in plain text, for the call's Authorization header and nothing else; None
        when none is set. SecretError when it cannot be read."""
        with self.cfg._lock:
            r = self._c().execute("SELECT ref, ciphertext, nonce, master_key_id FROM "
                                  "decision_provider_keys WHERE provider_id = ? AND scope = ?",
                                  (pid, scope)).fetchone()
        if r is None:
            return None
        return self.cfg.vault().decrypt(f"decision-provider-key:{r[0]}", bytes(r[1]),
                                        bytes(r[2]), r[3])

    def key_version(self, pid: str) -> str:
        """Moves whenever a key of the provider is set or cleared (the runtime's client cache)."""
        with self.cfg._lock:
            rows = self._c().execute("SELECT ref FROM decision_provider_keys WHERE "
                                     "provider_id = ? ORDER BY ref", (pid,)).fetchall()
        return ",".join(r[0] for r in rows)

    def key_hints(self, pid: str) -> dict:
        """scope -> {key_set, last4, sha256_6, set_at}: what the API shows of the keys."""
        with self.cfg._lock:
            rows = self._c().execute("SELECT scope, last4, sha256_6, created_at FROM "
                                     "decision_provider_keys WHERE provider_id = ?",
                                     (pid,)).fetchall()
        return {s: {"key_set": True, "last4": l4, "sha256_6": h6, "set_at": at}
                for s, l4, h6, at in rows}


def no_key() -> dict:
    return {"key_set": False, "last4": None, "sha256_6": None, "set_at": None}
