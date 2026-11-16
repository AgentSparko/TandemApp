"""The config database's schema as TandemApp 0.15 (`pihooks-int5`) creates and expects it: its
DDL, its in-place columns and its schema number, frozen here for SEC-22's rollback test
(tests/test_auth_int5_db.py). Copied from that release's proxy/config.py; never edit it to follow
a later schema. `migrate_like_int5` runs what that release's ConfigDB.migrate runs at a start.

SEC-28 adds that release's sign-in, frozen the same way (its tandemapp/auth.py Auth, which the
flip removed from the tree): `state_like_int5` writes a state folder as it writes one,
`sign_in_like_int5` is its sign-in (the admin token, then admin.json), `cookie_like_int5` its
`qse_dash` cookie, and `key_rule_like_int5` its rule on the OpenAI routes.
"""

SCHEMA_VERSION = 2
OLD_DEFAULT_INFLIGHT = 64

DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS backends (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT, url TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1, models_mode TEXT NOT NULL DEFAULT 'discover',
  models TEXT NOT NULL DEFAULT '[]', fallback TEXT NOT NULL DEFAULT '[]',
  max_inflight INTEGER NOT NULL DEFAULT 0, timeouts TEXT NOT NULL DEFAULT '{}',
  inject_usage INTEGER, harness TEXT NOT NULL DEFAULT '{}',
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, updated_by TEXT,
  host TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS backend_secrets (
  backend_id TEXT PRIMARY KEY REFERENCES backends(id) ON DELETE CASCADE,
  ciphertext BLOB NOT NULL, nonce BLOB NOT NULL, key_id TEXT NOT NULL,
  last4 TEXT, sha256_6 TEXT, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS models (
  backend_id TEXT NOT NULL REFERENCES backends(id) ON DELETE CASCADE, model_id TEXT NOT NULL,
  public_id TEXT, enabled INTEGER NOT NULL DEFAULT 1, present INTEGER NOT NULL DEFAULT 1,
  source TEXT NOT NULL, discovered_at INTEGER, context_length INTEGER,
  price_input REAL, price_cached_input REAL, price_cache_write REAL, price_output REAL,
  currency TEXT, price_updated_at INTEGER, price_offpeak TEXT, fallback TEXT,
  PRIMARY KEY (backend_id, model_id));
CREATE TABLE IF NOT EXISTS model_features (
  backend_id TEXT NOT NULL, model_id TEXT NOT NULL, feature TEXT NOT NULL, value TEXT NOT NULL,
  PRIMARY KEY (backend_id, model_id, feature));
CREATE TABLE IF NOT EXISTS routing_rules (
  id INTEGER PRIMARY KEY, position INTEGER NOT NULL, pattern TEXT NOT NULL,
  backend_id TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, source TEXT NOT NULL,
  env_var TEXT);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, label TEXT NOT NULL UNIQUE, key_hash TEXT NOT NULL UNIQUE,
  key_hint TEXT, role TEXT NOT NULL DEFAULT 'user', enabled INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL, last_seen_at INTEGER);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL,
  updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY, ts_ms INTEGER NOT NULL, who TEXT, action TEXT NOT NULL,
  object TEXT NOT NULL, object_id TEXT, fields TEXT);
