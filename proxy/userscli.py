"""`tandem-app users ...` (SEC-26): the accounts on this machine, without a password.

    tandem-app users list
    tandem-app users add NAME --role viewer|operator|admin|owner [--display-name TEXT]
    tandem-app users disable NAME | enable NAME
    tandem-app users role NAME ROLE
    tandem-app users reset-password NAME
    tandem-app users remove NAME
    tandem-app users setup-code
        [--state-dir D] [--config-db F]

It works on the config database in the state directory (`--config-db`, else TANDEM_CONFIG_DB,
else <state dir>/config.sqlite3). Whoever can read that directory is the owner already, so it asks
for no password. A running TandemApp sees each change at its next request: an ended session or a
disabled account stops at once, with no restart.

`add` and `reset-password` print a one-time code of 20 characters, valid 24 hours: the user signs
in with it as the password and then sets one. A reset stops the old password at once and ends
every session of the account. `setup-code` prints a code for the first-run setup while no owner
exists (single use, 24 hours). The last enabled owner cannot be disabled, demoted or removed.

`tandem-app password set|remove|status` stays: once the accounts hold an owner, `set` sets the
first owner's password (admin.json follows it) and `remove` is `users reset-password` for that
owner; before that it is the controller's admin password as before (proxy/adminpw.py).
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

ACTIONS = ("list", "add", "disable", "enable", "role", "reset-password", "remove", "setup-code")


def _say(*parts) -> None:
    print(*parts, flush=True)


def _db_path(a) -> str:
    if a.config_db:
        return os.path.expanduser(a.config_db)
    if os.environ.get("TANDEM_CONFIG_DB"):
        return os.path.expanduser(os.environ["TANDEM_CONFIG_DB"])
    return os.path.join(os.path.expanduser(a.state_dir), "config.sqlite3")


def open_store(path: str):
    """(cfg, store, auth) over the config database at `path`; admin.json beside it follows the
    first owner's password."""
    from proxy import accounts as acc_mod
    from proxy import authlegacy, authlocal
    from proxy import config as config_mod
    cfg = config_mod.ConfigDB(path, env={}, use_keychain=False).open()
    store = acc_mod.AccountStore(cfg)
    store.on_password = authlegacy.follow(store, os.path.join(os.path.dirname(cfg.path),
                                                              "admin.json"))
    codes = authlocal.SetupCodes(store, dict(os.environ))
    return cfg, store, codes


def _find(store, name: str) -> dict:
    from proxy import accounts as acc_mod
    row = store.by_username(name)
    if row is None:
        raise acc_mod.AccountError(404, "not_found", f"no account {name!r}")
    return row


def _when(ms) -> str:
    if not ms:
        return "never"
    import time
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ms / 1000)) + "Z"


