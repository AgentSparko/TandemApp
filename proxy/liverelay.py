"""The engines' live figures, relayed into the controller's own live view.

The controller measures what a client could measure (proxy/measure.py): the request's
arrival, its first frame, its frames. The live rates of the Performance page — a request's
`decode_live_now_tps` and `decode_live_avg_tps`, the history sample's and `now`'s
`decode_live_tps` and `prefill_live_tps` — come off the engine's decode loop, and the
controller has none: its `LiveRegistry` is built with `blocks=None, activity=False`
(proxy/app.py), so behind a TandemLLM backend the charts were empty and every in-flight
row read null.

THE JOIN. The engine keys its live rows by its request id, which is the id its answer
carries — `Stamps.response_id` (SRV-733: "our engine's is its ledger row's request_id").
The relay stamps it onto the record while the stream runs (proxy/relay.py), so a relayed
request knows its engine row from its first frame on; a request still waiting for its
first token has no id yet and its row stays exactly what the controller measured. The
engine's own `phase` is not carried: the controller's row keeps the phase its own stamps
say, which is what its timings are consistent with.

One subscriber thread per enabled `tandemllm` backend reads the engine's live stream
(`/v1/dashboard/live`, the same SSE the dashboard reads, with the backend's key when it
has one) and keeps, fresh for `ttl_s`: the in-flight rows by engine request id, the
engine's `now` block, its newest sample and its engine block. A supervisor thread syncs
the subscribers with the backend table and lets what went stale drop out. Every getter
answers None when there is nothing fresh, and the live view is then exactly what it was
before this module existed.

The figures of the whole engine (the rates, the engine block) come from one backend: the
default one when it is a fresh TandemLLM backend, else the freshest — a controller in
front of several engines shows one engine's rates, named by its model. Per-request rows
join exactly, each to the engine that answered it.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.parse

from proxy import canary as canary_mod

#: how long the engine's figures may stand in for ones the controller cannot measure
TTL_S = 5.0
#: how often the supervisor syncs the subscribers with the backend table
SYNC_S = 2.0
#: the live connection's socket timeout: the engine pings its streams every 15 s
READ_TIMEOUT_S = 60.0
#: the reconnect backoff: seconds until the next try after the stream died n times in a row
BACKOFF_S = (1.0, 2.0, 5.0, 10.0, 30.0)
#: the per-request fields carried into the controller's in-flight rows (its own nulls filled)
ROW_FIELDS = ("decode_live_now_tps", "decode_live_avg_tps", "decode_warmup")
#: the rate fields carried into the controller's history sample and `now`
RATE_FIELDS = ("decode_tps", "prefill_tps", "decode_live_tps", "prefill_live_tps")
#: the engine block's fields the controller cannot measure itself
ENGINE_FIELDS = ("model", "kv", "memory", "store")
#: the phases worth keeping: a finished engine row is the controller's own `recent` already
IN_FLIGHT = ("queued", "prefill", "decode")


class _Sub:
    """One backend: its subscriber thread and the last event it saw."""

    def __init__(self, bid: str, url: str):
        self.id, self.url = bid, url
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.at = 0.0                      # perf time of the last event kept (0: none yet)
        self.rows: dict[str, dict] = {}    # engine request id -> the fields we carry
        self.now: dict = {}
        self.sample: dict = {}
        self.engine: dict = {}


class LiveRelay:
    """`row_fields(rec)` / `rates()` / `engine_block()`: the engine-side live figures, or
    None when nothing fresh backs them. `svc` is read as the fleet changes: its backend
    table picks the subscribers, its key store answers for the ones that need a key."""

    def __init__(self, svc, *, ttl_s: float = TTL_S, sync_s: float = SYNC_S, log=None):
        self.svc = svc
        self.ttl_s = float(ttl_s)
        self.sync_s = float(sync_s)
        self.log = log or (lambda m: None)
        self._lock = threading.Lock()
        self._subs: dict[str, _Sub] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ----------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._supervise, name="qse-liverelay",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            subs = list(self._subs.values())
            self._subs.clear()
        for s in subs:
            s.stop.set()
        for s in subs:
            if s.thread is not None:
                s.thread.join(timeout=1.0)
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    # ----------------------------------------------------------------- the subscribers
    def _supervise(self) -> None:
        while not self._stop.wait(self.sync_s):
            try:
                self._sync()
            except Exception as exc:                             # noqa: BLE001 (never the box)
                self.log(f"[live-relay] the sync failed: {type(exc).__name__}: {exc}")

    def _sync(self) -> None:
        """One subscriber per enabled tandemllm backend, none for the others; a backend
        whose URL moved gets a fresh one (the old thread sees its stop and dies)."""
        want = {b["id"]: b["url"] for b in self.svc.cfg.backends()
                if b.get("kind") == "tandemllm" and b.get("enabled", True)}
        with self._lock:
            for bid in list(self._subs):
                if bid not in want or self._subs[bid].url != want[bid]:
                    self._subs.pop(bid).stop.set()
            for bid, url in want.items():
                if bid in self._subs:
                    continue
                s = _Sub(bid, url)
                s.thread = threading.Thread(target=self._watch, args=(s,),
                                            name=f"qse-liverelay-{bid}", daemon=True)
                self._subs[bid] = s
                s.thread.start()

    def _watch(self, s: _Sub) -> None:
        fails = 0
        while not s.stop.is_set():
            try:
                self._read_once(s)
                fails = 0
            except Exception:                                    # noqa: BLE001 (reconnect)
                pass
            if s.stop.is_set():
                return
            s.stop.wait(BACKOFF_S[min(fails, len(BACKOFF_S) - 1)])
            fails += 1

    def _read_once(self, s: _Sub) -> None:
        """One live connection: keep the subscriber's figures at the newest event. The
        engine ticks once a second while a stream is open, so one read per event is the
        whole cost; a stream that dies (restart, idle cut) returns and reconnects."""
        conn, target = _conn(canary_mod.engine_url(s.url, "/v1/dashboard/live"))
        try:
            conn.request("GET", target, headers=self._headers(s.id))
            resp = conn.getresponse()
            if resp.status != 200:
                resp.read(64 * 1024)
                return
            if conn.sock is not None:
                conn.sock.settimeout(READ_TIMEOUT_S)     # the engine pings every 15 s
            data: list[str] = []
            while not s.stop.is_set():
                line = resp.readline()
                if not line:
                    return
                line = line.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("data:"):
                    data.append(line[5:].lstrip())
                elif not line and data:
                    self._keep(s, "\n".join(data))
                    data = []
        finally:
            conn.close()

    def _headers(self, bid: str) -> dict:
        h = {"User-Agent": "tandem-app/live-relay", "Accept": "text/event-stream"}
        try:
            key = self.svc.cfg.key_for(bid)
        except Exception:                                        # noqa: BLE001
            key = None
        if key:
            h["Authorization"] = f"Bearer {key}"
        return h

    def _keep(self, s: _Sub, payload: str) -> None:
        try:
            doc = json.loads(payload)
        except ValueError:
            return
        if not isinstance(doc, dict):
            return
        rows = {}
        for r in doc.get("requests") or []:
            if not isinstance(r, dict) or r.get("phase") not in IN_FLIGHT:
                continue
            rid = r.get("request_id")
            if not isinstance(rid, str):
                continue
            rows[rid] = {k: r[k] for k in ROW_FIELDS if r.get(k) is not None}
        now, sample, engine = (doc.get("now"), doc.get("sample"), doc.get("engine"))
        with self._lock:
            if s.stop.is_set():
                return
            s.rows = rows
            s.now = now if isinstance(now, dict) else {}
            s.sample = sample if isinstance(sample, dict) else {}
            s.engine = engine if isinstance(engine, dict) else {}
            s.at = time.monotonic()

    # ----------------------------------------------------------------- the getters
    def _fresh(self) -> dict[str, _Sub]:
        with self._lock:
            subs = dict(self._subs)
        return {bid: s for bid, s in subs.items()
                if s.at and time.monotonic() - s.at <= self.ttl_s}

    def _preferred(self) -> _Sub | None:
        """The engine whose whole-engine figures the controller shows: the default backend
        when it is a fresh tandemllm one, else the freshest."""
        fresh = self._fresh()
        if not fresh:
            return None
        try:
            default = self.svc.cfg.default_backend
        except Exception:                                        # noqa: BLE001
            default = None
        sub = fresh.get(default) if default else None
        return sub or max(fresh.values(), key=lambda s: s.at)

    def row_fields(self, rec) -> dict | None:
        """The engine-side fields of a relayed request's live row, None when the engine
        has no fresh row for it. The join: `rec.backend` picks the engine, and
        `rec.upstream_request` — the answer's id, the engine's request id (SRV-733) —
        picks its row."""
        bid = getattr(rec, "backend", None)
        rid = getattr(rec, "upstream_request", None)
        if not bid or not rid:
            return None
        with self._lock:
            s = self._subs.get(bid)
            row = None
            if s is not None and s.at and time.monotonic() - s.at <= self.ttl_s:
                row = s.rows.get(rid)
        return dict(row) if row else None

    def rates(self) -> dict | None:
        """The rate fields the controller cannot measure, from the engine it serves by
        default: the newest sample's, falling back to its `now`. None when no engine is
        fresh — the live view then reads exactly as it did without this module."""
        s = self._preferred()
        if s is None:
            return None
        out = {}
        for k in RATE_FIELDS:
            v = s.sample.get(k)
            if v is None:
                v = s.now.get(k)
            if v is not None:
                out[k] = v
        return out or None

    def engine_block(self) -> dict | None:
        """The engine block's facts the controller cannot measure (model, kv, memory,
        store) from the engine it serves by default; None when none is fresh."""
        s = self._preferred()
        if s is None:
            return None
        out = {k: s.engine[k] for k in ENGINE_FIELDS if s.engine.get(k) is not None}
        return out or None


def _conn(url: str, connect_s: float = 5.0) -> tuple[http.client.HTTPConnection, str]:
    """A connection to an engine route (the idiom of proxy/chatengine.py's live pass-
    through): (conn, path+query to request)."""
    u = urllib.parse.urlsplit(url)
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    target = u.path + (f"?{u.query}" if u.query else "")
    return cls(u.hostname, u.port or (443 if u.scheme == "https" else 80),
               timeout=connect_s), target or "/"