"""SEC-22/SEC-24: a config database the accounts have written still opens in the release before
them (0.15, `pihooks-int5`), so a rollback finds its state (tests/int5_schema.py is that
release's schema, frozen).

  1  with TANDEM_AUTH=accounts the accounts, sessions and group roles are new tables: the schema
     number stays 2, and every table of that release has exactly the columns it expects
                                                                   test_only_new_tables
  2  that release's migration runs on the database without an error, reads its users, audit
     and settings rows, and the integrity and foreign key checks pass; opened again with the
     accounts on, every account and session is still there        test_int5_opens_it
  3  a move's export carries no account, session or group role     test_export_leaves_accounts

    python tests/test_auth_int5_db.py
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import int5_schema  # noqa: E402
from proxy import accounts as acc_mod  # noqa: E402
from proxy import config as config_mod  # noqa: E402
from proxy_util import run  # noqa: E402

NEW = {"accounts", "sessions", "group_roles"}


def _expected() -> dict:
    """{table: [columns]} as that release makes them: its DDL, then its added columns."""
    c = sqlite3.connect(":memory:")
    int5_schema.migrate_like_int5(c)
    out = {t: [r[1] for r in c.execute(f"PRAGMA table_info({t})")]
           for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    c.close()
    return out


def _written() -> str:
    d = tempfile.mkdtemp(prefix="int5-db-")
    cfg = config_mod.ConfigDB(os.path.join(d, "config.sqlite3"), env={},
                              use_keychain=False).open()
    a = acc_mod.AccountsAuth(cfg)
    own = a.store.create("own", "owner", "a long enough password", actor="test")
    a.store.create("view", "viewer", "another long password", actor="test")
    a.store.open_session(own["id"], "192.0.2.5", "a browser")
    cfg.set_setting("auth.session.idle_days", 7, who="test")
    with cfg._lock:
        cfg._conn.execute("INSERT INTO group_roles VALUES ('ldap', 'ops', 'operator')")
        cfg._conn.commit()
    cfg.close()
    return os.path.join(d, "config.sqlite3")


def test_only_new_tables():
    path = _written()
    c = sqlite3.connect(path)
    got = {t: [r[1] for r in c.execute(f"PRAGMA table_info({t})")]
           for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "2"
    c.close()
    want = _expected()
    assert NEW <= set(got) and not NEW & set(want)
    for t, cols in want.items():
        assert got.get(t) == cols, (t, got.get(t), cols)
    assert config_mod.SCHEMA_VERSION == int5_schema.SCHEMA_VERSION
    # the release's tables are this release's: the accounts add none of their own to config.py
    assert re.sub(r"\s+", " ", config_mod.DDL) == re.sub(r"\s+", " ", int5_schema.DDL)


def test_int5_opens_it():
    path = _written()
    c = sqlite3.connect(path)
    c.execute("PRAGMA foreign_keys=ON")
    assert int5_schema.migrate_like_int5(c) == 2
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    # its reads: the users' keys, the audit, the settings (each value JSON)
    c.execute("SELECT id, label, key_hash, key_hint, role, enabled FROM users").fetchall()
    rows = c.execute("SELECT ts_ms, who, action, object, object_id, fields, detail, change_set "
                     "FROM audit").fetchall()
    assert rows and all(json.loads(r[6]) is not None for r in rows if r[6])
    for k, v in c.execute("SELECT key, value FROM settings"):
        json.loads(v)
    c.close()
    # forward again: nothing lost
    cfg = config_mod.ConfigDB(path, env={}, use_keychain=False).open()
    a = acc_mod.AccountsAuth(cfg)
    assert sorted(r["username"] for r in a.store.all()) == ["own", "view"]
    assert len(a.store.sessions()) == 1 and a.policy.idle_days == 7
    cfg.close()


def test_export_leaves_accounts():
    import base64
    from proxy import ctlmove
    path = _written()
    cfg = config_mod.ConfigDB(path, env={}, use_keychain=False).open()
    acc_mod.AccountsAuth(cfg)
    bundle = ctlmove.export(cfg, keys=False)
    cfg.close()
    out = os.path.join(tempfile.mkdtemp(prefix="export-"), "export.sqlite3")
    with open(out, "wb") as f:
        f.write(base64.b64decode(bundle["db"]))
    c = sqlite3.connect(out)
    for t in NEW:
        assert c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0, t
        assert t not in ctlmove.TABLES
    c.close()

if __name__ == "__main__":
    run(globals())
