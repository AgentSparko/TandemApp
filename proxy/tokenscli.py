"""`tandem-app tokens ...` (SRV-867): the API tokens on this machine, without a password.

    tandem-app tokens list [NAME]                 every token, or one account's
    tandem-app tokens create NAME --label L --scope S [--scope S ...] [--model GLOB ...]
                                  [--days N|never]
    tandem-app tokens revoke ID
    tandem-app tokens rotate ID [--grace SECONDS]
    tandem-app tokens confirm ID                  an imported key, kept (it leaves Review)
    tandem-app tokens chatd-key FILE              SRV-869: a fresh secret of chatd's identity
                                                  svc:chatd into FILE (0600), for a chatd this
                                                  controller does not start (QSE_CHAT_ENGINE_KEY,
                                                  its engine.key); the one before stops
        [--state-dir D] [--config-db F]

It works on the config database as `tandem-app users` does (proxy/userscli.py): whoever can read
the state directory is the owner already. A token is made for the account NAME and never has more
than that account's role allows; it is printed once. A running TandemApp sees a revocation at its
next request.
"""

from __future__ import annotations

import argparse
import os
import sys

ACTIONS = ("list", "create", "revoke", "rotate", "confirm", "chatd-key")


def _say(*parts) -> None:
    print(*parts, flush=True)


def run(a) -> int:
    from proxy import accounts as acc_mod
    from proxy import tokens as tokens_mod
    from proxy import userscli
    path = userscli._db_path(a)
    if not os.path.exists(path):
        _say(f"no config database at {path}: start TandemApp once, or give --config-db")
        return 1
    cfg, store, _codes = userscli.open_store(path)
    ts = store.tokens
    who = "cli"
    try:
        if a.action == "list":
            if a.target:
                rows = ts.mine(userscli._find(store, a.target)["id"])
            else:
                rows = ts.all()
            if not rows:
                _say("no tokens")
            for r in rows:
                v = ts.view(r)
                _say(f"{v['id']}  {v['label'][:24]:<24} {v['owner'] or '-':<16} {v['state']:<8} "
                     f"{','.join(v['scopes'])}  expires {v['expires_at'] or 'never'}"
                     + ("  REVIEW" if v["needs_review"] else ""))
            return 0
        if not a.target:
            _say(f"tandem-app tokens {a.action} needs "
                 f"{'a NAME' if a.action == 'create' else 'a FILE' if a.action == 'chatd-key' else 'an ID'}")
            return 2
        if a.action == "create":
            owner = userscli._find(store, a.target)
            days = a.days
            if days not in (None, "never"):
                try:
                    days = int(days)
                except ValueError:
                    _say("--days is a number or never")
                    return 2
            view, token = ts.create(owner, label=a.label, scopes=a.scope or [],
                                    models=a.model or None, days=days, actor=who)
            _say(f"token {view['label']!r} of {view['owner']} ({', '.join(view['scopes'])}), "
                 f"expires {view['expires_at'] or 'never'}:")
            _say(token)
            _say("This token is shown once. Copy it now.")
            return 0
        if a.action == "revoke":
            v = ts.revoke(a.target, actor=who)
            _say(f"{v['id']} ({v['label']}): revoked")
            return 0
        if a.action == "rotate":
            view, token = ts.rotate(a.target, grace_s=a.grace, actor=who)
            _say(f"{view['id']} ({view['label']}): a new secret; the old one works {a.grace} s "
                 f"more:")
            _say(token)
            _say("This token is shown once. Copy it now.")
            return 0
        if a.action == "confirm":
            v = ts.confirm(a.target, actor=who)
            _say(f"{v['id']} ({v['label']}): confirmed")
            return 0
        if a.action == "chatd-key":
            out = os.path.expanduser(a.target)
            token = ts.mint_service(tokens_mod.CHATD_SERVICE, tokens_mod.CHATD_SCOPES,
                                    memory_user=tokens_mod.CHATD_LABEL, actor=who)
            tmp = f"{out}.tmp-{os.getpid()}"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.fchmod(fd, 0o600)
                os.write(fd, (token + "\n").encode())
            finally:
                os.close(fd)
            os.replace(tmp, out)
            _say(f"{tokens_mod.CHATD_SERVICE}: a fresh secret in {out} (0600); the one before "
                 f"stopped. Give that chatd QSE_CHAT_ENGINE_KEY from it.")
            return 0
    except (acc_mod.AccountError, tokens_mod.TokenError) as exc:
        _say(exc.message)
        return 1
    finally:
        ts.flush()
        cfg.close()
    return 2


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="tandem-app tokens",
                                 description="The API tokens on this machine (no password asked).")
    ap.add_argument("action", choices=ACTIONS)
    ap.add_argument("target", nargs="?", default=None, metavar="NAME|ID|FILE")
    ap.add_argument("--label", default=None)
    ap.add_argument("--scope", action="append", default=None)
    ap.add_argument("--model", action="append", default=None)
    ap.add_argument("--days", default=None, help="a number of days, or never (an admin's)")
    ap.add_argument("--grace", type=int, default=0, help="for rotate: seconds the old secret "
                                                         "still works")
    ap.add_argument("--state-dir", default=os.environ.get("TANDEM_APP_STATE") or "~/.tandemllm")
    ap.add_argument("--config-db", default=None)
    return ap


def main(argv: list[str]) -> int:
    return run(parser().parse_args(argv))


if __name__ == "__main__":                                        # pragma: no cover
    sys.exit(main(sys.argv[1:]))
