"""Pairing an engine host by code (SRV-829): TandemApp's side of contracts/engine-pair-v1.

    w = Window(address, port)                   the engine's pairing routes, plain HTTP
    w.status()                                  GET /v1/engine/pair: the window's state, no code
    run = Exchange(code, public_key, key_id)    one run: x random, pA = g^x * M^w
    a1 = w.step1(run.request1())                POST /v1/engine/pair/1
    body2 = run.check1(a1, expect=fp)           the host key and cB checked; {session, cA}
    answer = run.open2(w.step2(body2))          POST /v1/engine/pair/2: the sealed answer, opened

THE MATH is proxy/pairing.py's (the group, w, RFC 9382's transcript and confirmations, the seal),
with the engine's identities: A = `tandemapp-key <fp> <key id>` of TandemApp's new ed25519 key, B =
`tandemengine <fp>` of the engine's SSH host key in step 1's answer. The pinned fixtures
(contracts/engine-pair-v1/fixtures) replay through this module byte for byte
(tests/test_engine_pair.py).

WHAT IS CHECKED before anything is trusted, in order:
  1  step 1's answer has its fields; the fingerprint of `host_key` is `fingerprint`, else
     `host_key_mismatch` (the run is abandoned)
  2  that fingerprint is the one the user saw (`expect`: the window's state read at the start, or
     the LAN advertisement), else `host_key_mismatch` with both (abandoned)
  3  its type is one this side pins (sshonce.PREFER), else `engine_failed` (abandoned, so the
     engine installs nothing)
  4  cB is HMAC(KcB, TT) over the identity of THAT host key, else `wrong_code` (abandoned): a wrong
     code and a host key swapped on the way look the same, and neither is trusted
  5  step 2's seal opens under Ke (its tag first), the plaintext is the contract's answer, its key
     id is ours and its host key the one step 1 named, else `engine_failed`: nothing is stored
`abandon` answers the tries left and the window's state; `wrong_code` carries them.

THE WIRE. Plain HTTP: the engine's port has no TLS yet, and the exchange needs none (SPAKE2 gives an
eavesdropper nothing to test codes against, the answer is sealed). Each refusal of the engine
(engine-pair-v1's `{error: {type, message}}`) becomes a proxy/enginecodes.py code with the
engine's message as `detail`; the code, the token, the keys and the protocol messages are in no
error and no log line.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import http.client
import json
import re
import secrets
import socket
import struct

from proxy import enginecodes, pairing, sshonce

PAIR = "engine-pair-v1"
PREFIX = "/v1/engine/pair"
DEFAULT_PORT = 8000
CODE_RE = re.compile(r"^[0-9]{6}$")
FP_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
HEX_RE = re.compile(r"^[0-9a-f]+$")
SESSION_RE = re.compile(r"^[0-9a-f]{32}$")
NAME_MAX = 80
TIMEOUT_S = 10.0
#: the engine's 409s of step 2 that say why the key could not go into authorized_keys
KEY_PLACE = ("ssh_dir_link", "ssh_dir_type", "ssh_dir_owner", "ssh_dir_mode", "ssh_dir_create",
             "authorized_keys_link", "authorized_keys_type", "authorized_keys_owner",
             "authorized_keys_mode", "authorized_keys_open")
ANSWER_KEYS = {"pair", "key_id", "ssh_user", "ssh_port", "host_key", "host_keys", "http_port",
               "http_token", "engine_version", "mode", "authorized_keys"}


def fingerprint(b64: str) -> str:
    """OpenSSH's SHA256 fingerprint of a public key blob (what `ssh-keygen -l` prints)."""
    raw = base64.b64decode(b64, validate=True)
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


def parse_key(line) -> tuple[str, str] | None:
    """(type, base64 blob) of an OpenSSH public key line whose blob names its own type; None."""
    if not isinstance(line, str):
        return None
    parts = line.strip().split()
    if len(parts) < 2 or len(parts[1]) > 8192:
        return None
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(blob) < 4:
        return None
    n = struct.unpack(">I", blob[:4])[0]
    if blob[4:4 + n] != parts[0].encode():
        return None
    return parts[0], parts[1]


