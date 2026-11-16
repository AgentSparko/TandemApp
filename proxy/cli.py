"""The `tandem-app` commands other than `serve` (proxy/__main__.py): check, secrets, backends,
doctor.

Each returns the process's exit code. `check` and `backends list` read a copy of the config
database (SQLite's backup API), so they never write to the real one; `secrets init` is the one
command that writes, and only a missing key file.
"""

from __future__ import annotations

import contextlib
import ipaddress
import os
import shutil
import sqlite3
import sys
import tempfile

from proxy import config as config_mod
from proxy import secrets as vault_mod


def _say(*parts) -> None:
    print(*parts, flush=True)


@contextlib.contextmanager
def _copy_of(path: str, env):
    """A ConfigDB opened on a copy of `path` (or on a fresh file when there is none), with the
    environment applied. The copy and its directory are removed afterwards."""
    real = os.path.realpath(os.path.expanduser(path))
    d = tempfile.mkdtemp(prefix="tandem-app-")
    try:
        tmp = os.path.join(d, "config.sqlite3")
        if os.path.exists(real):
            src = sqlite3.connect(f"file:{real}?mode=ro", uri=True)
            dst = sqlite3.connect(tmp)
            src.backup(dst)
            src.close()
            dst.close()
        key = os.path.join(os.path.dirname(real), vault_mod.KEY_FILE)
        if os.path.exists(key):
            shutil.copy2(key, os.path.join(d, vault_mod.KEY_FILE))
        cfg = config_mod.ConfigDB(tmp, env=env, use_keychain=False).open()
        try:
            yield cfg, os.path.exists(real)
        finally:
            cfg.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _tilde(p: str) -> str:
    home = os.path.expanduser("~")
    return "~" + p[len(home):] if home != "/" and p.startswith(home) else p


