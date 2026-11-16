"""The thinking stash (SRV-881): an OpenAI-shaped client in a tool loop on a Messages API backend
keeps its earlier thinking.

The chat shape has no place for a thinking block's signature, so a client that speaks it sends
each earlier assistant turn back as text and tool calls only. A Messages API server checks the
thinking of a tool loop against its signature, and the blocks may be missing from the front of a
history but not from its middle. So the relay keeps every translated answer's own content blocks
(thinking, its signature, redacted thinking, text, tool calls, as the backend sent them) and,
when the client sends that turn back, sends those blocks upstream in place of the translated
ones.

KEY: the client's credential (a hash of its bearer; one stash cannot be read with another key),
the backend, the upstream model (blocks never go to another backend or model), and the turn: its
tool call ids, or for a turn without calls its place in the history and a hash of its text. A
turn with no thinking block is not kept: its translation is already what the backend saw.

A TURN COMES BACK: kept, and its calls (names and inputs) are what the backend sent: the original
blocks go. Evicted, expired, or changed: it goes without its thinking, and so does every earlier
turn (thinking may be missing from the front, not from the middle). The count of turns that went
without their thinking marks the ledger row (`error_type` `thinking_evicted` when the row has no
error of its own) and the answer's `X-Tandem-Dropped` names `thinking`.

BOUNDS: 64 MB of blocks (their JSON) and 1 hour since a turn was last kept or sent, in memory,
least recently used first out. An evicted turn's key is remembered (not its blocks, at most
TOMBS keys) so its return is known as an eviction.
"""

from __future__ import annotations

import collections
import copy
import hashlib
import json
import threading
import time

MAX_BYTES = 64 * 1024 * 1024
TTL_S = 3600.0
TOMBS = 100_000
THINKING = ("thinking", "redacted_thinking")


def token_of(headers) -> str:
    """The stash's owner: a hash of the client's bearer (`Authorization`), else `anon`."""
    try:
        auth = (headers.get("Authorization") or "").strip()
    except Exception:                                              # noqa: BLE001
        auth = ""
    tok = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
    return "k:" + hashlib.sha256(tok.encode()).hexdigest() if tok else "anon"


def _blocks(content) -> list:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    return [b for b in content or [] if isinstance(b, dict)]


def _calls(blocks: list) -> list:
    return [(b.get("id"), b.get("name"), b.get("input")) for b in blocks
            if b.get("type") == "tool_use"]


def turn_key(content, at: int) -> str:
    """A turn's key as the client sends it back: its call ids, else its index in the
    history (`at`) and its text."""
    blocks = _blocks(content)
    ids = [str(c[0]) for c in _calls(blocks)]
    if ids:
        return "calls:" + "\x1f".join(ids)
    text = "".join(b.get("text") or "" for b in blocks if b.get("type") == "text")
    return f"text:{at}:" + hashlib.sha256(text.encode()).hexdigest()


class Stash:
    def __init__(self, max_bytes: int = MAX_BYTES, ttl_s: float = TTL_S, tombs: int = TOMBS,
                 clock=time.monotonic):
        self.max_bytes, self.ttl_s, self.tombs, self.clock = max_bytes, ttl_s, tombs, clock
        self._d: collections.OrderedDict = collections.OrderedDict()   # key -> (blocks, size, t)
        self._dead: collections.OrderedDict = collections.OrderedDict()
        self.bytes = 0
        self._lock = threading.Lock()

    @staticmethod
    def _key(scope: tuple, tk: str) -> str:
        return hashlib.sha256("\x1e".join([*map(str, scope), tk]).encode()).hexdigest()

    def _bury(self, key: str) -> None:
        _, size, _ = self._d.pop(key)
        self.bytes -= size
        self._dead[key] = None
        self._dead.move_to_end(key)
        while len(self._dead) > self.tombs:
            self._dead.popitem(last=False)

    def _expire(self, now: float) -> None:
        while self._d:
            key, (_, _, t) = next(iter(self._d.items()))
            if now - t <= self.ttl_s:
                break
            self._bury(key)

    def keep(self, scope: tuple, content, at: int) -> bool:
        """Keep one answer's content blocks when they hold thinking; `at` is the index the turn
        takes in the history (the request's message count). True when kept."""
        blocks = _blocks(content)
        if not any(b.get("type") in THINKING for b in blocks):
            return False
        blocks = copy.deepcopy(blocks)
        size = len(json.dumps(blocks, ensure_ascii=False).encode())
        if size > self.max_bytes:
            return False
        key = self._key(scope, turn_key(blocks, at))
        with self._lock:
            now = self.clock()
            self._expire(now)
            if key in self._d:
                self.bytes -= self._d.pop(key)[1]
            self._dead.pop(key, None)
            self._d[key] = (blocks, size, now)
            self.bytes += size
            while self.bytes > self.max_bytes and self._d:
                self._bury(next(iter(self._d)))
        return True

    def restore(self, scope: tuple, messages: list) -> tuple[list, int]:
        """(the Messages history with each kept assistant turn's own blocks, the number of
        turns that go without their thinking)."""
        found = []                      # (index, "hit" | "lost", blocks)
        with self._lock:
            now = self.clock()
            self._expire(now)
            for i, m in enumerate(messages):
                if not isinstance(m, dict) or m.get("role") != "assistant":
                    continue
                key = self._key(scope, turn_key(m.get("content"), i))
                got = self._d.get(key)
                if got is not None:
                    if _calls(got[0]) == _calls(_blocks(m.get("content"))):
                        found.append((i, "hit", key))
                    else:
                        found.append((i, "lost", key))
                elif key in self._dead:
                    found.append((i, "lost", key))
            last_lost = max((i for i, s, _ in found if s == "lost"), default=-1)
            out = list(messages)
            dropped = 0
            for i, s, key in found:
                if i <= last_lost:
                    dropped += 1
                    continue
                blocks, size, _ = self._d[key]
                self._d[key] = (blocks, size, now)
                self._d.move_to_end(key)
                out[i] = dict(out[i], content=copy.deepcopy(blocks))
        return out, dropped

    def view(self) -> dict:
        with self._lock:
            return {"turns": len(self._d), "bytes": self.bytes, "max_bytes": self.max_bytes,
                    "ttl_s": self.ttl_s, "evicted_known": len(self._dead)}
