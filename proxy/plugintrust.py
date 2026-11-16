"""Plugin signing and trust (SEC-18, plugin-v1): the publisher keys this controller trusts, the
signature check against them, and what each trust level needs before a plugin runs.

    keys = TrustedKeys(cfg)             the table plugin_keys in the config database
    keys.add(public_key, name, plugins=None, levels=None, confirm=..., who=...)
    keys.remove(key_id, confirm=..., who=...)
    keys.policy(), keys.set_policy(require_signature_sandboxed, who)
    judge(signature, plugin_id, level, keys) -> {trusted, key, why}

THE SIGNATURE (`plugin.sig.json` at the tree's root, proxy/pluginmanifest.py THE SIGNATURE): an
ed25519 signature over a statement that names the plugin, its version, the tree's sha256 (the
digest of every file's sha256) and the sha256 of its plugin.json. `tools/plugin_sign.py` writes it.
A signature that does not hold refuses the install; one that holds is TRUSTED only when its public
key is in this table and the key's limits (plugin ids, trust levels) allow this plugin at this
level. A signature by a key that is not here counts as no signature.

A KEY is added and removed by the admin (pairing's rights), each time with the key's fingerprint
typed or pasted back (`confirm_fingerprint`, the sha256 of the 32-byte public key in hex): the
admin compares it with the one the publisher published. A key can be limited to plugin ids and to
trust levels; to change its limits, remove it and add it again.

THE LEVELS (the manifest's `trust`):

    sandboxed   runs unsigned, unless the admin set "require signatures for sandboxed plugins"
                (`require_signature_sandboxed`, off by default): then only with a trusted signature
    host        an explicit grant: with a trusted signature for host the grant takes pairing's
                rights alone (basis `signature`); without one it takes the typed confirmation, the
                plugin's id typed (basis `typed`). A grant that rested on a signature holds only
                while that signature is trusted
    engine      a trusted signature for engine AND the typed confirmation, per engine host; the
                grant is recorded, the plugin still does not run until the in-process chain
                exists (engine-ext `ext install`)

Every decision is audited: a key added or removed, the policy set, a grant or revoke, and each
refusal (at install, enable or load) with its reason; never a key's private half (there is none
here) or a secret.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import time

from proxy.pluginstore import PluginError

LEVELS = ("sandboxed", "host", "engine")
POLICY_KEY = "plugins.require_signature_sandboxed"
ENGINE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,63}$")
PLUGIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
NAME_MAX = 80
LIMIT_MAX = 64

DDL = """
CREATE TABLE IF NOT EXISTS plugin_keys (
  id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, public_key TEXT NOT NULL,
  name TEXT NOT NULL, plugins TEXT, levels TEXT, added_at INTEGER NOT NULL, added_by TEXT);
"""
_COLS = ("id", "fingerprint", "public_key", "name", "plugins", "levels", "added_at", "added_by")


def fingerprint(pub: bytes) -> str:
    """A public key's fingerprint: the sha256 of its 32 bytes, in hex."""
    return hashlib.sha256(pub).hexdigest()


def key_id(fp: str) -> str:
    return fp[:16]


def norm_fingerprint(text) -> str | None:
    """A typed fingerprint as 64 lowercase hex digits: `sha256:` in front, colons and spaces
    between are allowed; None when it is not one."""
    if not isinstance(text, str):
        return None
    t = text.strip().lower()
    if t.startswith("sha256:"):
        t = t[7:]
    t = re.sub(r"[\s:]", "", t)
    return t if re.fullmatch(r"[0-9a-f]{64}", t) else None


def decode_key(text) -> bytes:
    """A public key given as base64 (or hex) of its 32 bytes; raises PluginError."""
    if isinstance(text, str):
        t = text.strip()
        try:
            raw = base64.b64decode(t, validate=True)
        except (binascii.Error, ValueError):
            raw = b""
        if len(raw) != 32 and re.fullmatch(r"[0-9a-fA-F]{64}", t):
            raw = bytes.fromhex(t)
        if len(raw) == 32:
            return raw
    raise PluginError(422, "bad_value", "public_key: an ed25519 public key, the base64 of its 32 "
                                        "bytes (tools/plugin_sign.py pubkey prints it)",
                      "public_key")


def _now_ms() -> int:
    return int(time.time() * 1000)