def ed25519_public(line) -> str:
    """`ssh-ed25519 AAAA...` (no comment) of an ed25519 public key line; ValueError otherwise."""
    got = parse_key(line)
    if got is None or got[0] != "ssh-ed25519" or len(base64.b64decode(got[1])) != 51:
        raise ValueError("not an ssh-ed25519 public key")
    return f"{got[0]} {got[1]}"


def identity_a(key_fp: str, key_id: str) -> str:
    return f"tandemapp-key {key_fp} {key_id}"


def identity_b(host_fp: str) -> str:
    return f"tandemengine {host_fp}"


def _abandoned(kind: str, why: str, **facts):
    return enginecodes.error(kind, why, **facts)


class Exchange:
    """One run of the exchange on TandemApp's side. `x` is for the fixtures only."""

    def __init__(self, code: str, public_key: str, key_id: str, *, name: str = "TandemApp",
                 x: int | None = None):
        if not isinstance(code, str) or not CODE_RE.match(code):
            raise enginecodes.error("bad_code")
        if not isinstance(key_id, str) or not KEY_ID_RE.match(key_id):
            raise ValueError("bad key id")
        self.public = ed25519_public(public_key)
        self.key_id = key_id
        self.key_fp = fingerprint(self.public.split()[1])
        self.name = "".join(c for c in str(name or "") if c.isprintable())[:NAME_MAX]
        self._w = pairing.w_of(code)
        self._x = x if x is not None else 1 + secrets.randbelow(pairing.Q - 1)
        self.pa = pow(pairing.G, self._x, pairing.P) * pow(pairing.M, self._w, pairing.P) % pairing.P
        self.session: str | None = None
        self.host_key: str | None = None
        self.host_fp: str | None = None
        self._tt: bytes | None = None

    def request1(self) -> dict:
        body = {"pA": format(self.pa, "x"), "public_key": self.public, "key_id": self.key_id}
        if self.name:
            body["name"] = self.name
        return body

    def check1(self, answer, expect: str | None = None) -> dict:
        """Step 1's answer checked (the module's 1 to 4); step 2's body {session, cA}. An
        OnboardError otherwise, with `session` set so the caller can abandon the run."""
        if not isinstance(answer, dict) or answer.get("pair") != PAIR or \
                not SESSION_RE.match(str(answer.get("session") or "")) or \
                not isinstance(answer.get("cB"), str) or not FP_RE.match(str(answer.get("fingerprint"))):
            raise enginecodes.error("engine_failed", "step 1's answer is not the contract's")
        self.session = answer["session"]
        key = parse_key(answer.get("host_key"))
        offered = fingerprint(key[1]) if key else None
        if offered is None or offered != answer["fingerprint"]:
            raise _abandoned("host_key_mismatch", "the host key does not have the fingerprint "
                             "it came with", expected=answer["fingerprint"], offered=offered or "none")
        if expect is not None and expect != offered:
            raise _abandoned("host_key_mismatch", None, expected=expect, offered=offered)
        if key[0] not in sshonce.PREFER:
            raise _abandoned("engine_failed", f"the host key's type {key[0][:40]} is not one an "
                             "SSH login here pins")
        try:
            pb = pairing.element(answer.get("pB"))
        except pairing.PairError:
            raise enginecodes.error("engine_failed", "pB is not a group element") from None
        k = pow(pb * pow(pairing.N, pairing.Q - self._w, pairing.P) % pairing.P, self._x, pairing.P)
        kca, kcb, tt = pairing.run_keys(identity_a(self.key_fp, self.key_id), identity_b(offered),
                                        self.pa, pb, k, self._w)
        cb = answer["cB"]
        if not (cb.isascii() and hmac.compare_digest(cb, pairing.confirm_mac(kcb, tt))):
            raise _abandoned("wrong_code", None, tries_left=answer.get("tries_left"))
        self.host_key, self.host_fp, self._tt = f"{key[0]} {key[1]}", offered, tt
        return {"session": self.session, "cA": pairing.confirm_mac(kca, tt)}

    def open2(self, answer) -> dict:
        """Step 2's sealed answer, opened and checked (the module's 5): the plaintext."""
        if self._tt is None:
            raise RuntimeError("step 1 was not checked")
        sealed = answer.get("sealed") if isinstance(answer, dict) and answer.get("pair") == PAIR \
            else None
        try:
            raw = pairing.open_seal(pairing.ke_of(self._tt), sealed)
            doc = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise enginecodes.error("engine_failed", "the sealed answer does not open") from None
        why = check_answer(doc)
        if why is None and doc["key_id"] != self.key_id:
            why = "the answer is for another key id"
        if why is None and _key_text(doc["host_key"]) != self.host_key:
            why = "the answer names another host key than step 1"
        if why is not None:
            raise enginecodes.error("engine_failed", why)
        return doc


