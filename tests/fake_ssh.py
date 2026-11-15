"""A fake OpenSSH client (`ssh`, `ssh-keyscan`) for tests: no network, no sshd. It does what the
engine host control adapter (proxy/enginectl.py) relies on, the way OpenSSH and sshd do it.

    bin = install(dir)            DIR/ssh and DIR/ssh-keyscan, to put first on PATH
    server(path, host_key=(type, b64), authorized_keys=..., unreachable=False, print_fp=True)

FAKE_SSH_SERVER names the server's JSON file, FAKE_SSH_LOG the file each run appends its argv to
(one JSON line: {tool, argv, mux, remote, env: the SSH_* names it was given}). A call:

- reads the options (-F -p -l -i -o -O -M -N -f -T, `--`, then the host and the command); the
  command is the remaining arguments joined by a space, as ssh sends it
- `-O check` / `-O exit`: the control socket is there / is removed
- the host key: the known_hosts file the options name, the line of HostKeyAlias; a missing line, a
  type HostKeyAlgorithms leaves out, or another key is refused with OpenSSH's own words (exit 255),
  the offered key's fingerprint printed when `print_fp`
- the key: the -i file must be 0600 and its public key a line of the server's authorized_keys;
  else "Permission denied (publickey)." (255)
- a password (SRV-831): with PreferredAuthentications password and PubkeyAuthentication no, ssh
  asks SSH_ASKPASS (SSH_ASKPASS_REQUIRE force) as OpenSSH does; what it prints must be the server's
  `password`, else "Permission denied" (255); the login then runs the command itself (no forced
  command: the user's shell), never logging what the program printed
- the server's `home` is HOME of the command it runs (the account's home, its ~/.ssh)
- `-M -N -f` binds the ControlPath socket and returns; a later call with ControlMaster=no and a
  socket there counts as multiplexed (`mux`); with the server's `dead_master` the socket is one
  whose master is gone (ssh's "Control socket connect(...)" line, then a direct connection)
- the command runs as sshd runs a forced command: the line's command="..." through /bin/sh -c with
  SSH_ORIGINAL_COMMAND and SSH_CONNECTION set; its output and exit status are the call's.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import subprocess
import sys

NOVAL = set("TMNfnqvCgAaXxYk46")
WITHVAL = set("FplioOJEcmeLRDWwbBIQS")


def install(d: str) -> str:
    os.makedirs(d, exist_ok=True)
    for tool in ("ssh", "ssh-keyscan"):
        p = os.path.join(d, tool)
        with open(p, "w") as f:
            f.write(f"#!/bin/sh\nexec {sys.executable} {os.path.abspath(__file__)} {tool} \"$@\"\n")
        os.chmod(p, 0o755)
    return d


def server(path: str, **kw) -> str:
    doc = {"host_key": None, "authorized_keys": None, "unreachable": False, "print_fp": True,
           "home": None, "password": None}
    if os.path.exists(path):
        with open(path) as f:
            doc.update(json.load(f))
    doc.update(kw)
    with open(path, "w") as f:
        json.dump(doc, f)
    return path


def log(path: str | None) -> list[dict]:
    try:
        with open(path or os.environ["FAKE_SSH_LOG"]) as f:
            return [json.loads(x) for x in f if x.strip()]
    except FileNotFoundError:
        return []


def fp(b64: str) -> str:
    digest = hashlib.sha256(base64.b64decode(b64)).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def _record(**kw) -> None:
    p = os.environ.get("FAKE_SSH_LOG")
    kw["env"] = sorted(k for k in os.environ if k.startswith("SSH_") or k == "DISPLAY")
    if p:
        with open(p, "a") as f:
            f.write(json.dumps(kw) + "\n")


def _srv() -> dict:
    with open(os.environ["FAKE_SSH_SERVER"]) as f:
        return json.load(f)


def parse(argv: list[str]) -> tuple[dict, list, dict, list]:
    flags, opts, o_list, i = {}, {}, [], 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            i += 1
            break
        if not a.startswith("-") or a == "-":
            break
        c = a[1]
        if c in WITHVAL:
            v = a[2:] or argv[i + 1]
            i += 1 if a[2:] else 2
            if c == "o":
                k, _, val = v.partition("=")
                o_list.append((k, val))
                opts.setdefault(k.lower(), val)          # the first value wins, as in ssh
            else:
                flags.setdefault(c, v)
        else:
            for ch in a[1:]:
                flags[ch] = True
            i += 1
    return flags, argv[i:], opts, o_list


def keyscan(argv: list[str]) -> int:
    srv = _srv()
    host = argv[-1]
    if srv.get("unreachable") or not srv.get("host_key"):
        return 1
    t, b64 = srv["host_key"]
    print(f"{host} {t} {b64}")
    return 0


def _hostkey(opts: dict, host: str, port: str, srv: dict) -> int | None:
    alias = opts.get("hostkeyalias", host)
    kh = opts.get("userknownhostsfile")
    algos = opts.get("hostkeyalgorithms", "")
    t, b64 = srv["host_key"]
    if algos and t not in algos.split(","):
        sys.stderr.write(f"Unable to negotiate with {host} port {port}: no matching host key type "
                         f"found. Their offer: {t}\n")
        return 255
    known = None
    if kh and os.path.exists(kh):
        with open(kh) as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and parts[0] == alias:
                    known = (parts[1], parts[2])
    if known is None:
        sys.stderr.write(f"No {t.split('-')[-1].upper()} host key is known for {alias} and you have "
                         f"requested strict checking.\nHost key verification failed.\n")
        return 255
    if known != (t, b64):
        sys.stderr.write("@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
                         "@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\n"
                         "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
                         "IT IS POSSIBLE THAT SOMEONE IS DOING SOMETHING NASTY!\n")
        if srv.get("print_fp", True):
            sys.stderr.write(f"The fingerprint for the {t.split('-')[-1].upper()} key sent by the "
                             f"remote host is\n{fp(b64)}.\n")
        sys.stderr.write(f"Host key for {alias} has changed and you have requested strict "
                         f"checking.\nHost key verification failed.\n")
        return 255
    return None


def _password(opts: dict, user: str, host: str, srv: dict) -> tuple[int | None, str | None]:
    """A password login: the askpass program's line against the server's password."""
    denied = f"{user}@{host}: Permission denied (publickey,password).\n"
    ap = os.environ.get("SSH_ASKPASS")
    if not ap or os.environ.get("SSH_ASKPASS_REQUIRE") != "force" or not srv.get("password"):
        sys.stderr.write(denied)
        return 255, None
    got = subprocess.run([ap, f"{user}@{host}'s password: "], capture_output=True, timeout=10)
    if got.returncode != 0 or got.stdout.decode().rstrip("\n") != srv["password"]:
        sys.stderr.write(denied)
        return 255, None
    return None, ""


def _auth(flags: dict, user: str, host: str, srv: dict) -> tuple[int | None, str | None]:
    from cryptography.hazmat.primitives import serialization
    idf = flags.get("i")
    denied = f"{user}@{host}: Permission denied (publickey).\n"
    if not idf or not os.path.exists(idf):
        sys.stderr.write(denied)
        return 255, None
    mode = os.stat(idf).st_mode & 0o777
    if mode & 0o077:
        sys.stderr.write(f"Permissions 0{mode:o} for '{idf}' are too open.\n" + denied)
        return 255, None
    with open(idf, "rb") as f:
        key = serialization.load_ssh_private_key(f.read(), password=None)
    pub = key.public_key().public_bytes(serialization.Encoding.OpenSSH,
                                        serialization.PublicFormat.OpenSSH).decode().split()
    ak = srv.get("authorized_keys")
    if ak and os.path.exists(ak):
        with open(ak) as f:
            for line in f:
                if f" {pub[0]} {pub[1]}" in line:
                    m = re.match(r'command="((?:[^"\\]|\\.)*)"', line)
                    return None, (m.group(1) if m else "")
    sys.stderr.write(denied)
    return 255, None


def ssh(argv: list[str]) -> int:
    flags, rest, opts, o_list = parse(argv)
    srv = _srv()
    cp = opts.get("controlpath")
    if "O" in flags:
        host = rest[0] if rest else ""
        _record(tool="ssh", argv=argv, mux=None, remote=None)
        if flags["O"] == "check":
            return 0 if cp and os.path.exists(cp) else 255
        if flags["O"] == "exit":
            if cp and os.path.lexists(cp):
                os.unlink(cp)
            return 0
        return 255
    host = rest[0]
    remote = " ".join(rest[1:]) if len(rest) > 1 else None
    port = flags.get("p", "22")
    user = flags.get("l", os.environ.get("USER", "u"))
    mux = False
    if not flags.get("M") and opts.get("controlmaster", "no") == "no" and cp and os.path.lexists(cp):
        if srv.get("dead_master"):
            sys.stderr.write(f"Control socket connect({cp}): Connection refused\n")
        else:
            mux = True
    _record(tool="ssh", argv=argv, mux=mux, remote=remote)
    if srv.get("unreachable"):
        sys.stderr.write(f"ssh: connect to host {host} port {port}: Connection refused\n")
        return 255
    if not mux:
        bad = _hostkey(opts, host, port, srv)
        if bad is not None:
            return bad
    if opts.get("preferredauthentications", "").startswith("password") and \
            opts.get("pubkeyauthentication") == "no":
        rc, forced = _password(opts, user, host, srv)
    else:
        rc, forced = _auth(flags, user, host, srv)
    if rc is not None:
        return rc
    if flags.get("M"):
        if cp and os.path.lexists(cp):
            os.unlink(cp)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(cp)
        s.close()
        return 0
    if flags.get("N"):
        return 0
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": srv.get("home") or os.environ.get("HOME", "/"),
           "SSH_CONNECTION": f"127.0.0.1 50000 127.0.0.1 {port}", "USER": user}
    if remote is not None:
        env["SSH_ORIGINAL_COMMAND"] = remote
    cmd = forced if forced else (remote or "")
    if not cmd:
        sys.stderr.write("fake ssh: no command and no forced command (a shell)\n")
        return 1
    return subprocess.call(["/bin/sh", "-c", cmd], env=env)


if __name__ == "__main__":
    tool, args = sys.argv[1], sys.argv[2:]
    sys.exit(keyscan(args) if tool == "ssh-keyscan" else ssh(args))
