"""The way from the old sign-in to the accounts (SRV-865), with TANDEM_AUTH=accounts only.

    migrate(a, state_dir) -> dict        at each start, before the first request; a second run
                                         changes nothing
    scan(env, files) -> (env, findings)  the legacy variables: what is set, what does not read

THE OWNER. admin.json with a password becomes the account `owner` (role owner) with the same hash
and parameters, so the password keeps working; it is the first owner (`auth_state.first_owner`).
Nothing is imported once an owner with a password exists. With no admin.json password and the
admin token set, no account is made: the token is accepted once as the setup code at POST
/v1/auth/setup (proxy/authlocal.py).

THE KEYS (SRV-867, proxy/tokens.py import_legacy): the rows of `users`, TANDEM_API_KEYS and
TANDEM_API_USERS_FROM become API tokens with the same hash, under review; the admin token, once
an owner exists, a 30-day management token of the first owner. After it none of them is read to
let a request in.

THE WAY BACK. The config database keeps its schema number and its tables; admin.json is never
removed. Each change of the first owner's password (the page, the setup, `tandem-app password
set`, a reset's one-time code) rewrites admin.json in the old format, so the release before
signs in with the password of now.

THE LEGACY VARIABLES never stop the start. Each one that is set is a health finding naming it and
the file it is in: `legacy_env_set` when it reads, `legacy_env_invalid` when it does not (it is
then ignored: an admin or metrics token under 32 characters, a TANDEM_API_KEYS entry that is not
label:key with 16 characters or more, a dashboard login that is not on or off; the valid
TANDEM_API_KEYS entries stay), `legacy_users_from_unreadable` for a TANDEM_API_USERS_FROM file
that cannot be read. `--dashboard-login` and `--trust-loopback` are accepted and ignored.
"""

from __future__ import annotations

import json
import os
import secrets

from proxy import adminpw

TOKENS = ("TANDEM_ADMIN_TOKEN", "QSE_ADMIN_TOKEN", "QSE_METRICS_TOKEN")
LOGINS = ("TANDEM_DASHBOARD_LOGIN", "QSE_DASHBOARD_LOGIN")
KEYS = "TANDEM_API_KEYS"
USERS_FROM = "TANDEM_API_USERS_FROM"
VARIABLES = TOKENS + (KEYS, USERS_FROM) + LOGINS
OLD_FIELDS = ("v", "kdf", "n", "r", "p", "rounds", "salt", "hash", "set_at")
ENVIRONMENT = "the environment"


def env_file_vars(path: str | None) -> set:
    """The names an env file sets (proxy/__main__.py load_env_file's format); empty without one."""
    out: set = set()
    if not path:
        return out
    try:
        with open(os.path.expanduser(path)) as f:
            for line in f:
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:].lstrip()
                k, sep, _ = line.partition("=")
                if sep and k.strip() and not line.startswith("#"):
                    out.add(k.strip())
    except OSError:
        pass
    return out


def scan(env, files: dict | None = None) -> tuple[dict, list[dict]]:
    """(the environment without what does not read, the findings). `files`: {variable: file}."""
    files = files or {}
    out = dict(env)
    found: list[dict] = []

    def note(code: str, var: str) -> None:
        found.append({"code": code, "variable": var, "file": files.get(var) or ENVIRONMENT})

    for var in TOKENS:
        v = env.get(var)
        if v is None or v == "":
            continue
        if len(v.strip()) < 32:
            note("legacy_env_invalid", var)
            out.pop(var, None)
        else:
            note("legacy_env_set", var)
    v = env.get(KEYS)
    if v:
        good, bad = [], 0
        for item in (x.strip() for x in v.split(",")):
            if not item:
                continue
            label, sep, key = item.partition(":")
            if sep and label.strip() and len(key.strip()) >= 16:
                good.append(item)
            else:
                bad += 1
        note("legacy_env_invalid" if bad else "legacy_env_set", KEYS)
        if good:
            out[KEYS] = ",".join(good)
        else:
            out.pop(KEYS, None)
    v = (env.get(USERS_FROM) or "").strip()
    if v:
        path = os.path.realpath(os.path.expanduser(v))
        if os.path.isfile(path) and os.access(path, os.R_OK):
            note("legacy_env_set", USERS_FROM)
        else:
            note("legacy_users_from_unreadable", USERS_FROM)
    for var in LOGINS:
        v = env.get(var)
        if v is None or v == "":
            continue
        if v.strip().lower() in ("on", "off"):
            note("legacy_env_set", var)
        else:
            note("legacy_env_invalid", var)
            out.pop(var, None)
    return out, found


def old_doc(doc: dict) -> dict:
    """An account's hash as admin.json holds it (proxy/adminpw.py's file)."""
    return {k: doc[k] for k in OLD_FIELDS if k in doc}


def follow(store, path: str):
    """AccountStore.on_password: the first owner's new hash goes to admin.json, old format."""
    def on_password(account_id: str, doc: dict) -> None:
        if store.state("first_owner") != account_id or not isinstance(doc, dict):
            return
        adminpw.Password(path)._write(old_doc(doc))
    return on_password


def _free_name(store, want: str = "owner") -> str:
    if store.by_username(want) is None:
        return want
    while True:
        name = f"{want}-{secrets.token_hex(2)}"
        if store.by_username(name) is None:
            return name


def migrate(a, state_dir: str) -> dict:
    """The owner from admin.json, or the admin token as the setup code. Returns what it did."""
    st = a.store
    path = os.path.join(state_dir, "admin.json")
    st.on_password = follow(st, path)
    did = {"owner": None, "token_code": False}
    pw = adminpw.Password(path)
    doc = pw._live()
    usable = isinstance(doc, dict) and not doc.get("broken") and doc.get("hash") and \
        doc.get("salt") and doc.get("kdf") in ("scrypt", "pbkdf2-sha256")
    if usable and st.setup_required():
        name = _free_name(st)
        aid = secrets.token_hex(6)
        with st.cfg._lock:
            if st.setup_required():
                st._c.execute("INSERT INTO accounts(id, provider, subject, username, display_name, "
                              "role, enabled, pw, must_change, created_at) VALUES (?, 'local', ?, "
                              "?, NULL, 'owner', 1, ?, 0, ?)",
                              (aid, aid, name, json.dumps(old_doc(doc)), st.now_ms()))
                st._c.execute("INSERT OR REPLACE INTO auth_state(key, value) VALUES "
                              "('first_owner', ?)", (json.dumps(aid),))
                st.cfg._audit("migration", "create", "account", aid,
                              detail={"username": name, "role": "owner", "from": "admin.json"})
                st._c.commit()
                did["owner"] = name
    token = (a.env.get("TANDEM_ADMIN_TOKEN") or a.env.get("QSE_ADMIN_TOKEN") or "").strip()
    if len(token) >= 32 and st.setup_required():
        a.codes.add_secret(token, "admin_token")
        did["token_code"] = True
    # SRV-867: the legacy keys and the admin token as API tokens
    did["tokens"] = st.tokens.import_legacy(a.env, keys_env=dict(st.cfg.env_users),
                                            users_from=getattr(st.cfg, "users_from", None),
                                            first_owner=st.first_owner())
    return did
