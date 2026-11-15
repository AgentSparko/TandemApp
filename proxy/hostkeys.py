"""This controller's SSH key pair per engine host (SRV-839): made here, the private half sealed.

    keys = HostKeys(cfg)                       cfg: the config database (proxy/config.py)
    keys.create(host_id)                       {host_id, key_id, public, fingerprint}; a new pair
    keys.get(host_id)                          the same without making one; None when there is none
    keys.private(host_id)                      the OpenSSH private key, for the SSH adapter only
    keys.authorized_line(host_id, engine_dir, from_=None)   the engine host's authorized_keys line
    keys.delete(host_id)                       the pair and the host's HTTP token
    keys.put_http_token(host_id, token)        the engine's HTTP token (`tandem remote token
    keys.http_token_hint(host_id)              rotate`), sealed; its hint, never the token

ONE PAIR PER ENGINE HOST, ed25519, made with `cryptography` (already TandemApp's dependency): no
ssh-keygen run, no key file written to make it. The private key (OpenSSH format, no passphrase) is
stored in the config database's `engine_host_keys` as AES-256-GCM ciphertext under the master key
(proxy/secrets.py), with `engine-host:<host id>` as the associated data: a ciphertext copied onto
another host's row, or a backend key's row, does not decrypt. A lost master key means pairing the
host again; nothing else is lost.

THE KEY ID is `tandemapp-` and 10 hex digits of the public key's sha256: the engine's `--key-id`
for this key (contracts/engine-ctl-v1 verbs.json `key_line`), so `tandem remote unpair` removes
this key's line and no other, and the key's comment.

THE LINE for the paste-a-key path (the engine's docs/remote-control.md):

    command="/bin/bash DIR/bin/tandem remote --key-id ID",no-pty,no-port-forwarding,
    no-agent-forwarding,no-X11-forwarding[,from="..."] ssh-ed25519 AAAA... ID

on one line. DIR is the engine's install on that host (an absolute path of plain characters: no
space, quote, backslash, `$` or comma), `from` a list of address patterns.

THE HTTP TOKEN (SRV-839, engine API 1.9): the bearer the engine wants from callers that are not on
its own host. `tandem remote token rotate` answers it on the SSH channel once (enginectl
`token_rotate`), and it goes straight to `put_http_token`: sealed in the config database's
`engine_http_tokens` with `engine-http:<host id>` as associated data. The data plane reads it
through ConfigDB.key_for for a TandemLLM backend on that host; nothing else does.

WHAT LEAVES THIS MODULE: the public key, its id and fingerprint, the token's hint. `private` is for
proxy/enginectl.py's key file; no log line, answer, audit row or fleet record holds it
(tests/test_enginectl.py greps every output for it).
"""

from __future__ import annotations

import base64
import hashlib
import re

from proxy import enginectl

DIR_RE = re.compile(r"^/[A-Za-z0-9._/+@-]{0,1023}$")
FROM_RE = re.compile(r"^!?[A-Za-z0-9.:*?/%_-]{1,253}$")
KEY_LINE_OPTS = "no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding"


class HostKeyError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


def generate() -> tuple[str, str]:
    """(private key in OpenSSH PEM, public `ssh-ed25519 AAAA...`) of a new ed25519 pair."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    k = Ed25519PrivateKey.generate()
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                          serialization.NoEncryption()).decode()
    pub = k.public_key().public_bytes(serialization.Encoding.OpenSSH,
                                      serialization.PublicFormat.OpenSSH).decode()
    return pem, pub


def key_id_of(public: str) -> str:
    blob = base64.b64decode(public.split()[1])
    return "tandemapp-" + hashlib.sha256(blob).hexdigest()[:10]


def fingerprint_of(public: str) -> str:
    return enginectl.sshonce.fingerprint(public.split()[1])


def _host(host_id: str) -> str:
    if not isinstance(host_id, str) or not enginectl.HOST_ID_RE.match(host_id):
        raise HostKeyError("an engine host id is letters, digits, . _ - (64 at most)")
    return host_id


def key_line(public: str, key_id: str, engine_dir: str, from_=None) -> str:
    """The authorized_keys line that lets `public` run `tandem remote` of `engine_dir` only."""
    if not isinstance(engine_dir, str) or not DIR_RE.match(engine_dir) or "/../" in engine_dir + "/":
        raise HostKeyError("the engine's directory is an absolute path of letters, digits and "
                           ". _ / + @ - (no space, quote, backslash, $ or comma)")
    if not enginectl.KEY_ID_RE.match(key_id or ""):
        raise HostKeyError("bad key id")
    parts = public.split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise HostKeyError("not an ed25519 public key")
    opts = f'command="/bin/bash {engine_dir.rstrip("/") or "/"}/bin/tandem remote --key-id {key_id}",' \
           f"{KEY_LINE_OPTS}"
    if from_:
        pats = [from_] if isinstance(from_, str) else list(from_)
        pats = [p.strip() for x in pats for p in str(x).split(",") if p.strip()]
        if not pats or any(not FROM_RE.match(p) for p in pats):
            raise HostKeyError("from is a list of addresses or patterns (192.0.2.0/24, *.lan)")
        opts += f',from="{",".join(pats)}"'
    return f"{opts} {parts[0]} {parts[1]} {key_id}"


class HostKeys:
    def __init__(self, cfg):
        self.cfg = cfg

    def get(self, host_id: str) -> dict | None:
        row = self.cfg.host_key(_host(host_id))
        if row is None:
            return None
        return {"host_id": host_id, "key_id": row["key_id"], "public": row["public"],
                "fingerprint": fingerprint_of(row["public"])}

    def create(self, host_id: str, *, replace: bool = False, who: str = "controller") -> dict:
        """A new key pair for `host_id`; the one it has unless `replace` (a new pairing)."""
        got = self.get(host_id)
        if got is not None and not replace:
            return got
        pem, pub = generate()
        kid = key_id_of(pub)
        self.cfg.put_host_key(host_id, kid, f"{pub} {kid}", pem, who=who)
        return self.get(host_id)

    def private(self, host_id: str) -> str:
        pem = self.cfg.host_key_private(_host(host_id))
        if pem is None:
            raise enginectl.CtlNotPaired(f"no key of this controller for {host_id}: pair it",
                                         code="no_key", host=host_id)
        return pem

    def authorized_line(self, host_id: str, engine_dir: str, from_=None) -> str:
        got = self.get(host_id)
        if got is None:
            raise HostKeyError(f"no key for {host_id} yet", status=404)
        return key_line(got["public"], got["key_id"], engine_dir, from_)

    def delete(self, host_id: str, who: str = "controller") -> bool:
        gone = self.cfg.delete_host_key(_host(host_id), who=who)
        self.cfg.delete_engine_token(host_id, who=who)
        return gone

    # ------------------------------------------------------------------ the HTTP token (SRV-839)
    def put_http_token(self, host_id: str, token: str, who: str = "controller") -> dict:
        """The engine host's HTTP token, sealed; its hint {set, last4, sha256_6, rotated_at}."""
        return self.cfg.put_engine_token(_host(host_id), token, who=who)

    def http_token_hint(self, host_id: str) -> dict:
        return self.cfg.engine_token_hint(_host(host_id))

    def rotate_http_token(self, ctl, host_id: str, who: str = "controller") -> dict:
        """`token rotate` on the engine host through `ctl` (enginectl), the new token stored here
        before anything else sees it; the answer without the token, with its hint."""
        return ctl.token_rotate(store=lambda tok: self.put_http_token(host_id, tok, who=who))