def _key_text(line) -> str | None:
    got = parse_key(line)
    return f"{got[0]} {got[1]}" if got else None


def check_answer(doc) -> str | None:
    """Why `doc` is not answer.schema.json's plaintext, or None."""
    if not isinstance(doc, dict) or set(doc) != ANSWER_KEYS or doc.get("pair") != PAIR:
        return "the answer's fields are not the contract's"
    ints = ("ssh_port", "http_port")
    if any(not isinstance(doc[k], int) or isinstance(doc[k], bool) or not 0 < doc[k] < 65536
           for k in ints):
        return "a port is not a port"
    if not isinstance(doc["ssh_user"], str) or not re.match(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}$",
                                                              doc["ssh_user"]):
        return "the SSH user is not a login name"
    if not isinstance(doc["http_token"], str) or len(doc["http_token"]) < 32 or \
            not doc["http_token"].isprintable() or any(c.isspace() for c in doc["http_token"]):
        return "the HTTP token is not one"
    if parse_key(doc["host_key"]) is None or not isinstance(doc["host_keys"], list) or \
            any(parse_key(k) is None for k in doc["host_keys"]):
        return "a host key does not read"
    if doc["mode"] not in ("setup", "engine") or not isinstance(doc["engine_version"], (str, type(None))):
        return "the mode or version is not the contract's"
    ak = doc["authorized_keys"]
    if not isinstance(ak, dict) or set(ak) != {"added", "line"} or not isinstance(ak["added"], bool) \
            or not isinstance(ak["line"], str):
        return "authorized_keys is not the contract's"
    if not isinstance(doc["key_id"], str) or not KEY_ID_RE.match(doc["key_id"]):
        return "the key id is not one"
    return None


# ====================================================================== the wire

def _refusal(status: int, doc: dict, headers: dict, address: str, port: int):
    e = doc.get("error") if isinstance(doc.get("error"), dict) else {}
    kind, msg = str(e.get("type") or ""), str(e.get("message") or "")[:300] or None
    f = {"address": address, "port": port}
    if kind == "wrong_code":
        m = re.search(r"(\d+) tries left", msg or "")
        if m:
            return enginecodes.error("wrong_code", None, tries_left=int(m.group(1)), state="open", **f)
        return enginecodes.error("code_spent", msg, state="spent", **f)
    if kind == "no_window" or (status == 401 and not kind.startswith("forbidden")):
        return enginecodes.error("no_window", msg, **f)
    if kind == "code_spent":
        return enginecodes.error("code_spent", msg, **f)
    if kind in ("no_pairing", "not_found") or status == 404:
        return enginecodes.error("pairing_unsupported", msg, **f)
    if kind == "rate_limited" or status == 429:
        try:
            wait = int(headers.get("retry-after") or 60)
        except ValueError:
            wait = 60
        return enginecodes.error("rate_limited", msg, retry_after=wait, **f)
    if kind == "forbidden_address" or (status == 403 and kind not in ("local_only", "window_key")):
        return enginecodes.error("not_allowed", msg, **f)
    if kind in ("key_present", "key_id_taken"):
        return enginecodes.error("key_present", msg, **f)
    if kind in KEY_PLACE:
        return enginecodes.error("key_refused", msg, **f)
    return enginecodes.error("engine_failed", msg or f"HTTP {status}", **f)


