"""Sign a TandemApp plugin folder with an ed25519 key, and make or show such a key (SEC-18).

    python tools/plugin_sign.py keygen KEYFILE           a new private key in KEYFILE (PEM, 0600;
                                                          refused when it exists); prints the
                                                          public key and its fingerprint
    python tools/plugin_sign.py pubkey KEYFILE           the public key and its fingerprint
    python tools/plugin_sign.py sign --key KEYFILE FOLDER [--manifest-dir DIR]
                                                          writes FOLDER/plugin.sig.json
    python tools/plugin_sign.py verify FOLDER [--manifest-dir DIR] [--fingerprint FP]
                                                          checks it (and that FP signed it)

A passphrase-protected key reads its passphrase from the environment variable named by
--passphrase-env (keygen writes one encrypted with it). The private key is never printed or
logged: what this prints is the public key (base64 of its 32 bytes, what an admin adds on
/v1/fleet/plugin-trust/keys) and its fingerprint (the sha256 of those bytes in hex, what the admin
types back to confirm it).

What is signed (docs/plugins.md, proxy/pluginmanifest.py THE SIGNATURE): five lines naming the
format, the plugin, its version, the tree's sha256 (every file's sha256 folded in) and the sha256
of plugin.json. Sign last: any change to a file after signing breaks the signature, and the
controller refuses the install. Nothing in the folder runs.

Exit 0 on success, 1 when the folder is refused or the signature does not hold, 2 for a usage
error.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from proxy import pluginmanifest as PM  # noqa: E402


def _crypto():
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError:
        print("the cryptography package is missing (pip install cryptography)", file=sys.stderr)
        raise SystemExit(2)
    return serialization, Ed25519PrivateKey


def _passphrase(name: str | None) -> bytes | None:
    if not name:
        return None
    v = os.environ.get(name)
    if not v:
        print(f"--passphrase-env {name}: the variable is not set", file=sys.stderr)
        raise SystemExit(2)
    return v.encode()


def _public(key) -> tuple[str, str]:
    serialization, _ = _crypto()
    raw = key.public_key().public_bytes(serialization.Encoding.Raw,
                                        serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode(), hashlib.sha256(raw).hexdigest()


def load_key(path: str, passphrase: bytes | None):
    serialization, Ed25519PrivateKey = _crypto()
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as exc:
        print(f"{path}: {exc.strerror}", file=sys.stderr)
        raise SystemExit(2)
    try:
        key = serialization.load_pem_private_key(data, password=passphrase)
    except (TypeError, ValueError):
        # never echo the file: say what is wrong, not what it holds
        print(f"{path}: not an ed25519 private key in PEM, or the passphrase is wrong",
              file=sys.stderr)
        raise SystemExit(2)
    if not isinstance(key, Ed25519PrivateKey):
        print(f"{path}: not an ed25519 key", file=sys.stderr)
        raise SystemExit(2)
    return key


def _print_public(pub: str, fp: str) -> None:
    print(f"public_key {pub}")
    print(f"fingerprint sha256:{fp}")


def keygen(path: str, passphrase: bytes | None) -> int:
    serialization, Ed25519PrivateKey = _crypto()
    key = Ed25519PrivateKey.generate()
    enc = (serialization.BestAvailableEncryption(passphrase) if passphrase else
           serialization.NoEncryption())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, enc)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(f"{path} exists: keygen never overwrites a key", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"{path}: {exc.strerror}", file=sys.stderr)
        return 2
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    _print_public(*_public(key))
    return 0


def statement_of(folder: str, manifest_dir: str) -> tuple[dict, bytes]:
    """(the facts signed, the statement) of a folder; raises SystemExit(1) when it would be
    refused for something other than its signature."""
    rep = PM.inspect(folder, manifest_dir=manifest_dir)
    other = [e for e in rep["errors"] if e["code"] != "signature_invalid"]
    if other or not isinstance(rep["manifest"], dict):
        for e in other or [{"code": "manifest_missing", "message": "no manifest"}]:
            print(f"{e['code']}: {e['message']}" + (f" [{e['field']}]" if e.get("field") else ""),
                  file=sys.stderr)
        raise SystemExit(1)
    m = rep["manifest"]
    facts = {"plugin": m["name"], "version": m["version"], "tree_sha256": rep["sha256"],
             "manifest_sha256": PM.file_sha256(PM.manifest_path(folder, manifest_dir))}
    return facts, PM.signature_statement(**facts)


def sign_with(key, folder: str, manifest_dir: str = "") -> dict:
    """Write FOLDER/plugin.sig.json signed by `key` (an Ed25519PrivateKey); the facts signed.
    Raises SystemExit(1) when the folder would be refused for another reason."""
    facts, msg = statement_of(folder, manifest_dir)
    pub, _ = _public(key)
    doc = {"format": PM.SIG_FORMAT, "algorithm": "ed25519", "public_key": pub, **facts,
           "signature": base64.b64encode(key.sign(msg)).decode()}
    path = os.path.join(folder, PM.SIGNATURE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)
    return facts


def sign(folder: str, key_path: str, manifest_dir: str, passphrase: bytes | None) -> int:
    key = load_key(key_path, passphrase)
    facts = sign_with(key, folder, manifest_dir)
    pub, fp = _public(key)
    sig = PM.check_signature(folder, facts["tree_sha256"], manifest_dir)
    if not sig["verified"]:                                        # pragma: no cover
        print(f"the signature written does not hold: {sig['error']}", file=sys.stderr)
        return 1
    print(f"signed {facts['plugin']} {facts['version']} tree {facts['tree_sha256']}")
    _print_public(pub, fp)
    return 0


def verify(folder: str, manifest_dir: str, want_fp: str | None) -> int:
    rep = PM.inspect(folder, manifest_dir=manifest_dir)
    sig = rep["signature"] or {}
    if not sig.get("present"):
        print(f"not signed: no {PM.SIGNATURE}", file=sys.stderr)
        return 1
    if not sig.get("verified"):
        print(f"signature_invalid: {sig.get('error')}", file=sys.stderr)
        return 1
    if want_fp:
        from proxy import plugintrust
        if plugintrust.norm_fingerprint(want_fp) != sig["fingerprint"]:
            print(f"signed by sha256:{sig['fingerprint']}, not by the key given",
                  file=sys.stderr)
            return 1
    print(f"ok {sig['plugin']} {sig['version']} signed by key {sig['key_id']} "
          f"(sha256:{sig['fingerprint']})")
    for e in rep["errors"]:
        print(f"but refused: {e['code']}: {e['message']}", file=sys.stderr)
    return 0 if rep["ok"] else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen", help="make a new private key")
    k.add_argument("keyfile")
    k.add_argument("--passphrase-env", help="encrypt it with this variable's value")
    p = sub.add_parser("pubkey", help="print a key's public half and fingerprint")
    p.add_argument("keyfile")
    p.add_argument("--passphrase-env")
    s = sub.add_parser("sign", help="sign a plugin folder")
    s.add_argument("folder")
    s.add_argument("--key", required=True)
    s.add_argument("--manifest-dir", default="")
    s.add_argument("--passphrase-env")
    v = sub.add_parser("verify", help="check a plugin folder's signature")
    v.add_argument("folder")
    v.add_argument("--manifest-dir", default="")
    v.add_argument("--fingerprint", help="the signer's fingerprint the signature must carry")
    a = ap.parse_args(argv)
    if a.cmd == "keygen":
        return keygen(a.keyfile, _passphrase(a.passphrase_env))
    if a.cmd == "pubkey":
        _print_public(*_public(load_key(a.keyfile, _passphrase(a.passphrase_env))))
        return 0
    if not os.path.isdir(a.folder):
        print(f"no folder {a.folder}", file=sys.stderr)
        return 2
    folder = os.path.abspath(a.folder)
    if a.cmd == "sign":
        return sign(folder, a.key, a.manifest_dir, _passphrase(a.passphrase_env))
    return verify(folder, a.manifest_dir, a.fingerprint)


if __name__ == "__main__":
    sys.exit(main())