CREATE TABLE IF NOT EXISTS nodes (
  id TEXT PRIMARY KEY, alias TEXT NOT NULL, colour INTEGER NOT NULL,
  icon TEXT NOT NULL DEFAULT 'desk', tags TEXT NOT NULL DEFAULT '[]',
  class TEXT NOT NULL DEFAULT 'spark', hw TEXT, dmi TEXT, serial TEXT,
  transport TEXT NOT NULL DEFAULT 'lan', endpoint TEXT NOT NULL, address TEXT,
  trust TEXT NOT NULL DEFAULT '{}', owner TEXT, lease_end_ms INTEGER, cost_per_hour REAL,
  added_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS layouts (
  page TEXT NOT NULL, device TEXT NOT NULL, doc TEXT NOT NULL, saved_at REAL NOT NULL,
  PRIMARY KEY (page, device));
CREATE TABLE IF NOT EXISTS add_sessions (
  id TEXT PRIMARY KEY, state TEXT NOT NULL, doc TEXT NOT NULL, created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS fleet_jobs (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL, doc TEXT NOT NULL,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS deployments (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, model TEXT NOT NULL, pack TEXT NOT NULL DEFAULT '',
  type TEXT NOT NULL, members TEXT NOT NULL DEFAULT '[]', stages TEXT NOT NULL DEFAULT '[]',
  options TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL DEFAULT 'serving',
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS engine_host_keys (
  host_id TEXT PRIMARY KEY, key_id TEXT NOT NULL, public TEXT NOT NULL, ciphertext BLOB NOT NULL,
  nonce BLOB NOT NULL, master_key_id TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS settings_versions (
  scope TEXT NOT NULL, target TEXT NOT NULL, n INTEGER NOT NULL, at_ms INTEGER NOT NULL, who TEXT,
  change_set TEXT, note TEXT, keys TEXT NOT NULL DEFAULT '[]', doc TEXT NOT NULL,
  PRIMARY KEY (scope, target, n));
CREATE TABLE IF NOT EXISTS engine_http_tokens (
  host_id TEXT PRIMARY KEY, ciphertext BLOB NOT NULL, nonce BLOB NOT NULL,
  master_key_id TEXT NOT NULL, last4 TEXT, sha256_6 TEXT, rotated_at INTEGER NOT NULL);
"""

ADDED = (("models", "price_offpeak", "TEXT"),
         ("backends", "host", "TEXT NOT NULL DEFAULT '{}'"),       # P1.5
         # 0.11.3: what a provider's model list says of a model (DeepSeek's GET /models): its
         # own name, output limit, input kinds (JSON list) and effort levels (JSON object)
         ("models", "name", "TEXT"), ("models", "max_output", "INTEGER"),
         ("models", "input_modalities", "TEXT"), ("models", "effort", "TEXT"),
         # VIS-227: the operator's output limit, apart from the provider's so a discovery never
         # overwrites it
         ("models", "max_output_set", "INTEGER"),
         # SEC-4: what a node's pairing, take-over or release did (JSON, never a secret)
         ("audit", "detail", "TEXT"),
         # VIS-250: a node in maintenance (drained): {since, by} as JSON, NULL when it serves
         ("nodes", "drain", "TEXT"),
         # SRV-845: the settings change set an audit row belongs to
         ("audit", "change_set", "TEXT"))


def migrate_like_int5(c) -> int:
    """That release's migration on an open sqlite3 connection; the schema number it found. (Its
    replica_rules step reads routing_rules columns only an older branch wrote: a no-op here.)"""
    c.executescript(DDL)
    for table, col, kind in ADDED:
        if col not in {r[1] for r in c.execute(f"PRAGMA table_info({table})")}:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {kind}")
    got = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    was = int(got[0]) if got else 0
    if 0 < was < 2:
        c.execute("UPDATE backends SET max_inflight = 0 WHERE max_inflight = ?",
                  (OLD_DEFAULT_INFLIGHT,))
    if was < SCHEMA_VERSION:
        c.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                      [("schema_version", str(SCHEMA_VERSION))] + ([] if was else
                                                                   [("created_at", "0")]))
    c.commit()
    return was


def key_rule_like_int5(c, bearer, env_users=None,
                       trusted_local=False):
    """That release's rule on the OpenAI routes (its proxy/service.py `authorize` over its
    proxy/config.py `users_active` and `user_for_key`, SRV-867's rollback test), on an open
    sqlite3 connection: (200, the user's label or None) or (401, None). `env_users`: its
    TANDEM_API_KEYS as {sha256 hex: label}; `trusted_local`: a loopback caller with no forwarding
    header. TANDEM_API_USERS_FROM is not used by these tests."""
    import hashlib
    import hmac
    env_users = env_users or {}
    active = bool(env_users) or c.execute(
        "SELECT 1 FROM users WHERE enabled = 1 LIMIT 1").fetchone() is not None
    if not active:
        return 200, None
    label = None
    if bearer:
        h = hashlib.sha256(bearer.encode()).hexdigest()
        for eh, lab in env_users.items():
            if hmac.compare_digest(eh, h):
                label = lab
                break
        if label is None:
            r = c.execute("SELECT id, label FROM users WHERE key_hash = ? AND enabled = 1",
                          (h,)).fetchone()
            label = r[1] if r else None
    if label is not None:
        return 200, label
    if trusted_local:
        return 200, None
    return 401, None


def user_like_int5(c, label: str, key: str, enabled: bool = True) -> None:
    """A row of `users` as that release's ConfigDB.create_user writes it (the key's sha256)."""
    import hashlib
    import time
    c.execute("INSERT INTO users(label, key_hash, key_hint, role, enabled, created_at) "
              "VALUES (?, ?, ?, 'user', ?, ?)",
              (label, hashlib.sha256(key.encode()).hexdigest(), key[-4:], int(enabled),
               int(time.time() * 1000)))


def state_like_int5(folder: str, *, password: str | None = None, keys: dict | None = None,
                    chatd_key: str | None = None) -> str:
    """A state folder as that release leaves it: config.sqlite3 (its schema, its `users` rows:
    {label: key}, and its own chatd's row `chatd` with the key in chat/engine.key), admin.json
    when it had a password. Returns the config database's path."""
    import os
    import sqlite3
    path = os.path.join(folder, "config.sqlite3")
    c = sqlite3.connect(path)
    migrate_like_int5(c)
    for label, key in (keys or {}).items():
        user_like_int5(c, label, key)
    if chatd_key:
        user_like_int5(c, "chatd", chatd_key)
        os.makedirs(os.path.join(folder, "chat"), mode=0o700, exist_ok=True)
        with open(os.path.join(folder, "chat", "engine.key"), "w") as f:
            f.write(chatd_key + "\n")
    c.commit()
    c.close()
    if password:
        from proxy import adminpw          # its proxy/adminpw.py Password, unchanged since
        adminpw.Password(os.path.join(folder, "admin.json")).set(password)
    return path


def sign_in_like_int5(folder: str, value, admin_token: str | None = None) -> bool:
    """That release's sign-in (Auth.sign_in): the admin token in constant time, then the
    password in admin.json."""
    import hmac
    import os
    from proxy import adminpw
    if isinstance(value, str) and admin_token and \
            hmac.compare_digest(value.encode(), admin_token.encode()):
        return True
    pw = adminpw.Password(os.path.join(folder, "admin.json"))
    return bool(pw.is_set and pw.verify(value))


def cookie_like_int5(folder: str, admin_token: str | None = None, ttl_s: int = 3600) -> str:
    """A `qse_dash` cookie value as that release made it after a sign-in (Auth.make_cookie): the
    key from the admin token and admin.json's hash material."""
    import hashlib
    import hmac
    import os
    import secrets
    import time
    from proxy import adminpw
    tok = (hmac.new(admin_token.encode(), b"qse-dash-session-v1", hashlib.sha256).digest()
           if admin_token else None)
    m = adminpw.Password(os.path.join(folder, "admin.json")).material()
    key = tok if m is None else hmac.new(m, b"qse-dash-session-v2" + (tok or b""),
                                         hashlib.sha256).digest()
    assert key is not None, "that release had no session without a secret"
    exp = int(time.time() + ttl_s)
    nonce = secrets.token_hex(8)
    return f"{exp}.{nonce}." + hmac.new(key, f"{exp}.{nonce}".encode(), hashlib.sha256).hexdigest()