class Window:
    """The pairing routes of the engine at address:port. `connect(address, port, timeout)` makes
    the HTTP connection (a fake's in the tests)."""

    def __init__(self, address: str, port: int = DEFAULT_PORT, *, timeout: float = TIMEOUT_S,
                 connect=None):
        self.address, self.port, self.timeout = address, int(port), timeout
        self._connect = connect or (lambda a, p, t: http.client.HTTPConnection(a, p, timeout=t))

    def _call(self, method: str, path: str, body=None) -> tuple[int, dict, dict]:
        conn = self._connect(self.address, self.port, self.timeout)
        try:
            data = json.dumps(body).encode() if body is not None else None
            hdrs = {"Accept": "application/json"}
            if data is not None:
                hdrs["Content-Type"] = "application/json"
            conn.request(method, path, body=data, headers=hdrs)
            r = conn.getresponse()
            raw = r.read(1 << 20)
            headers = {k.lower(): v for k, v in r.getheaders()}
            status = r.status
        except (OSError, http.client.HTTPException, socket.timeout) as exc:
            raise enginecodes.error("unreachable", type(exc).__name__, address=self.address,
                                    port=self.port) from None
        finally:
            conn.close()
        try:
            doc = json.loads(raw) if raw else {}
        except (ValueError, UnicodeDecodeError):
            doc = None
        if not isinstance(doc, dict):
            raise enginecodes.error("not_an_engine", f"HTTP {status}, not JSON",
                                    address=self.address, port=self.port)
        return status, doc, headers

    def _ok(self, method: str, path: str, body=None) -> dict:
        st, doc, headers = self._call(method, path, body)
        if st != 200:
            raise _refusal(st, doc, headers, self.address, self.port)
        if doc.get("pair") != PAIR:
            raise enginecodes.error("not_an_engine", "the answer is not engine-pair-v1's",
                                    address=self.address, port=self.port)
        return doc

    def status(self) -> dict:
        """{pair, open, state, expires_in?, tries_left?, fingerprint?}: the window's state."""
        doc = self._ok("GET", PREFIX)
        if not isinstance(doc.get("open"), bool) or not isinstance(doc.get("state"), str):
            raise enginecodes.error("not_an_engine", "the window's state is not the contract's",
                                    address=self.address, port=self.port)
        if doc.get("fingerprint") is not None and not FP_RE.match(str(doc["fingerprint"])):
            raise enginecodes.error("not_an_engine", "the window's fingerprint is not one",
                                    address=self.address, port=self.port)
        return doc

    def step1(self, body: dict) -> dict:
        return self._ok("POST", PREFIX + "/1", body)

    def step2(self, body: dict) -> dict:
        return self._ok("POST", PREFIX + "/2", body)

    def abandon(self, session: str) -> dict | None:
        """The run given up: {abandoned, tries_left, state}, or None when the engine did not
        answer it (the run then expires there in 60 s)."""
        try:
            doc = self._ok("POST", PREFIX + "/abandon", {"session": session})
        except enginecodes.OnboardError:
            return None
        if not isinstance(doc.get("tries_left"), int) or not isinstance(doc.get("state"), str):
            return None
        return doc

    def health(self) -> int | None:
        """The HTTP status of GET /health, None when nothing answers (tells an old engine from
        something else)."""
        try:
            st, doc, _ = self._call("GET", "/health")
        except enginecodes.OnboardError:
            return None
        return st if isinstance(doc, dict) and ("status" in doc or "mode" in doc) else None


def run(window: Window, code: str, public_key: str, key_id: str, *, expect: str | None = None,
        name: str = "TandemApp") -> dict:
    """The whole exchange against `window`: the engine's opened answer. A run whose check fails is
    abandoned; its `wrong_code` (or `code_spent`, when that was the last try) carries the tries
    left and the window's state."""
    ex = Exchange(code, public_key, key_id, name=name)
    a1 = window.step1(ex.request1())
    try:
        body2 = ex.check1(a1, expect=expect)
    except enginecodes.OnboardError as exc:
        if ex.session is not None:
            left = window.abandon(ex.session)
            if left is not None and exc.code == "wrong_code":
                if left["state"] != "open":
                    raise enginecodes.error("code_spent", None, state=left["state"],
                                            address=window.address, port=window.port) from None
                raise enginecodes.error("wrong_code", None, tries_left=left["tries_left"],
                                        state=left["state"], address=window.address,
                                        port=window.port) from None
        raise
    return ex.open2(window.step2(body2))