def _has(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except Exception:                                                # noqa: BLE001
        return False


def _backend_lines(cfg) -> list[str]:
    out = []
    for b in cfg.backends():
        hint = cfg.key_hint(b["id"])
        key = (f"key …{hint['last4'] or '????'} ({hint['source']})" if hint["key_set"]
               else "no key")
        locks = cfg.backend_locks(b["id"])
        host = b.get("host") or {}
        out.append(f"  {b['id']:<16} {b['kind']:<10} {b['url']:<40} "
                   f"{'on ' if b['enabled'] else 'off'} {key}; host {host.get('mode', 'auto')}"
                   f"{' ' + host['url'] if host.get('url') else ''}"
                   f"{'; set by the environment: ' + ', '.join(sorted(locks)) if locks else ''}")
    return out


def check(args) -> int:
    env = os.environ
    from proxy import _version
    _say(f"{_version.DIST} {_version.release() or 'unknown'}, python {sys.version.split()[0]}")
    try:
        config_mod.parse_env(env)
    except config_mod.ConfigError as exc:
        _say(f"FAIL the environment: {exc.message}")
        return 1
    try:
        with _copy_of(args.config_db, env) as (cfg, existed):
            _say(f"config  {_tilde(os.path.realpath(os.path.expanduser(args.config_db)))}"
                 f"{'' if existed else ' (does not exist yet: serve creates it)'}")
            ov = cfg.apply_env()
            real = config_mod.ConfigDB(args.config_db, env=env)
            src = real.key_source()
            _say(f"keys    master key from {src or 'a new key file at the first stored key'}")
            for b in cfg.backends():
                hint = cfg.key_hint(b["id"])
                if hint["key_set"] and hint["source"] == "database" and src is None:
                    _say(f"WARN    {b['id']}: a key is stored but no master key answers: "
                         f"enter it again or restore secrets.key")
            bl = _backend_lines(cfg)
            _say(f"backends {len(bl)}" + ("" if bl else ": none yet, the page opens on Setup"))
            for line in bl:
                _say(line)
            rules = cfg.rules()
            for r in rules:
                _say(f"  rule {r['pattern']} -> {r['backend']}"
                     f"{' (' + r['variable'] + ')' if r.get('variable') else ''}")
            users = cfg.users()
            _say(f"users   {len(users)} (keys from the environment: {len(ov['users'])})")
    except config_mod.ConfigError as exc:
        _say(f"FAIL the configuration: {exc.message}")
        return 1
    from tandemapp import static
    dist = args.dashboard_dir or static.default_dir()
    page = static.bundle_info(dist)
    if not page["present"]:
        state = (" MISSING: the page shows a placeholder; build it with "
                 "`python3 tools/dashboard_bundle.py build` in TandemApp's folder, or fetch a "
                 "released bundle (docs/proxy.md)")
    elif page["verified"]:
        state = f" bundle {page['sha256'][:16]} ({page['source'] or 'unrecorded'})"
    else:
        state = f" bundle {page['sha256'][:16]} MODIFIED: the files differ from its bundle.json"
    _say(f"page    {_tilde(dist)}{state}")
    led = args.usage_ledger
    _say(f"ledger  {_tilde(os.path.expanduser(led)) if led != 'off' else 'off'}")
    _say(f"extras  cryptography {'yes' if _has('cryptography') else 'NO (keys cannot be stored)'}"
         f", psutil {'yes' if _has('psutil') else 'no'}, nvml {'yes' if _has('pynvml') else 'no'}"
         f" (the host view falls back to /proc and nvidia-smi)")
    try:
        loop = ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        loop = args.host == "localhost"
    # OPS-298, SEC-28: the sign-in (proxy/authlocal.py): what the health page would say
    from proxy import authlegacy, authlocal, userscli
    owner = userscli.owner_ready(os.path.realpath(os.path.expanduser(args.config_db)))
    legacy = authlegacy.admin_token_set(env)
    local = loop and not owner and not legacy and authlocal.deploy_kind(env) == "bare" and \
        (env.get(authlocal.LOCAL_ENV) or "").strip().lower() != "off"
    _say(f"listen  {args.host}:{args.port}, sign-in: "
         + ("an owner with a password" if owner else
            "local mode, this machine only, no password yet" if local else "no owner yet"))
    if not owner and not local:
        _say("WARN    no owner yet (auth_setup_pending): nobody signs in until one is made "
             "with a setup code; `tandem-app users setup-code` prints one")
    if not _has("cryptography"):
        return 1
    _say("OK")
    return 0


def secrets(args) -> int:
    real = os.path.realpath(os.path.expanduser(args.config_db))
    d = os.path.dirname(real)
    cfg = config_mod.ConfigDB(args.config_db, env=os.environ)
    src = cfg.key_source()
    if args.action == "status":
        _say(f"master key: {src or 'none yet'} ({_tilde(d)})")
        return 0
    path = os.path.join(d, vault_mod.KEY_FILE)
    if src is not None:
        _say(f"a master key answers already: {src}"
             f"{' at ' + _tilde(path) if src == 'file' else ''}; nothing written")
        return 0
    try:
        v = vault_mod.Vault.load(d, env=os.environ, use_keychain=not args.no_keychain)
    except vault_mod.SecretError as exc:
        _say(f"FAIL {exc}")
        return 1
    _say(f"master key: {v.source}" + (f", written to {_tilde(path)} (0600)" if v.source == "file"
                                      else ""))
    return 0


def backends(args) -> int:
    try:
        with _copy_of(args.config_db, os.environ) as (cfg, existed):
            cfg.apply_env()
            lines = _backend_lines(cfg)
    except config_mod.ConfigError as exc:
        _say(f"FAIL {exc.message}")
        return 1
    if not lines:
        _say("no backends")
    for line in lines:
        _say(line.strip())
    return 0


def doctor(args) -> int:
    """SRV-869: the running controller's /health at --host and --port, as this machine's CLI sees
    it. The user who owns the state dir reads <state>/run/cli.token (svc:cli, minted at each
    start, the file 0600) and gets the full health; anyone else gets the open answer, the status
    only. Exit 0 when it says ok."""
    import json
    import urllib.error
    import urllib.request
    from proxy import tokens as tokens_mod
    state = os.path.dirname(os.path.realpath(os.path.expanduser(args.config_db)))
    token = tokens_mod.read_cli_token(state)
    host = "127.0.0.1" if args.host in ("0.0.0.0", "") else "::1" if args.host == "::" \
        else args.host
    base = f"http://[{host}]:{args.port}" if ":" in host else f"http://{host}:{args.port}"
    req = urllib.request.Request(base + "/health")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            doc = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        _say(f"{base}/health answered HTTP {exc.code}")
        return 1
    except (OSError, ValueError) as exc:
        _say(f"{base}/health does not answer: {type(exc).__name__}")
        return 1
    _say(f"health: {base}/health "
         f"({'full, with ' + _tilde(tokens_mod.cli_token_path(state)) if token else 'open: the status only; the full answer needs the CLI token, which this user cannot read'})")
    _say(json.dumps(doc, indent=1, sort_keys=True))
    return 0 if doc.get("status") == "ok" else 1
