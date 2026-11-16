"""The plugin registry (SRV-857, plugin-v1): what is installed, from where, its tree's sha256, whether
it is enabled, the capabilities granted and its settings. Two tables in the config database and one
folder a plugin version under the controller's state dir.

    plugins          one row a plugin: id (= the manifest's name), name, version, kind, title,
                     source (JSON: folder path, wheel path and the archive's sha256, or git URL,
                     commit and subdir), sha256 (the tree's, proxy/pluginmanifest.py THE DIGEST),
                     signature (JSON), manifest (JSON), manifest_dir, path (the installed tree),
                     enabled, trust (requested), trust_granted (sandboxed, or host once the
                     admin granted it: SRV-859), granted (JSON:
                     capability -> {by, at, confirmed_by, confirmed_at}), settings (JSON: the set
                     values; a key not here holds its schema's default), integrity (ok, mismatch,
                     missing) and when it was checked, installed and updated (when, by whom),
                     row_version
    plugin_secrets   a plugin's secrets as AES-256-GCM ciphertext under the master key
                     (proxy/secrets.py), `plugin-secret:<id>:<name>` the associated data, with the
                     last 4 characters and a sha256 prefix for the page; write-only

    <state dir>/plugins/<id>/<version>/     the tree as installed (read-only to the controller)
    <state dir>/plugins/.staging/<random>/  a source while it is fetched and checked

NOTHING RUNS. An install copies and reads files; no entry command, setup script or hook of the
source runs (F2 starts the plugin, from `load`).

AN INSTALL is all or nothing: the source is staged, checked (proxy/pluginmanifest.py inspect),
moved into its version folder and only then written to the table, under the config database's lock.
An install cut short leaves a staging folder or a version folder no row names: the first use of the
registry in a process removes both. Refused: a failed check (422 plugin_refused with every error),
a name that is installed (409 exists: install again with `upgrade`), the same version with another
tree (409 version_unchanged), an older version (409 downgrade, unless `allow_downgrade`).

AN UPGRADE keeps what the new manifest allows: a setting whose key is still a property and whose
value holds against the new schema, a secret whose name is still declared, a capability still
requested; the rest is dropped and listed in the answer (`kept`, `dropped`). It keeps enabled as it
was. The old version's folder is removed after the row is written.

ENABLE checks the tree again (its sha256 against the row's) and the compatibility with this
TandemApp; DISABLE always works. The plugin host (proxy/pluginhost.py, SRV-859) listens to every
change (`listeners`) and stops a plugin before its removal (`before_remove`). REMOVE is refused while a hook names the plugin (kind plugin or
mcp, target `<id>` or `<id>/<hook>`): 409 referenced with the hooks. `load(id)` is what the plugin
host (F2) calls before it starts a plugin: enabled, the tree checked, the settings with their
defaults, the granted capabilities; secrets only through `secret_for`.

CAPABILITIES are deny by default: the manifest requests, the settings interface grants
(proxy/pluginsettings.py: pairing's rights, and a second confirm for prompts:full, engine:control
and a network host outside the LAN). Granted is always a subset of requested. The plugin host
enforces them (SRV-859); signatures and trusted keys are F3's.

TRUST. A manifest asks `sandboxed` (the default), `host` or `engine`. `host` runs only once the
admin granted it (`trust_granted`, the plugins scope's `trust` key: pairing's rights and the second
confirm); `engine` is refused until the in-process chain exists. An upgrade keeps a host grant
while the new manifest still asks host.

Every write is audited (field names and capability names, never a value or a secret).
"""

from __future__ import annotations

import json
import os
import secrets as _rand
import shutil
import time

from proxy import pluginmanifest as PM
from proxy import pluginsource
from proxy import schemaform
from proxy import secrets as vault_mod