def run(a) -> int:
    from proxy import accounts as acc_mod
    from proxy import authz
    path = _db_path(a)
    if a.action != "setup-code" and not os.path.exists(path):
        _say(f"no config database at {path}: start TandemApp once, or give --config-db")
        return 1
    cfg, store, codes = open_store(path)
    who = "cli"
    try:
        if a.action == "list":
            rows = store.all()
            if not rows:
                _say("no accounts" + (" (the first-run setup is open)" if store.setup_required()
                                      else ""))
            for r in rows:
                state = "enabled" if r["enabled"] else "disabled"
                pw = ("one-time code" if r["must_change"] else "password") if r["pw"] else \
                    "no password"
                _say(f"{r['username']:<24} {r['role']:<9} {state:<9} {pw:<14} last sign-in "
                     f"{_when(r['last_login_at'])}")
            return 0
        if a.action == "setup-code":
            if not store.setup_required():
                _say("an owner exists already: the setup is closed. A lost password: "
                     "tandem-app users reset-password NAME")
                return 1
            code = codes.new("cli")
            _say(f"setup code: {code}")
            _say("single use, 5 tries, valid 24 hours; it opens only the first-run setup. Open the "
                 "page and enter it with the owner's name and password.")
            return 0
        if not a.name:
            _say(f"tandem-app users {a.action} needs a NAME")
            return 2
        if a.action == "add":
            if a.role not in authz.ROLES:
                _say(f"--role is one of {', '.join(authz.ROLES)}")
                return 2
            row = store.create(a.name, a.role, None, display_name=a.display_name, actor=who,
                               must_change=True)
            if a.role == "owner" and store.first_owner() is None:
                store.set_state("first_owner", row["id"])   # admin.json follows it
            code = store.set_code(row["id"], actor=who, action="create")
            _say(f"account {row['username']} ({row['role']}) made. One-time code: {code}")
            _say("valid 24 hours: sign in with it as the password, then set a password.")
            return 0
        row = _find(store, a.name)
        if a.action in ("disable", "enable"):
            store.update(row["id"], {"enabled": a.action == "enable"}, actor=who)
            _say(f"{row['username']}: {a.action}d" + ("; its sessions ended" if
                                                     a.action == "disable" else ""))
            return 0
        if a.action == "role":
            if a.role not in authz.ROLES:
                _say(f"the role is one of {', '.join(authz.ROLES)}")
                return 2
            _, ended = store.update(row["id"], {"role": a.role}, actor=who)
            _say(f"{row['username']}: {a.role}" + ("; its sessions ended" if ended else ""))
            return 0
        if a.action == "reset-password":
            code = store.set_code(row["id"], actor=who)
            _say(f"{row['username']}: the old password stopped and every session ended. One-time "
                 f"code: {code}")
            _say("valid 24 hours: sign in with it as the password, then set a password.")
            return 0
        if a.action == "remove":
            store.remove(row["id"], actor=who)
            _say(f"{row['username']}: removed; its sessions ended")
            return 0
    except acc_mod.AccountError as exc:
        _say(exc.message)
        return 1
    finally:
        cfg.close()
    return 2


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="tandem-app users",
                                 description="The accounts on this machine (no password asked).")
    ap.add_argument("action", choices=ACTIONS)
    ap.add_argument("name", nargs="?", default=None)
    ap.add_argument("role_arg", nargs="?", default=None, metavar="ROLE",
                    help="for `role NAME ROLE`")
    ap.add_argument("--role", default=None, help="for `add`: viewer, operator, admin or owner")
    ap.add_argument("--display-name", default=None)
    ap.add_argument("--state-dir", default=os.environ.get("TANDEM_APP_STATE") or "~/.tandemllm")
    ap.add_argument("--config-db", default=None)
    return ap


def main(argv: list[str]) -> int:
    a = parser().parse_args(argv)
    if a.action == "role":
        a.role = a.role_arg or a.role
    elif a.role_arg is not None:
        _say(f"tandem-app users {a.action} takes one NAME")
        return 2
    return run(a)


# ---------------------------------------------------------------------------- password alias
def _has_owner(path: str) -> bool:
    if not os.path.exists(path):
        return False
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return bool(c.execute("SELECT COUNT(*) FROM accounts WHERE role = 'owner'")
                        .fetchone()[0])
        finally:
            c.close()
    except sqlite3.Error:
        return False


def password_main(argv: list[str]) -> int:
    """`tandem-app password set|remove|status`: the first owner's once the accounts hold one."""
    from proxy import adminpw
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("action", nargs="?")
    pre.add_argument("--state-dir", default="~/.tandemllm")
    known, _ = pre.parse_known_args(argv)
    path = os.path.join(os.path.expanduser(known.state_dir), "config.sqlite3")
    if known.action not in ("set", "remove", "status") or not _has_owner(path):
        return adminpw.main(argv)
    import getpass
    from proxy import accounts as acc_mod
    cfg, store, _ = open_store(path)
    try:
        owner = store.first_owner() or next(
            (r for r in store.all() if r["role"] == "owner" and r["enabled"]), None)
        if owner is None:
            _say("no enabled owner: tandem-app users list")
            return 1
        if known.action == "status":
            _say(f"owner {owner['username']}: " + ("a password is set" if owner["pw"] else
                                                   "no password"))
            return 0
        if known.action == "remove":
            code = store.set_code(owner["id"], actor="cli")
            _say(f"{owner['username']}: the password stopped and every session ended. One-time "
                 f"code: {code} (valid 24 hours: sign in with it, then set a password)")
            return 0
        one = getpass.getpass(f"New password for {owner['username']}: ")
        if one != getpass.getpass("Again: "):
            _say("the two do not match; nothing changed")
            return 1
        store.set_password(owner["id"], one, actor="cli")
        store.revoke_account(owner["id"], actor="cli", why="password changed")
        _say(f"{owner['username']}: password set; every session signed in before ends")
        return 0
    except acc_mod.AccountError as exc:
        _say(exc.message)
        return 1
    finally:
        cfg.close()


if __name__ == "__main__":                                        # pragma: no cover
    sys.exit(main(sys.argv[1:]))
