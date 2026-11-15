"""What this TandemApp runs (OPS-288, VIS-280, SRV-845): Chat and the conductor switched on and off
from the page, and what is connected to it.

    p = Parts(cfg, install_chat=..., install_conductor=..., state_dir=..., app=...)
    GET  /v1/fleet/app/parts                  chat, conductor, the engine hosts' own mode, the
                                              backends, where the MCP servers are
    POST /v1/fleet/app/parts/{chat|conductor} {on, confirm?}
                                              without confirm: the plan (what changes, in words,
                                              and its confirm), nothing changed; with the plan's
                                              confirm: switched, then the view

BARE BY DEFAULT. A new TandemApp (packaging/install.sh, the container, the Helm chart) runs only
the gateway in front of the engines it pairs: Chat off (no chatd, no sandbox runtime needed), the
conductor off (no profiles, router, packages, notes, no decision model), no MCP server. Each part
is switched on here by an admin.

THE SETTINGS. `parts.chat` and `parts.conductor` ("on" or "off") in the config database, written
only by a switch here and audited. While a key is absent the install decides: TANDEM_CHAT
(`--chat` off, external or own) and TANDEM_CONDUCTOR (`--conductor-config`, a folder or off). A
default changed in a later release therefore never flips a running install, and once switched here
the page's choice wins over the env file. The keys are outside `app.*`: the fleet's export and
import (proxy/appsettings.py) never carry them.

CHAT. On: an install that says `external` forwards /v1/chat-app/* to QSE_CHAT_URL again; any
other starts this controller's own chatd (proxy/appchat.py, <state dir>/chatd.env) and its watcher.
Without a sandbox runtime chat runs without its sandbox tools (files, shell, browser): `sandbox`
says so. Off: this controller's chatd is stopped as a hand stop (its watcher holds it; a run open
then ends `interrupted`, Continue resumes it once Chat is on again), its data stays; every chat
route answers 503 `chat_off` (tandemapp/chat_forward.py). The MCP servers are chatd's: they run and
are listed only while Chat is on (`mcp`).

THE CONDUCTOR. On: its documents in TANDEM_CONDUCTOR's folder, or <state dir>/conductor when the
install said off (the shipped defaults are stored in a new folder); the harness runs in front of
the backends from the next request. Off: every backend is relayed as it is; the documents stay.
A decision model (Laya) runs only when the conductor's `decider` role names one.

THE ENGINE'S OWN MODE is the engine's setting (`tandem remote mode set engine|chat`), not this
controller's: `engines` lists the engine hosts paired over SSH, each switched with POST
/v1/fleet/engines/{id}/mode (proxy/engineonboard.py); an engine reached only over HTTP is switched
on its own host.

THE CONFIRM is the plan's: a digest of the part, the target and the state it was planned against,
keyed with this process's random secret. Applying with a confirm that does not match the state now
is 409 `stale_plan`; asking for the state that already holds changes nothing (`changed` false).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading

CONTRACT = "1.0"
PARTS = ("chat", "conductor")
KEYS = {"chat": "parts.chat", "conductor": "parts.conductor"}
CHAT_MODES = ("off", "external", "own")
SANDBOX_NONE_NOTE = ("No sandbox runtime on this host: chats run without their sandbox tools "
                     "(files, shell, browser).")


class PartsError(Exception):
    def __init__(self, status: int, kind: str, message: str):
        super().__init__(message)
        self.status, self.kind, self.message = status, kind, message

    def body(self) -> dict:
        return {"error": {"type": self.kind, "message": self.message}}


def stored(cfg, part: str) -> bool | None:
    """The page's switch for `part` (True on, False off), None while it was never switched."""
    v = cfg.setting(KEYS[part])
    return {"on": True, "off": False}.get(v) if isinstance(v, str) else None


def chat_on(cfg, install: str) -> bool:
    s = stored(cfg, "chat")
    return (install != "off") if s is None else s


def chat_runner(install: str) -> str:
    """Who runs chatd once Chat is on: `external` stays external, anything else is this
    controller's own chatd."""
    return "external" if install == "external" else "own"