DDL = """
CREATE TABLE IF NOT EXISTS plugins (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, version TEXT NOT NULL, kind TEXT NOT NULL,
  title TEXT, source TEXT NOT NULL, sha256 TEXT NOT NULL, signature TEXT, manifest TEXT NOT NULL,
  manifest_dir TEXT NOT NULL DEFAULT '', path TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
  trust TEXT NOT NULL DEFAULT 'sandboxed', granted TEXT NOT NULL DEFAULT '{}',
  settings TEXT NOT NULL DEFAULT '{}', integrity TEXT NOT NULL DEFAULT 'ok', checked_at INTEGER,
  installed_at INTEGER NOT NULL, installed_by TEXT, updated_at INTEGER NOT NULL, updated_by TEXT,
  row_version INTEGER NOT NULL DEFAULT 1, trust_granted TEXT NOT NULL DEFAULT 'sandboxed');
CREATE TABLE IF NOT EXISTS plugin_secrets (
  plugin_id TEXT NOT NULL, name TEXT NOT NULL, ciphertext BLOB NOT NULL, nonce BLOB NOT NULL,
  master_key_id TEXT NOT NULL, last4 TEXT, sha256_6 TEXT, set_at INTEGER NOT NULL, set_by TEXT,
  PRIMARY KEY (plugin_id, name));
"""
SECRET_MAX = 8192
_COLS = ("id", "name", "version", "kind", "title", "source", "sha256", "signature", "manifest",
         "manifest_dir", "path", "enabled", "trust", "granted", "settings", "integrity",
         "checked_at", "installed_at", "installed_by", "updated_at", "updated_by", "row_version",
         "trust_granted")
TRUST_GRANTS = ("sandboxed", "host")


