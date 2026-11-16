"""The config database's schema as TandemApp 0.15 (`pihooks-int5`) creates and expects it: its
DDL, its in-place columns and its schema number, frozen here for SEC-22's rollback test
(tests/test_auth_int5_db.py). Copied from that release's proxy/config.py; never edit it to follow
a later schema. `migrate_like_int5` runs what that release's ConfigDB.migrate runs at a start.
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
