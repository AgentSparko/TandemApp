"""One remote hook call: its payload, its signature, the HTTP post (SRV-849, SEC-16).

THE PAYLOAD. A hook sees what its payload level grants and nothing more:

    meta      ids, the model, sizes, counts, timings, the class, tool names. Never a prompt's or an
              answer's text: `event` holds no `content` key
    redacted  meta plus `content`, every string of it through the conductor's redaction first
              (tandemapp/conductor/redact.py: keys, tokens, passwords become [redacted:<kind>]).
              A redaction that fails gives the hook nothing (`RedactionFailed`); the hook's
              failure policy decides
    full      meta plus `content` as it is. Granted per hook; a destination outside the LAN needs a
              second confirm (proxy/hookstore.py)

THE SIGNATURE. Every call carries

    X-Tandem-Hook        the hook's id
    X-Tandem-Delivery    the call's id (also the body's `id`, which the signature covers)
    X-Tandem-Timestamp   unix seconds
    X-Tandem-Signature   v1=<hex HMAC-SHA256(secret, "<timestamp>." + body)>; during a secret
                         rotation's grace a second v1= for the previous secret, comma-separated

The receiver recomputes the HMAC over the bytes it read, refuses a timestamp more than
REPLAY_WINDOW_S away from its clock, and refuses a body `id` it has seen inside the window
(`Verifier`, the fake hook server and docs/hooks.md use this code's rule). The secret is the
string the hook's creation answered (`hks_...`), its UTF-8 bytes the HMAC key. It is never logged,
journaled, exported or answered again.

THE DESTINATION. `host_is_lan` says whether a URL's host is on the LAN: loopback, the private
ranges, link-local, the tailnet range (100.64.0.0/10), the IPv6 local ranges, a single-label name
or one under .local, .lan, .home.arpa or .internal, or a name all of whose addresses are one of
those. A full-payload hook granted for the LAN checks the address it actually connected to on
every call: a name that moved outside the LAN is refused before a byte of the payload is sent.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import secrets as _rand
import socket
import ssl
import time
from urllib.parse import urlsplit

SIG_HEADER = "X-Tandem-Signature"
TS_HEADER = "X-Tandem-Timestamp"
HOOK_HEADER = "X-Tandem-Hook"
DELIVERY_HEADER = "X-Tandem-Delivery"
REPLAY_WINDOW_S = 300
ANSWER_MAX = 1024 * 1024
SECRET_PREFIX = "hks_"
USER_AGENT = "TandemApp-hooks/1"

LAN_NETS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16",
    "100.64.0.0/10", "::1/128", "fc00::/7", "fe80::/10"))
LAN_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal")


class CallError(Exception):
    """A call that got no usable answer: `code` is timeout, connect, http_<status>, too_large,
    bad_json, outside_lan."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class RedactionFailed(Exception):
    """The redaction step raised: the hook gets no payload."""


# ----------------------------------------------------------------------------- the signature
def new_secret() -> str:
    return SECRET_PREFIX + _rand.token_urlsafe(32)


def signature(secret: str, ts: int, body: bytes) -> str:
    return hmac.new(secret.encode(), str(int(ts)).encode() + b"." + body,
                    hashlib.sha256).hexdigest()


def sign(secrets: list[str], ts: int, body: bytes) -> str:
    """The X-Tandem-Signature value: one v1= per secret (the current first)."""
    return ",".join("v1=" + signature(s, ts, body) for s in secrets)


class Verifier:
    """The receiver's check (the fake hook server's, and the docs' examples do the same):
    `check(headers, body) -> (True, "ok") | (False, why)`, why one of no_signature, bad_timestamp,
    stale, bad_signature, bad_body, replayed."""

    def __init__(self, secrets: list[str], window_s: int = REPLAY_WINDOW_S, clock=time.time):
        self.secrets = list(secrets)
        self.window_s = window_s
        self.clock = clock
        self.seen: dict[str, float] = {}

    def check(self, headers, body: bytes) -> tuple[bool, str]:
        def h(name):
            for k, v in (headers.items() if hasattr(headers, "items") else headers):
                if k.lower() == name.lower():
                    return v
            return None
        sig, ts = h(SIG_HEADER), h(TS_HEADER)
        if not sig or not ts:
            return False, "no_signature"
        try:
            t = int(ts)
        except ValueError:
            return False, "bad_timestamp"
        now = self.clock()
        if abs(now - t) > self.window_s:
            return False, "stale"
        got = [p.strip()[3:] for p in sig.split(",") if p.strip().startswith("v1=")]
        if not any(hmac.compare_digest(signature(s, t, body), g) for s in self.secrets
                   for g in got):
            return False, "bad_signature"
        try:
            did = json.loads(body)["id"]
        except (ValueError, KeyError, TypeError):
            return False, "bad_body"
        for k in [k for k, v in self.seen.items() if now - v > self.window_s]:
            del self.seen[k]
        if did in self.seen:
            return False, "replayed"
        self.seen[did] = now
        return True, "ok"