def conductor_root(cfg, install: str | None, state_dir: str) -> str | None:
    """The conductor's folder while it is on, None while it is off."""
    s = stored(cfg, "conductor")
    folder = install if install and install != "off" else None
    if s is None:
        return folder
    if not s:
        return None
    return folder or os.path.join(state_dir, "conductor")


class Parts:
    """The switches. `app` is the controller's side (proxy/app.py `AppSide`):

        app.chat_status() -> {running, url, data, sandbox, sandbox_path}
        app.set_chat(on: bool, runner: str, who: str) -> str      what happened, in words
        app.set_conductor(root: str | None, who: str) -> str
        app.backends() -> [{id, label, kind, enabled, env}]
        app.engines() -> [{id, alias}]
    """

    def __init__(self, cfg, *, install_chat: str, install_conductor: str | None,
                 state_dir: str, app, log=lambda m: print(m, flush=True)):
        if install_chat not in CHAT_MODES:
            raise ValueError(f"install_chat is one of {', '.join(CHAT_MODES)}")
        self.cfg = cfg
        self.install_chat = install_chat
        self.install_conductor = install_conductor if install_conductor not in ("", None) \
            else "off"
        self.state_dir = state_dir
        self.app = app
        self.log = log
        self._secret = secrets.token_bytes(32)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ the state
    def chat_on(self) -> bool:
        return chat_on(self.cfg, self.install_chat)

    def conductor_dir(self) -> str | None:
        return conductor_root(self.cfg, self.install_conductor, self.state_dir)

    def _state(self, part: str) -> str:
        if part == "chat":
            return "on" if self.chat_on() else "off"
        return self.conductor_dir() or "off"

    def _confirm(self, part: str, on: bool) -> str:
        msg = f"{part}\0{int(on)}\0{self._state(part)}".encode()
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()[:32]

    def view(self) -> dict:
        on = self.chat_on()
        runner = chat_runner(self.install_chat) if on else None
        st = self.app.chat_status() if on else {}
        sandbox = st.get("sandbox") if runner == "own" else None
        note = None
        if not on:
            note = "Chat is off: no chatd runs here and the Chat tab says so."
        elif runner == "external":
            note = f"Chat is another host's chatd ({st.get('url') or 'QSE_CHAT_URL'})."
        elif sandbox == "none":
            note = SANDBOX_NONE_NOTE
        cdir = self.conductor_dir()
        return {
            "contract_version": CONTRACT,
            "chat": {"on": on, "switched": stored(self.cfg, "chat") is not None,
                     "install": self.install_chat, "runner": runner,
                     "running": st.get("running") if runner == "own" else None,
                     "sandbox": sandbox, "note": note},
            "conductor": {"on": cdir is not None,
                          "switched": stored(self.cfg, "conductor") is not None,
                          "install": "off" if self.install_conductor == "off" else "on",
                          "dir": cdir},
            "engines": [{"id": e["id"], "alias": e["alias"]} for e in self.app.engines()],
            "backends": [{"id": b["id"], "label": b.get("label"), "kind": b["kind"],
                          "enabled": bool(b["enabled"]), "env": bool(b.get("env"))}
                         for b in self.app.backends()],
            "mcp": {"where": "chat", "available": on},
        }

    # ------------------------------------------------------------------ the plan
    def changes(self, part: str, on: bool) -> list[str]:
        if part == "chat":
            runner = chat_runner(self.install_chat)
            st = self.app.chat_status()
            if on and runner == "external":
                return [f"The Chat tab forwards to the chatd at {st.get('url')} again.",
                        "Nothing starts on this host."]
            if on:
                out = [f"chatd starts on this host and is watched here; its chats are kept in "
                       f"{st.get('data')}.",
                       "The Chat tab opens. Chat's requests go through this gateway and count "
                       "in its usage ledger."]
                sb = st.get("sandbox")
                out.append(SANDBOX_NONE_NOTE if sb == "none" else
                           "Each chat's sandbox runs as a pod in this namespace." if
                           sb == "kubernetes" else
                           f"Each chat's sandbox runs in the container runtime at "
                           f"{st.get('sandbox_path')}.")
                return out
            if runner == "external":
                return [f"The Chat tab stops forwarding to {st.get('url')}; the chatd there "
                        f"keeps running.", "The Chat tab says Chat is off."]
            return ["chatd stops. A chat that is answering now ends interrupted; Continue "
                    "resumes it once Chat is on again.",
                    f"The chats stay in {st.get('data')}.",
                    "The Chat tab says Chat is off, and its MCP servers stop with chatd."]
        cdir = conductor_root(_Fake({KEYS["conductor"]: "on"}), self.install_conductor,
                              self.state_dir)
        if on:
            return ["The conductor runs in front of the backends from the next request: "
                    "profiles, the router, packages and notes.",
                    f"Its documents are in {cdir}; a new folder gets the shipped defaults.",
                    "A decision model runs only when the conductor's decider role names one.",
                    "The engines' own chat mode is a separate switch on each engine host."]
        return ["Every backend is relayed as it is: no profiles, router, packages or notes.",
                f"The documents stay in {self.conductor_dir()}.",
                "The engines' own chat mode is a separate switch on each engine host."]

    def plan(self, part: str, on: bool) -> dict:
        now = self._state(part)
        cur = (now == "on") if part == "chat" else (now != "off")
        if cur == on:
            return {"contract_version": CONTRACT, "part": part, "on": on, "changed": False,
                    "needs_confirm": False, "confirm": None, "changes": [
                        f"{'Chat' if part == 'chat' else 'The conductor'} is "
                        f"{'on' if on else 'off'} already: nothing changes."]}
        return {"contract_version": CONTRACT, "part": part, "on": on, "changed": True,
                "needs_confirm": True, "confirm": self._confirm(part, on),
                "changes": self.changes(part, on)}

    # ------------------------------------------------------------------ the switch
    def switch(self, part: str, body, who: str) -> dict:
        if part not in PARTS:
            raise PartsError(404, "not_found", f"no part {part!r}: chat or conductor")
        if not isinstance(body, dict) or set(body) - {"on", "confirm"} or \
                not isinstance(body.get("on"), bool) or \
                ("confirm" in body and not isinstance(body["confirm"], str)):
            raise PartsError(400, "bad_request", "the body is {on: true|false, confirm?}")
        on = body["on"]
        with self._lock:
            p = self.plan(part, on)
            if "confirm" not in body or not p["changed"]:
                return p
            if not hmac.compare_digest(body["confirm"], p["confirm"]):
                raise PartsError(409, "stale_plan", "the state changed since this plan, or the "
                                                    "confirm is not its: plan again")
            word = "on" if on else "off"
            if part == "chat":
                said = self.app.set_chat(on, chat_runner(self.install_chat), who)
                self.cfg.set_setting(KEYS["chat"], word, who=who)
                text = f"Chat switched {word}"
            else:
                root = conductor_root(_Fake({KEYS["conductor"]: word}),
                                      self.install_conductor, self.state_dir)
                said = self.app.set_conductor(root, who)
                self.cfg.set_setting(KEYS["conductor"], word, who=who)
                text = f"the conductor switched {word}"
            self.cfg.log_audit(who, "switch", "part", part, detail={"text": text, "said": said})
            self.log(f"[parts] {text} by {who}: {said}")
        out = self.view()
        out["done"] = {"part": part, "on": on, "said": said}
        return out

    # ------------------------------------------------------------------ the routes
    def handle(self, method: str, parts: list[str], body, who: str) -> tuple[int, dict]:
        """`parts` from /v1/fleet: ["app", "parts", ...]."""
        sub = parts[2:]
        try:
            if method == "GET" and not sub:
                return 200, self.view()
            if method == "POST" and len(sub) == 1:
                return 200, self.switch(sub[0], body, who)
            raise PartsError(404, "not_found",
                             f"no route {method} /v1/fleet/{'/'.join(parts)}")
        except PartsError as exc:
            return exc.status, exc.body()


class _Fake:
    """A settings reader that answers one key: the state a switch would make."""

    def __init__(self, values: dict):
        self.values = values

    def setting(self, key, default=None):
        return self.values.get(key, default)
