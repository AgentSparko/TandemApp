"""`tandem-app import-backends` (ENG-413 WP14b): the backends of an engine's old relay into this
controller's config database.

Up to engine API 1.7 a TandemLLM engine could relay to other backends itself, with `--backends
<config database>` (`QSE_BACKENDS` in its serve.env). That file is this package's own format: the
relay was this code inside the engine's process. Since 1.8 the engine relays to nothing, and the
controller serves those backends. This copies them over:

    tandem-app import-backends --from ~/.qwen38-spark-engine/backends/config.sqlite3   what it would do
    tandem-app import-backends --from FILE --write [--disabled]                        do it

What is copied, per backend the controller does not have yet (an id it has is left as it is):

- the backend's row as stored (kind, label, URL, on or off, the model list and its mode, fallbacks,
  the in-flight cap, timeouts, usage injection, the harness and host settings);
- its key: read with the old file's master key (the key file beside it, else TANDEM_SECRETS_KEY)
  and stored again under this controller's (AES-256-GCM, as every key here). A key that cannot be
  read is left out and named; the key itself is never printed;
- its models as stored (public ids, on or off, prices, fallbacks, the output limit, what the
  provider said) and their conductor switches;
- the routing rules that name an imported backend, after this controller's own, as GUI rules.

What is not: the users. Their keys stay in the old file, and `TANDEM_API_USERS_FROM=<that file>`
makes this controller check them there, read only (docs/proxy.md, the data plane). Settings, the
audit log and the nodes stay too. A backend the engine's environment declared (TANDEM_BACKEND_<ID>_*)
is copied as a row; its variables, and a key that lived only in the environment, are named so they
can move to this controller's env file.

The old file is read from a copy (SQLite's backup API) and never written, moved or removed. With
--disabled every imported backend is stored off, for a switch that turns them on at the moment the
engine stops relaying. Without --write the import runs against a copy of this controller's
database too, so what it prints is what --write would do, and nothing is written anywhere.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import tempfile
import urllib.parse

from proxy import config as config_mod
from proxy import secrets as vault_mod

WHO = "import"
#: the backends columns create_backend takes as they are stored (JSON columns parsed)
_FIELDS = ("kind", "label", "url", "enabled", "models_mode", "models", "fallback", "max_inflight",
           "timeouts", "inject_usage", "harness", "host")
_JSON = ("models", "fallback", "timeouts", "harness", "host")


def _rows(conn, sql: str, args: tuple = ()) -> list[dict]:
    cur = conn.execute(sql, args)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def _cols(conn, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _old_vaults(src: str, env) -> list:
    """The master keys the old file's keys may be under: the key file beside it, then the
    environment's. Never the keychain (the engine's process had none), never a new key."""
    out = []
    key = os.path.join(os.path.dirname(src), vault_mod.KEY_FILE)
    if os.path.isfile(key):
        try:
            out.append(vault_mod.Vault.load(os.path.dirname(src), env={}, use_keychain=False))
        except vault_mod.SecretError:
            pass
    if env.get(vault_mod.ENV):
        try:
            out.append(vault_mod.Vault.load(os.path.dirname(src), env={vault_mod.ENV: env[vault_mod.ENV]},
                                            use_keychain=False))
        except vault_mod.SecretError:
            pass
    return out


def _old_key(vaults: list, bid: str, row: dict | None) -> tuple[str | None, str | None]:
    """(the key, None) or (None, why)."""
    if row is None:
        return None, None
    for v in vaults:
        if v.key_id == row["key_id"]:
            try:
                return v.decrypt(bid, bytes(row["ciphertext"]), bytes(row["nonce"]), row["key_id"]), None
            except vault_mod.SecretError as exc:
                return None, f"its key does not decrypt ({exc})"
    return None, "its key is under a master key not found here (the key file beside the old file, or TANDEM_SECRETS_KEY)"


def plan_and_apply(old, cfg, *, src: str, env, disabled: bool) -> dict:
    """Copy `old`'s backends (a ConfigDB on a copy of the engine's file) into `cfg`. Returns the
    report: {backends: [{id, state, notes}], models, features, rules, users, env_vars}."""
    o = old._conn
    have = {b["id"] for b in cfg.backends()}
    vaults = _old_vaults(src, env)
    secrets = {r["backend_id"]: r for r in _rows(o, "SELECT backend_id, ciphertext, nonce, key_id FROM backend_secrets")}
    report = {"backends": [], "models": 0, "features": 0, "rules": 0, "users": 0, "env_vars": []}
    done = []
    for raw in _rows(o, "SELECT * FROM backends ORDER BY created_at, id"):
        bid = raw["id"]
        notes = []
        if bid in have:
            report["backends"].append({"id": bid, "state": "kept", "notes": [
                "the controller has a backend of this id: left as it is, nothing of the old one copied"]})
            continue
        doc = {"id": bid}
        for f in _FIELDS:
            v = raw.get(f)
            if f in _JSON:
                v = json.loads(v) if v else ({} if f in ("timeouts", "harness", "host") else [])
            if f == "enabled":
                v = bool(v) and not disabled
            if f == "max_inflight":
                v = int(v) if v else None
            if f == "inject_usage":
                if v is None:
                    continue
                v = bool(v)
            doc[f] = v
        key, why = _old_key(vaults, bid, secrets.get(bid))
        if key:
            doc["key"] = key
            notes.append("its key, under this controller's master key")
        elif why:
            notes.append(f"no key copied: {why}; set it under Backends")
        if raw.get("updated_by") == "env":
            var = config_mod.env_name(bid)
            notes.append(f"the engine's environment declared it (TANDEM_BACKENDS, TANDEM_BACKEND_{var}_*): "
                         f"move those lines to this controller's env file, or keep the row")
            report["env_vars"].append(f"TANDEM_BACKEND_{var}_*")
            if secrets.get(bid) is None:
                notes.append(f"a key it had from TANDEM_BACKEND_{var}_KEY is not in the file: set it here")
        if disabled and raw.get("enabled"):
            notes.append("stored off (--disabled): turn it on under Backends at the switch")
        try:
            cfg.create_backend(doc, who=WHO)
        except config_mod.ConfigError as exc:
            report["backends"].append({"id": bid, "state": "refused", "notes": [exc.message]})
            continue
        done.append(bid)
        report["backends"].append({"id": bid, "state": "imported", "notes": notes})
    c = cfg._conn
    with cfg._lock:
        mcols = [x for x in _cols(o, "models") if x in set(_cols(c, "models"))]
        for bid in done:
            for r in _rows(o, f"SELECT {', '.join(mcols)} FROM models WHERE backend_id = ?", (bid,)):
                c.execute(f"INSERT OR IGNORE INTO models({', '.join(mcols)}) VALUES "
                          f"({', '.join('?' for _ in mcols)})", tuple(r[k] for k in mcols))
                report["models"] += 1
            for r in _rows(o, "SELECT backend_id, model_id, feature, value FROM model_features WHERE backend_id = ?",
                           (bid,)):
                c.execute("INSERT OR IGNORE INTO model_features VALUES (?, ?, ?, ?)",
                          (r["backend_id"], r["model_id"], r["feature"], r["value"]))
                report["features"] += 1
            cfg._audit(WHO, "import", "models", bid, ["models", "model_features"])
        c.commit()
        cfg._bump()
    cur = cfg.rules()
    seen = {(r["pattern"], r["backend"]) for r in cur}
    new = [{"pattern": r["pattern"], "backend": r["backend"], "enabled": r["enabled"]}
           for r in old.rules() if r["backend"] in done and (r["pattern"], r["backend"]) not in seen]
    if new:
        keep = [{"id": r["id"], "pattern": r["pattern"], "backend": r["backend"], "enabled": r["enabled"]}
                for r in cur]
        cfg.set_rules(keep + new, who=WHO)
    report["rules"] = len(new)
    report["users"] = len(old.users())
    return report


def lines(report: dict, *, src: str, dst: str, write: bool) -> list[str]:
    out = [f"from {src} (read from a copy; the file is left as it is)",
           f"into {dst}" + ("" if write else " (a copy of it: nothing is written without --write)")]
    for b in report["backends"]:
        verb = {"imported": "import" if not write else "imported", "kept": "kept", "refused": "REFUSED"}[b["state"]]
        out.append(f"  {verb:<9} {b['id']}" + (": " + "; ".join(b["notes"]) if b["notes"] else ""))
    if not report["backends"]:
        out.append("  no backends in that file")
    out.append(f"  models {report['models']}, conductor switches {report['features']}, routing rules "
               f"{report['rules']}")
    if report["users"]:
        out.append(f"  users {report['users']}: not copied. Their keys stay in that file; set "
                   f"TANDEM_API_USERS_FROM={src} in this controller's env file to check them there, and "
                   f"send the clients' OpenAI routes through the controller (docs/proxy.md, the data plane)")
    if write and any(b["state"] == "imported" for b in report["backends"]):
        out.append("restart the controller so it serves them (its service unit, or tandem-app serve again)")
    if not write:
        out.append("nothing written; run again with --write")
    return out


@contextlib.contextmanager
def _snapshot(path: str, env):
    """A ConfigDB on a copy of `path` (SQLite's backup API) and of the key file beside it, removed
    afterwards; a fresh one when there is no file. Nothing is written beside `path`: a file with a
    -wal beside it is read read-only (its writer keeps the -shm and -wal), and one without is read
    as immutable, which creates no -shm or -wal of its own."""
    real = os.path.realpath(os.path.expanduser(path))
    d = tempfile.mkdtemp(prefix="tandem-app-import-")
    try:
        tmp = os.path.join(d, "config.sqlite3")
        if os.path.exists(real):
            how = "mode=ro" if os.path.exists(real + "-wal") else "immutable=1"
            src = sqlite3.connect(f"file:{urllib.parse.quote(real)}?{how}", uri=True)
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
            finally:
                src.close()
                dst.close()
        key = os.path.join(os.path.dirname(real), vault_mod.KEY_FILE)
        if os.path.exists(key):
            shutil.copy2(key, os.path.join(d, vault_mod.KEY_FILE))
        cfg = config_mod.ConfigDB(tmp, env=env, use_keychain=False).open()
        try:
            yield cfg
        finally:
            cfg.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main(args) -> int:
    """`tandem-app import-backends`: the exit code. 0: done (or the dry run); 1: refused."""
    from proxy.cli import _say, _tilde
    src = os.path.realpath(os.path.expanduser(args.from_db))
    dst = os.path.realpath(os.path.expanduser(args.config_db))
    if not os.path.isfile(src):
        _say(f"FAIL no file {_tilde(src)}: --from takes the engine's config database (its QSE_BACKENDS)")
        return 1
    if src == dst:
        _say(f"FAIL {_tilde(src)} is this controller's own config database: nothing to import")
        return 1
    try:
        with _snapshot(src, {}) as old:
            if not old.schema_was:
                _say(f"FAIL {_tilde(src)} is not a TandemApp config database")
                return 1
            if args.write:
                cfg = config_mod.ConfigDB(dst, env=os.environ, use_keychain=not args.no_keychain).open()
                try:
                    rep = plan_and_apply(old, cfg, src=src, env=os.environ, disabled=args.disabled)
                finally:
                    cfg.close()
            else:
                with _snapshot(dst, os.environ) as cfg:
                    rep = plan_and_apply(old, cfg, src=src, env=os.environ, disabled=args.disabled)
    except (config_mod.ConfigError, vault_mod.SecretError) as exc:
        _say(f"FAIL {getattr(exc, 'message', exc)}")
        return 1
    except sqlite3.DatabaseError as exc:
        _say(f"FAIL {_tilde(src)} does not open as a config database ({exc})")
        return 1
    for line in lines(rep, src=_tilde(src), dst=_tilde(dst), write=args.write):
        _say(line)
    return 1 if any(b["state"] == "refused" for b in rep["backends"]) else 0