# ----------------------------------------------------------------------------- the destination
def destination(url: str) -> dict:
    """{scheme, host, port} of a hook's URL (the host lower case, without brackets)."""
    u = urlsplit(url)
    port = u.port or (443 if u.scheme == "https" else 80)
    return {"scheme": u.scheme, "host": (u.hostname or "").lower(), "port": port}


def ip_is_lan(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None) is not None:
        ip = ip.ipv4_mapped
    return any(ip in n for n in LAN_NETS if n.version == ip.version)


def host_is_lan(host: str, resolve=None) -> bool:
    """The host is on the LAN (see THE DESTINATION). A name that does not resolve is not."""
    host = (host or "").lower().strip("[]").rstrip(".")
    if not host:
        return False
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        return ip_is_lan(host)
    except ValueError:
        pass
    if host == "localhost" or "." not in host or host.endswith(LAN_SUFFIXES):
        return True
    resolve = resolve or socket.getaddrinfo
    try:
        addrs = {a[4][0] for a in resolve(host, None)}
    except (OSError, UnicodeError):
        return False
    return bool(addrs) and all(ip_is_lan(a) for a in addrs)


def check_url(url) -> str | None:
    """None when `url` may be a hook's endpoint, else why not."""
    if not isinstance(url, str) or not url or len(url) > 2048:
        return "an http or https URL of at most 2048 characters"
    try:
        u = urlsplit(url)
        u.port
    except ValueError:
        return "the URL does not parse"
    if u.scheme not in ("http", "https") or not u.hostname:
        return "an http or https URL with a host"
    if u.username is not None or u.password is not None:
        return "no user or password in the URL: the call is signed instead"
    if u.fragment:
        return "no #fragment in the URL"
    if any(c.isspace() for c in url):
        return "no spaces in the URL"
    return None


# ----------------------------------------------------------------------------- the post
def post(url: str, body: bytes, headers: list[tuple[str, str]], timeout_s: float,
         need_lan: bool = False, max_bytes: int = ANSWER_MAX) -> tuple[int, bytes]:
    """POST `body` to `url`; (status, answer bytes). `need_lan`: the address connected to must be
    on the LAN, checked before the body is sent. CallError for anything else."""
    u = urlsplit(url)
    timeout_s = max(0.001, timeout_s)
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    if u.scheme == "https":
        conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout_s,
                                           context=ssl.create_default_context())
    else:
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout_s)
    try:
        try:
            conn.connect()
        except socket.timeout:
            raise CallError("timeout", f"no connection within {timeout_s * 1000:.0f} ms")
        except OSError as exc:
            raise CallError("connect", f"connect failed: {type(exc).__name__}")
        if need_lan:
            try:
                peer = conn.sock.getpeername()[0]
            except OSError:
                peer = ""
            if not ip_is_lan(peer):
                raise CallError("outside_lan", "the destination resolved outside the LAN; the "
                                "full payload was not sent")
        try:
            conn.putrequest("POST", path, skip_accept_encoding=True)
            for k, v in headers:
                conn.putheader(k, v)
            conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body)
            r = conn.getresponse()
            data = r.read(max_bytes + 1)
        except socket.timeout:
            raise CallError("timeout", f"no answer within {timeout_s * 1000:.0f} ms")
        except (OSError, http.client.HTTPException) as exc:
            raise CallError("connect", f"the call failed: {type(exc).__name__}")
        if len(data) > max_bytes:
            raise CallError("too_large", f"the answer is over {max_bytes} bytes")
        return r.status, data
    finally:
        conn.close()


# ----------------------------------------------------------------------------- the payload
def redact_value(v):
    """`v` with every string through the conductor's redaction (a new value)."""
    from tandemapp.conductor import redact
    if isinstance(v, str):
        return redact.redact(v)[0]
    if isinstance(v, list):
        return [redact_value(x) for x in v]
    if isinstance(v, dict):
        return {k: redact_value(x) for k, x in v.items()}
    return v