class PluginError(Exception):
    def __init__(self, status: int, code: str, message: str, field: str | None = None,
                 extra: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.field = status, code, message, field
        self.extra = dict(extra or {})

    def body(self) -> dict:
        err = {"type": self.code, "message": self.message}
        if self.field:
            err["field"] = self.field
        err.update(self.extra)
        return {"error": err}


def _now_ms() -> int:
    return int(time.time() * 1000)


class PluginStore:
    """The registry over a `proxy.config.ConfigDB` (its connection, lock, audit and vault).
    `hooks`: the hook registry (proxy/hookstore.py), for the references a removal checks.
    `root`: the plugins' folder (default: `plugins` beside the config database)."""

    def __init__(self, cfg, hooks=None, root: str | None = None, *, tandemapp: str | None = None,
                 hooks_api: str | None = None, log=None):
        self.cfg = cfg
        self.hooks = hooks
        self._root = root
        self.tandemapp = tandemapp            # None: this TandemApp's (tests give their own)
        self.hooks_api = hooks_api
        self.log = log or (lambda m: None)
        self.gen = 0
        self._ready = False
        self.listeners: list = []           # fn(plugin id or None), after every change
        self.before_remove = None           # fn(plugin id): the plugin host stops it first

    def _changed(self, pid: str | None) -> None:
        self.gen += 1
        for fn in list(self.listeners):
            try:
                fn(pid)
            except Exception as exc:                               # noqa: BLE001
                self.log(f"plugins: a listener failed: {type(exc).__name__}")

    # ------------------------------------------------------------------ places
    def root(self) -> str:
        if self._root is None:
            self._root = os.path.join(os.path.dirname(os.path.abspath(self.cfg.path)), "plugins")
        return self._root

    def _staging(self) -> str:
        return os.path.join(self.root(), ".staging")

    def version_dir(self, pid: str, version: str) -> str:
        return os.path.join(self.root(), pid, version)

    def _versions(self) -> tuple[str, str]:
        return (self.tandemapp or PM.this_tandemapp(), self.hooks_api or PM.this_hooks_api())

    # ------------------------------------------------------------------ lifecycle
    def ready(self) -> bool:
        if self._ready:
            return True
        conn = getattr(self.cfg, "_conn", None)
        if conn is None:
            return False
        with self.cfg._lock:
            if not self._ready:
                conn.executescript(DDL)
                cols = {r[1] for r in conn.execute("PRAGMA table_info(plugins)").fetchall()}
                if "trust_granted" not in cols:              # a table made before SRV-859
                    conn.execute("ALTER TABLE plugins ADD COLUMN trust_granted TEXT NOT NULL "
                                 "DEFAULT 'sandboxed'")
                conn.commit()
                self._ready = True
        self._sweep()
        return True

    def _c(self):
        if not self.ready():
            raise PluginError(503, "unavailable", "the config database is not open")
        return self.cfg._conn

    def _sweep(self) -> None:
        """Remove what an install cut short left: staging folders and version folders no row
        names."""
        root = self.root()
        if not os.path.isdir(root):
            return
        shutil.rmtree(self._staging(), ignore_errors=True)
        with self.cfg._lock:
            rows = {(r[0], r[1]) for r in
                    self.cfg._conn.execute("SELECT id, version FROM plugins").fetchall()}
        for pid in os.listdir(root):
            pdir = os.path.join(root, pid)
            if pid.startswith(".") or not os.path.isdir(pdir) or os.path.islink(pdir):
                continue
            for v in os.listdir(pdir):
                if (pid, v) not in rows:
                    shutil.rmtree(os.path.join(pdir, v), ignore_errors=True)
                    self.log(f"plugins: removed {pid}/{v}, left by an install cut short")
            try:
                os.rmdir(pdir)
            except OSError:
                pass

    # ------------------------------------------------------------------ reads
    def _row(self, r) -> dict:
        d = dict(zip(_COLS, r))
        for k, dflt in (("source", {}), ("signature", None), ("manifest", {}), ("granted", {}),
                        ("settings", {})):
            try:
                d[k] = json.loads(d[k]) if d[k] else dflt
            except ValueError:
                d[k] = dflt
        d["enabled"] = bool(d["enabled"])
        return d

    def list(self) -> list[dict]:
        with self.cfg._lock:
            rows = self._c().execute(f"SELECT {', '.join(_COLS)} FROM plugins ORDER BY id"
                                     ).fetchall()
        return [self._row(r) for r in rows]

    def get(self, pid: str) -> dict | None:
        with self.cfg._lock:
            r = self._c().execute(f"SELECT {', '.join(_COLS)} FROM plugins WHERE id = ?",
                                  (pid,)).fetchone()
        return self._row(r) if r else None

    def need(self, pid: str) -> dict:
        p = self.get(pid) if isinstance(pid, str) else None
        if p is None:
            raise PluginError(404, "not_found", f"no plugin {pid!r}")
        return p

    @staticmethod
    def requested(p: dict) -> list[str]:
        return PM.requested(p["manifest"])

    @staticmethod
    def settings_schema(p: dict) -> dict | None:
        s = p["manifest"].get("settings")
        return s if isinstance(s, dict) else None

    @staticmethod
    def secret_names(p: dict) -> list[str]:
        return [s["name"] for s in p["manifest"].get("secrets") or []]

    def effective_settings(self, p: dict) -> dict:
        sch = self.settings_schema(p)
        out = schemaform.defaults(sch) if sch else {}
        out.update(p["settings"])
        return out

    def references(self, pid: str) -> list[dict]:
        """The hooks that name the plugin: kind plugin or mcp, target `<id>` or `<id>/<hook>`."""
        if self.hooks is None or not self.hooks.ready():
            return []
        out = []
        for h in self.hooks.list():
            t = h.get("target") or ""
            if h["kind"] in ("plugin", "mcp") and (t == pid or t.startswith(pid + "/")):
                out.append({"id": h["id"], "name": h["name"], "enabled": h["enabled"]})
        return out

    # ------------------------------------------------------------------ inspect and install
    def _stage(self, source) -> tuple[pluginsource.Staged, dict]:
        os.makedirs(self._staging(), mode=0o700, exist_ok=True)
        into = os.path.join(self._staging(), _rand.token_hex(8))
        try:
            st = pluginsource.stage(source, into)
        except pluginsource.SourceError as exc:
            shutil.rmtree(into, ignore_errors=True)
            raise PluginError(exc.status, exc.code, exc.message, exc.field) from None
        except BaseException:
            shutil.rmtree(into, ignore_errors=True)
            raise
        ta, ha = self._versions()
        rep = PM.inspect(st.dir, manifest_dir=st.manifest_dir, tandemapp=ta, hooks_api=ha)
        return st, rep

    def inspect(self, source) -> dict:
        """The checks of a source without installing it: the report, the source as recorded, the
        settings' form; the staged tree is removed."""
        self.ready()
        st, rep = self._stage(source)
        try:
            return self._report(st, rep)
        finally:
            shutil.rmtree(st.dir, ignore_errors=True)

    def _report(self, st, rep: dict) -> dict:
        m = rep["manifest"] if isinstance(rep["manifest"], dict) else None
        installed = self.get(m["name"]) if m and isinstance(m.get("name"), str) else None
        sch = m.get("settings") if m else None
        return {"ok": rep["ok"], "source": st.record, "manifest": m, "errors": rep["errors"],
                "warnings": rep["warnings"] + [f"left as they are: {s}" for s in st.skipped],
                "sha256": rep["sha256"], "signature": rep["signature"], "files": rep["files"],
                "bytes": rep["bytes"], "manifest_dir": st.manifest_dir,
                "capabilities": {"requested": rep["capabilities"],
                                 "confirm": [c for c in rep["capabilities"]
                                             if _confirm(c)]},
                "compat": rep["compat"],
                "installed": None if installed is None else
                {"version": installed["version"], "sha256": installed["sha256"]},
                "settings_form": schemaform.describe(sch) if rep["ok"] and sch else None}

    def install(self, source, who: str = "gui", *, upgrade: bool = False,
                allow_downgrade: bool = False) -> tuple[dict, dict]:
        """(the plugin, what happened: {action: installed|upgraded|unchanged, from?, kept,
        dropped, warnings})."""
        self.ready()
        st, rep = self._stage(source)
        try:
            if not rep["ok"]:
                first = rep["errors"][0] if rep["errors"] else {"message": "refused"}
                raise PluginError(422, "plugin_refused", first["message"], first.get("field"),
                                  extra={"errors": rep["errors"], "warnings": rep["warnings"],
                                         "sha256": rep["sha256"]})
            m = rep["manifest"]
            pid = m["name"]
            old = self.get(pid)
            note = {"action": "installed", "kept": [], "dropped": [],
                    "warnings": rep["warnings"] + [f"left as they are: {s}" for s in st.skipped]}
            if old is not None:
                if old["version"] == m["version"]:
                    if old["sha256"] == rep["sha256"]:
                        note["action"] = "unchanged"
                        return old, note
                    raise PluginError(409, "version_unchanged", (
                        f"{pid} {m['version']} is installed with another tree (sha256 "
                        f"{old['sha256'][:12]}, this one {rep['sha256'][:12]}): a changed plugin "
                        f"carries a new version"), "version")
                if not upgrade:
                    raise PluginError(409, "exists", (
                        f"a plugin named {pid} is installed ({old['version']}): install with "
                        f"upgrade true to replace it with {m['version']}"), "name",
                        extra={"installed": old["version"], "version": m["version"]})
                newer = PM.parse_version(m["version"]) > PM.parse_version(old["version"])
                if not newer and not allow_downgrade:
                    raise PluginError(409, "downgrade", (
                        f"{m['version']} is older than the installed {old['version']}: install "
                        f"with allow_downgrade true to go back"), "version")
                note["action"] = "upgraded"
                note["from"] = old["version"]
            return self._commit(pid, st, rep, old, who, note), note
        finally:
            shutil.rmtree(st.dir, ignore_errors=True)

    def _carry(self, old: dict | None, m: dict, note: dict) -> tuple[dict, dict, list[str]]:
        """(settings, granted, secret names to drop) an upgrade keeps from `old`."""
        if old is None:
            return {}, {}, []
        sch = m.get("settings") if isinstance(m.get("settings"), dict) else None
        props = (sch or {}).get("properties") or {}
        settings = {}
        for k, v in old["settings"].items():
            if k in props and not schemaform.validate(v, props[k], root=sch, limit=1):
                settings[k] = v
                note["kept"].append(f"settings.{k}")
            else:
                note["dropped"].append(f"settings.{k}")
        req = set(PM.requested(m))
        granted = {}
        for c, g in old["granted"].items():
            if c in req:
                granted[c] = g
                note["kept"].append(f"capabilities.{c}")
            else:
                note["dropped"].append(f"capabilities.{c}")
        if old.get("trust_granted") == "host":
            if m.get("trust") == "host":
                note["kept"].append("trust")
            else:
                note["dropped"].append("trust")
        names = {s["name"] for s in m.get("secrets") or []}
        drop = []
        for n in self._secret_rows(old["id"]):
            if n in names:
                note["kept"].append(f"secrets.{n}")
            else:
                drop.append(n)
                note["dropped"].append(f"secrets.{n}")
        return settings, granted, drop

    def _commit(self, pid: str, st, rep: dict, old: dict | None, who: str, note: dict) -> dict:
        m = rep["manifest"]
        dst = self.version_dir(pid, m["version"])
        os.makedirs(os.path.dirname(dst), mode=0o755, exist_ok=True)
        if os.path.exists(dst):
            shutil.rmtree(dst)              # left by an install cut short (no row names it)
        os.rename(st.dir, dst)
        settings, granted, drop = self._carry(old, m, note)
        now = _now_ms()
        c = self._c()
        try:
            with self.cfg._lock:
                cur = self.get(pid)
                if (cur is None) != (old is None) or (cur and cur["row_version"] !=
                                                      old["row_version"]):
                    raise PluginError(409, "conflict", f"{pid} changed meanwhile; try again")
                vals = (m["version"], m.get("kind", "plugin"), m.get("title"),
                        json.dumps(st.record, sort_keys=True), rep["sha256"],
                        json.dumps(rep["signature"], sort_keys=True),
                        json.dumps(m, sort_keys=True), st.manifest_dir, dst,
                        m.get("trust", "sandboxed"), json.dumps(granted, sort_keys=True),
                        json.dumps(settings, sort_keys=True))
                tgrant = "host" if old is not None and old.get("trust_granted") == "host" \
                    and m.get("trust") == "host" else "sandboxed"
                if old is None:
                    c.execute("INSERT INTO plugins(id, name, version, kind, title, source, sha256, "
                              "signature, manifest, manifest_dir, path, trust, granted, settings, "
                              "enabled, integrity, checked_at, installed_at, installed_by, "
                              "updated_at, updated_by, row_version) VALUES (?, ?, ?, ?, ?, ?, ?, "
                              "?, ?, ?, ?, ?, ?, ?, 0, 'ok', ?, ?, ?, ?, ?, 1)",
                              (pid, m["name"], *vals, now, now, who, now, who))
                    self.cfg._audit(who, "install", "plugin", pid, ["version", "sha256"],
                                    detail={"version": m["version"], "source": st.record["type"],
                                            "sha256": rep["sha256"][:16]})
                else:
                    c.execute("UPDATE plugins SET version = ?, kind = ?, title = ?, source = ?, "
                              "sha256 = ?, signature = ?, manifest = ?, manifest_dir = ?, "
                              "path = ?, trust = ?, granted = ?, settings = ?, integrity = 'ok', "
                              "checked_at = ?, updated_at = ?, updated_by = ?, trust_granted = ?, "
                              "row_version = row_version + 1 WHERE id = ?",
                              (*vals, now, now, who, tgrant, pid))
                    for n in drop:
                        c.execute("DELETE FROM plugin_secrets WHERE plugin_id = ? AND name = ?",
                                  (pid, n))
                    self.cfg._audit(who, "upgrade", "plugin", pid, ["version", "sha256"],
                                    detail={"from": old["version"], "version": m["version"],
                                            "source": st.record["type"],
                                            "sha256": rep["sha256"][:16],
                                            "dropped": note["dropped"][:50]})
                c.commit()
                self._changed(pid)
        except BaseException:
            c.rollback()
            shutil.rmtree(dst, ignore_errors=True)
            raise
        if old is not None and old["path"] != dst:
            shutil.rmtree(old["path"], ignore_errors=True)
        return self.get(pid)

    # ------------------------------------------------------------------ integrity
    def verify(self, pid: str, record: bool = True) -> dict:
        """Check the tree against the row's sha256 now: {integrity, sha256, expected}. `record`:
        write the result to the row (a plan does not)."""
        p = self.need(pid)
        if not os.path.isdir(p["path"]):
            got, state = None, "missing"
        else:
            try:
                got = PM.tree_digest(p["path"])
                state = "ok" if got == p["sha256"] else "mismatch"
            except PM.TreeError:
                got, state = None, "mismatch"
        if not record:
            return {"integrity": state, "sha256": got, "expected": p["sha256"]}
        with self.cfg._lock:
            self._c().execute("UPDATE plugins SET integrity = ?, checked_at = ? WHERE id = ?",
                              (state, _now_ms(), pid))
            if state != p["integrity"]:
                self.cfg._audit(None, "verify", "plugin", pid, ["integrity"],
                                detail={"integrity": state})
            self._c().commit()
        if state != "ok":
            self.log(f"plugins: {pid} {p['version']}: its tree is {state} (sha256 "
                     f"{p['sha256'][:12]} recorded)")
        return {"integrity": state, "sha256": got, "expected": p["sha256"]}

    def load(self, pid: str) -> dict:
        """What the plugin host (F2) starts a plugin from; refuses one that is disabled, whose
        tree changed or that no longer fits this TandemApp."""
        p = self.need(pid)
        if not p["enabled"]:
            raise PluginError(409, "disabled", f"{pid} is disabled")
        v = self.verify(pid)
        if v["integrity"] != "ok":
            raise PluginError(409, "integrity", f"{pid}: its tree is {v['integrity']} since the "
                                                f"install; install it again")
        self._compatible(p)
        return {"id": pid, "version": p["version"], "kind": p["kind"], "path": p["path"],
                "manifest_dir": p["manifest_dir"], "manifest": p["manifest"],
                "settings": self.effective_settings(p), "granted": sorted(p["granted"]),
                "trust": p["trust"], "trust_granted": p["trust_granted"] or "sandboxed",
                "row_version": p["row_version"], "secrets": sorted(self._secret_rows(pid))}

    def _compatible(self, p: dict) -> None:
        ta, ha = self._versions()
        errs = PM.compat(p["manifest"], tandemapp=ta, hooks_api=ha)
        if errs:
            raise PluginError(409, errs[0]["code"], errs[0]["message"], errs[0]["field"],
                              extra={"needed": errs[0].get("needed"),
                                     "have": errs[0].get("have")})

    # ------------------------------------------------------------------ switches and removal
    def enable_check(self, pid: str, record: bool = True) -> None:
        """Raise PluginError when the plugin may not be enabled now."""
        p = self.need(pid)
        v = self.verify(pid, record=record)
        if v["integrity"] != "ok":
            raise PluginError(409, "integrity", f"{pid}: its tree is {v['integrity']} since the "
                                                f"install (sha256 {p['sha256'][:12]} recorded); "
                                                f"install it again", "enabled")
        self._compatible(p)

    def set_enabled(self, pid: str, on: bool, who: str = "gui") -> dict:
        if on:
            self.enable_check(pid)
        p = self.need(pid)
        if p["enabled"] == bool(on):
            return p
        with self.cfg._lock:
            self._c().execute("UPDATE plugins SET enabled = ?, updated_at = ?, updated_by = ?, "
                              "row_version = row_version + 1 WHERE id = ?",
                              (int(bool(on)), _now_ms(), who, pid))
            self.cfg._audit(who, "enable" if on else "disable", "plugin", pid, ["enabled"])
            self._c().commit()
        self._changed(pid)
        return self.need(pid)

    def remove(self, pid: str, who: str = "gui") -> dict:
        p = self.need(pid)
        refs = self.references(pid)
        if refs:
            raise PluginError(409, "referenced", (
                f"{pid} is named by {len(refs)} hook(s): {', '.join(r['id'] for r in refs[:8])}. "
                f"Remove those hooks first"), extra={"hooks": refs})
        if self.before_remove is not None:
            self.before_remove(pid)              # its process ends before its folder goes
        with self.cfg._lock:
            c = self._c()
            c.execute("DELETE FROM plugins WHERE id = ?", (pid,))
            c.execute("DELETE FROM plugin_secrets WHERE plugin_id = ?", (pid,))
            self.cfg._audit(who, "remove", "plugin", pid, ["version"],
                            detail={"version": p["version"]})
            c.commit()
        shutil.rmtree(os.path.join(self.root(), pid), ignore_errors=True)
        self._changed(pid)
        return {"removed": pid, "version": p["version"]}

    # ------------------------------------------------------------------ secrets
    def _secret_rows(self, pid: str) -> dict:
        with self.cfg._lock:
            rows = self._c().execute("SELECT name, last4, sha256_6, set_at, set_by FROM "
                                     "plugin_secrets WHERE plugin_id = ?", (pid,)).fetchall()
        return {r[0]: {"last4": r[1], "sha256_6": r[2], "set_at": r[3], "set_by": r[4]}
                for r in rows}

    def secret_hints(self, pid: str) -> dict:
        """{name: {set, last4, sha256_6, set_at}} for every declared secret."""
        p = self.need(pid)
        rows = self._secret_rows(pid)
        out = {}
        for n in self.secret_names(p):
            r = rows.get(n)
            out[n] = {"set": r is not None, "last4": r["last4"] if r else None,
                      "sha256_6": r["sha256_6"] if r else None,
                      "set_at": r["set_at"] if r else None}
        return out

    def secret_for(self, pid: str, name: str) -> str | None:
        """A secret in plain text, for the plugin host (F2) and nothing else."""
        with self.cfg._lock:
            r = self._c().execute("SELECT ciphertext, nonce, master_key_id FROM plugin_secrets "
                                  "WHERE plugin_id = ? AND name = ?", (pid, name)).fetchone()
        if r is None:
            return None
        return self.cfg.vault().decrypt(f"plugin-secret:{pid}:{name}", bytes(r[0]), bytes(r[1]),
                                        r[2])

    # ------------------------------------------------------------------ a settings change
    def write(self, pid: str, *, enabled=None, settings_set=None, settings_unset=(),
              secrets_set=None, secrets_unset=(), grant=(), revoke=(), confirmed=(),
              trust: str | None = None, who: str = "gui") -> dict:
        """One change of the plugins scope (proxy/pluginsettings.py), in one transaction; the
        scope has checked the values. `confirmed`: the grants whose second confirm was given."""
        p = self.need(pid)
        if enabled:
            self.enable_check(pid)
        settings = dict(p["settings"])
        for k, v in (settings_set or {}).items():
            settings[k] = v
        for k in settings_unset:
            settings.pop(k, None)
        granted = dict(p["granted"])
        now = _now_ms()
        req = set(self.requested(p))
        for cap in grant:
            if cap not in req:
                raise PluginError(422, "not_requested", f"{pid} does not request {cap}",
                                  f"capabilities.{cap}")
            if cap in granted:
                continue
            if _confirm(cap) and cap not in confirmed:
                raise PluginError(409, "confirm_needed", f"{cap} takes the second confirm",
                                  f"capabilities.{cap}")
            granted[cap] = {"by": who, "at": now,
                            "confirmed_by": who if cap in confirmed else None,
                            "confirmed_at": now if cap in confirmed else None}
        for cap in revoke:
            granted.pop(cap, None)
        tgrant = p["trust_granted"] or "sandboxed"
        if trust is not None:
            if trust not in TRUST_GRANTS:
                raise PluginError(422, "bad_value", "trust: sandboxed or host", "trust")
            if trust == "host" and p["trust"] != "host":
                raise PluginError(422, "not_requested", f"{pid} does not ask to run as the "
                                                        f"app's user (trust host)", "trust")
            if trust == "host" and tgrant != "host" and "trust" not in confirmed:
                raise PluginError(409, "confirm_needed", "trust host takes the second confirm",
                                  "trust")
            tgrant = trust
        names = set(self.secret_names(p))
        vault = None
        with self.cfg._lock:
            c = self._c()
            if self.need(pid)["row_version"] != p["row_version"]:
                raise PluginError(409, "conflict", f"{pid} changed meanwhile; read it again")
            fields = []
            for n, v in (secrets_set or {}).items():
                if n not in names or not isinstance(v, str) or not 1 <= len(v) <= SECRET_MAX:
                    raise PluginError(422, "bad_value", f"secrets.{n}: a string of 1 to "
                                                        f"{SECRET_MAX} characters",
                                      f"secrets.{n}")
                vault = vault or self.cfg.vault()
                ct, nonce, kid = vault.encrypt(f"plugin-secret:{pid}:{n}", v)
                h = vault_mod.hint(v)
                c.execute("INSERT OR REPLACE INTO plugin_secrets(plugin_id, name, ciphertext, "
                          "nonce, master_key_id, last4, sha256_6, set_at, set_by) VALUES "
                          "(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (pid, n, ct, nonce, kid, h["last4"], h["sha256_6"], now, who))
                fields.append(f"secrets.{n}")
            for n in secrets_unset:
                c.execute("DELETE FROM plugin_secrets WHERE plugin_id = ? AND name = ?", (pid, n))
                fields.append(f"secrets.{n}")
            on = p["enabled"] if enabled is None else bool(enabled)
            c.execute("UPDATE plugins SET enabled = ?, settings = ?, granted = ?, updated_at = ?, "
                      "updated_by = ?, trust_granted = ?, row_version = row_version + 1 "
                      "WHERE id = ?",
                      (int(on), json.dumps(settings, sort_keys=True),
                       json.dumps(granted, sort_keys=True), now, who, tgrant, pid))
            fields += [f"settings.{k}" for k in sorted(set(settings_set or {}) |
                                                        set(settings_unset))]
            if enabled is not None and on != p["enabled"]:
                fields.append("enabled")
            added = sorted(set(granted) - set(p["granted"]))
            gone = sorted(set(p["granted"]) - set(granted))
            fields += [f"capabilities.{x}" for x in added + gone]
            if tgrant != (p["trust_granted"] or "sandboxed"):
                fields.append("trust")
                added = added + ([f"trust:{tgrant}"] if tgrant == "host" else [])
                gone = gone + (["trust:host"] if tgrant != "host" else [])
            self.cfg._audit(who, "settings", "plugin", pid, fields or ["settings"],
                            detail={"granted": added, "revoked": gone,
                                    "confirmed": sorted(x for x in added if x in confirmed or
                                                        (x == "trust:host" and
                                                         "trust" in confirmed))}
                            if added or gone else None)
            c.commit()
        self._changed(pid)
        return self.need(pid)


def _confirm(cap: str) -> bool:
    try:
        return PM.needs_confirm(cap)
    except Exception:                                              # noqa: BLE001
        return True