class TrustedKeys:
    """The publisher keys this controller trusts, and the signature policy, over a
    `proxy.config.ConfigDB`."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._ready = False
        self.gen = 0                        # moves at each change: the plugin host looks again

    def ready(self) -> bool:
        if self._ready:
            return True
        conn = getattr(self.cfg, "_conn", None)
        if conn is None:
            return False
        with self.cfg._lock:
            if not self._ready:
                conn.executescript(DDL)
                conn.commit()
                self._ready = True
        return True

    def _c(self):
        if not self.ready():
            raise PluginError(503, "unavailable", "the config database is not open")
        return self.cfg._conn

    @staticmethod
    def _row(r) -> dict:
        d = dict(zip(_COLS, r))
        for k in ("plugins", "levels"):
            d[k] = json.loads(d[k]) if d[k] else None
        return d

    def list(self) -> list[dict]:
        with self.cfg._lock:
            rows = self._c().execute(f"SELECT {', '.join(_COLS)} FROM plugin_keys ORDER BY name, "
                                     f"id").fetchall()
        return [self._row(r) for r in rows]

    def get(self, kid: str) -> dict | None:
        with self.cfg._lock:
            r = self._c().execute(f"SELECT {', '.join(_COLS)} FROM plugin_keys WHERE id = ?",
                                  (kid,)).fetchone()
        return self._row(r) if r else None

    def by_fingerprint(self, fp: str | None) -> dict | None:
        if not fp:
            return None
        with self.cfg._lock:
            r = self._c().execute(f"SELECT {', '.join(_COLS)} FROM plugin_keys WHERE "
                                  f"fingerprint = ?", (fp,)).fetchone()
        return self._row(r) if r else None

    # ------------------------------------------------------------------ writes
    @staticmethod
    def _confirm(fp: str, confirm) -> None:
        if confirm is None or confirm == "":
            raise PluginError(428, "fingerprint_confirm_required", (
                f"type or paste the key's fingerprint back (confirm_fingerprint) after comparing "
                f"it with the one its publisher published: sha256:{fp}"), "confirm_fingerprint",
                extra={"fingerprint": fp})
        if norm_fingerprint(confirm) != fp:
            raise PluginError(422, "fingerprint_mismatch", (
                "confirm_fingerprint is not this key's fingerprint: do not trust a key whose "
                "fingerprint differs from its publisher's"), "confirm_fingerprint",
                extra={"fingerprint": fp})

    @staticmethod
    def _limits(plugins, levels) -> tuple[list | None, list | None]:
        if plugins is not None:
            if not isinstance(plugins, list) or not 1 <= len(plugins) <= LIMIT_MAX or not all(
                    isinstance(x, str) and PLUGIN_RE.match(x) for x in plugins):
                raise PluginError(422, "bad_value", f"plugins: null (any plugin) or a list of 1 "
                                                    f"to {LIMIT_MAX} plugin ids", "plugins")
            plugins = sorted(set(plugins))
        if levels is not None:
            if not isinstance(levels, list) or not levels or not all(
                    x in LEVELS for x in levels):
                raise PluginError(422, "bad_value", "levels: null (any level) or a list of "
                                                    "sandboxed, host, engine", "levels")
            levels = [x for x in LEVELS if x in levels]
        return plugins, levels

    def add(self, public_key, name, plugins=None, levels=None, *, confirm=None,
            who: str = "gui") -> dict:
        pub = decode_key(public_key)
        fp = fingerprint(pub)
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= NAME_MAX:
            raise PluginError(422, "bad_value", f"name: the publisher's name, 1 to {NAME_MAX} "
                                                f"characters", "name")
        plugins, levels = self._limits(plugins, levels)
        if self.by_fingerprint(fp) is not None:
            raise PluginError(409, "key_exists", f"the key sha256:{fp} is trusted already (id "
                                                 f"{key_id(fp)}): remove it first to change its "
                                                 f"limits", "public_key")
        self._confirm(fp, confirm)
        now = _now_ms()
        with self.cfg._lock:
            c = self._c()
            c.execute("INSERT INTO plugin_keys(id, fingerprint, public_key, name, plugins, levels, "
                      "added_at, added_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (key_id(fp), fp, base64.b64encode(pub).decode(), name.strip(),
                       json.dumps(plugins) if plugins is not None else None,
                       json.dumps(levels) if levels is not None else None, now, who))
            self.cfg._audit(who, "trust_key", "plugin_key", key_id(fp),
                            ["fingerprint", "plugins", "levels"],
                            detail={"fingerprint": fp, "name": name.strip(),
                                    "plugins": plugins, "levels": levels})
            c.commit()
        self.gen += 1
        return self.get(key_id(fp))

    def remove(self, kid: str, *, confirm=None, who: str = "gui") -> dict:
        k = self.get(kid) if isinstance(kid, str) else None
        if k is None:
            raise PluginError(404, "not_found", f"no trusted key {kid!r}")
        self._confirm(k["fingerprint"], confirm)
        with self.cfg._lock:
            c = self._c()
            c.execute("DELETE FROM plugin_keys WHERE id = ?", (kid,))
            self.cfg._audit(who, "distrust_key", "plugin_key", kid, ["fingerprint"],
                            detail={"fingerprint": k["fingerprint"], "name": k["name"]})
            c.commit()
        self.gen += 1
        return {"removed": kid, "fingerprint": k["fingerprint"], "name": k["name"]}

    def policy(self) -> dict:
        return {"require_signature_sandboxed": bool(self.cfg.setting(POLICY_KEY, False))}

    def set_policy(self, require, who: str = "gui") -> dict:
        if not isinstance(require, bool):
            raise PluginError(422, "bad_value", "require_signature_sandboxed: true or false",
                              "require_signature_sandboxed")
        if require:
            self.cfg.set_setting(POLICY_KEY, True, who=who)
        else:
            self.cfg.delete_setting(POLICY_KEY, who=who)
        self.gen += 1
        return self.policy()


def view_key(k: dict) -> dict:
    return {"id": k["id"], "fingerprint": k["fingerprint"], "public_key": k["public_key"],
            "name": k["name"], "plugins": k["plugins"], "levels": k["levels"],
            "added_at": k["added_at"], "added_by": k["added_by"]}


def judge(sig: dict | None, pid: str, level: str, keys: TrustedKeys | None) -> dict:
    """Whether a checked signature (proxy/pluginmanifest.py check_signature) is trusted for the
    plugin `pid` at `level`: {trusted, key: {id, name} | None, why}."""
    if not sig or not sig.get("present"):
        return {"trusted": False, "key": None, "why": "it is not signed"}
    if not sig.get("verified"):
        return {"trusted": False, "key": None,
                "why": f"its signature does not hold ({sig.get('error') or 'not verified'})"}
    k = keys.by_fingerprint(sig.get("fingerprint")) if keys is not None and keys.ready() \
        else None
    if k is None:
        return {"trusted": False, "key": None,
                "why": f"it is signed by a key this TandemApp does not trust (id "
                       f"{sig.get('key_id')}, sha256:{sig.get('fingerprint')})"}
    who = {"id": k["id"], "name": k["name"]}
    if k["plugins"] is not None and pid not in k["plugins"]:
        return {"trusted": False, "key": who,
                "why": f"the key {k['name']} ({k['id']}) is trusted for "
                       f"{', '.join(k['plugins'])} only"}
    if k["levels"] is not None and level not in k["levels"]:
        return {"trusted": False, "key": who,
                "why": f"the key {k['name']} ({k['id']}) is trusted at "
                       f"{', '.join(k['levels'])} only, not {level}"}
    return {"trusted": True, "key": who, "why": f"signed by {k['name']} ({k['id']})"}


def check_typed(pid: str, confirm, level: str) -> None:
    """The typed confirmation: the plugin's id, typed. Raises PluginError."""
    if confirm is None or confirm == "":
        raise PluginError(428, "typed_confirm_required", (
            f"trust {level} for {pid} takes the typed confirmation: type the plugin's id "
            f"({pid}) as confirm"), "confirm", extra={"expect": "the plugin's id"})
    if not isinstance(confirm, str) or confirm.strip() != pid:
        raise PluginError(422, "typed_confirm_mismatch", (
            f"confirm is not the plugin's id: type {pid} exactly"), "confirm",
            extra={"expect": "the plugin's id"})


def check_engine(engine) -> str:
    if not isinstance(engine, str) or not ENGINE_RE.match(engine):
        raise PluginError(400, "bad_request", "engine: the engine host's id (the engine scope's "
                                              "target)", "engine")
    return engine


__all__ = ["TrustedKeys", "judge", "check_typed", "check_engine", "fingerprint", "key_id",
           "norm_fingerprint", "decode_key", "view_key", "LEVELS", "POLICY_KEY"]