def event_at(level: str, meta: dict, content, redactor=None) -> dict:
    """The stage's event as a hook of `level` sees it."""
    ev = dict(meta)
    if level == "meta" or content is None:
        return ev
    if level == "redacted":
        try:
            ev["content"] = (redactor or redact_value)(content)
        except Exception as exc:                                   # noqa: BLE001
            raise RedactionFailed(type(exc).__name__)
        return ev
    ev["content"] = content
    return ev


def _text_len(c) -> int:
    if isinstance(c, str):
        return len(c)
    if isinstance(c, list):
        return sum(len(p.get("text") or "") for p in c if isinstance(p, dict)
                   and isinstance(p.get("text"), str))
    return 0


def request_meta(body: dict, raw_len: int | None = None) -> dict:
    """What a meta hook knows of a request body: sizes, the scalar parameters, tool names."""
    from proxy import hookstages
    msgs = body.get("messages") if isinstance(body.get("messages"), list) else []
    chars = {"system": 0, "user": 0, "assistant": 0, "tool": 0}
    for m in msgs:
        if isinstance(m, dict):
            role = m.get("role")
            role = "system" if role == "developer" else role
            if role in chars:
                chars[role] += _text_len(m.get("content"))
    if not msgs:
        for k in ("prompt", "input"):
            if isinstance(body.get(k), str):
                chars["user"] += len(body[k])
    tools = body.get("tools") if isinstance(body.get("tools"), list) else []
    names = []
    for t in tools:
        if isinstance(t, dict):
            f = t.get("function") if isinstance(t.get("function"), dict) else t
            n = f.get("name")
            if isinstance(n, str):
                names.append(n[:128])
    params, others = {}, []
    for k in hookstages.PARAMS:
        if k not in body:
            continue
        v = body[k]
        if isinstance(v, bool) or isinstance(v, (int, float)) or v is None or (
                isinstance(v, str) and len(v) <= 64):
            params[k] = v
        elif k == "response_format" and isinstance(v, dict) and isinstance(v.get("type"), str):
            params[k] = {"type": v["type"][:64]}
        else:
            others.append(k)
    if raw_len is None:
        raw_len = len(json.dumps(body, ensure_ascii=False).encode())
    return {"sizes": {"body_bytes": int(raw_len), "messages": len(msgs), "tools": len(tools),
                      "chars": chars},
            "params": params, "params_set": others, "tool_names": names}


def answer_meta(obj, status: int, body_bytes: int, streamed: bool) -> dict:
    """What a meta hook knows of an answer: status, finish, usage, sizes, tool names."""
    finish, content, reasoning, calls, names, usage = None, 0, 0, 0, [], None
    if isinstance(obj, dict):
        for ch in obj.get("choices") or []:
            if not isinstance(ch, dict):
                continue
            finish = finish or ch.get("finish_reason")
            m = ch.get("message") or {}
            if isinstance(m, dict):
                content += _text_len(m.get("content"))
                rc = m.get("reasoning_content") or m.get("reasoning")
                reasoning += len(rc) if isinstance(rc, str) else 0
                for tc in m.get("tool_calls") or []:
                    calls += 1
                    f = (tc or {}).get("function") or {}
                    if isinstance(f.get("name"), str):
                        names.append(f["name"][:128])
            elif isinstance(ch.get("text"), str):
                content += len(ch["text"])
        u = obj.get("usage")
        if isinstance(u, dict):
            usage = {k: u[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens",
                                       "input_tokens", "output_tokens")
                     if isinstance(u.get(k), int)}
    return {"status": int(status), "finish_reason": finish if isinstance(finish, str) else None,
            "streamed": bool(streamed), "usage": usage,
            "sizes": {"body_bytes": int(body_bytes), "content_chars": content,
                      "reasoning_chars": reasoning, "tool_calls": calls},
            "tool_names": names}


def calls_meta(calls: list) -> list[dict]:
    out = []
    for i, tc in enumerate(calls or []):
        if not isinstance(tc, dict):
            continue
        f = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        args = f.get("arguments")
        out.append({"index": tc.get("index", i) if isinstance(tc.get("index", i), int) else i,
                    "id": tc.get("id") if isinstance(tc.get("id"), str) else None,
                    "name": f.get("name")[:128] if isinstance(f.get("name"), str) else None,
                    "args_bytes": len(args.encode()) if isinstance(args, str) else 0})
    return out
