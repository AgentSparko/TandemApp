"""What went wrong with an engine host, in words a page can show (SRV-827..833), and what the
health page finds (SRV-835, proxy/fleethealth.py): one table.

    raise enginecodes.error("wrong_code", tries_left=3)
    enginecodes.finding("key_not_installed", user="u", host="h")   a 200 answer's finding
    enginecodes.CATALOG                                            code -> (status, message, fix)

Every refusal of /v1/fleet/engines/* is one code from CATALOG with its HTTP status, ONE plain
sentence (`message`) and what to do about it (`fix`), both filled from the facts the call knows
(the address and port tried, the fingerprints on both sides, the tries left, the seconds to wait).
`detail` is the other side's own words when it gave some (the engine's message, ssh's last line),
already scrubbed by whoever read them. contracts/fleet-v1/engine-codes.json is this table for the
page; tests/test_engine_pair.py holds the two equal.

A FACT is never a secret: the password, a private key, the HTTP token and the pairing code are
never an argument here, and `_fill` takes only the names in FACTS.
"""

from __future__ import annotations

import string

#: code -> (HTTP status, the sentence, the fix); {name} is a fact, filled when the call knows it
CATALOG: dict[str, tuple[int, str, str]] = {
    "bad_request": (400, "The request is not valid: {detail}",
                    "Send the fields the page asks for."),
    "not_found": (404, "There is no such engine host or pairing here.",
                  "Start again from Add engine."),
    "too_many": (429, "Too many pairing tries from this TandemApp.",
                 "Wait a minute and try again."),
    # finding the engine
    "lan_denied": (409, "This TandemApp may not use the local network, so it found no engine.",
                   "Allow it under System Settings, Privacy & Security, Local Network, or type the "
                   "engine's address."),
    "lan_failed": (502, "The search on the local network failed: {detail}",
                   "Type the engine's address instead, a tailnet name or address included."),
    "unreachable": (502, "TandemApp cannot reach the engine at {address}:{port}.",
                    "Check that the engine host is on, that the engine runs (`bin/tandem start` "
                    "there), and that the address and port are right."),
    "not_an_engine": (502, "{address}:{port} answers, but not as a TandemLLM engine.",
                      "Check the address and port: the engine's port is 8000 unless its install "
                      "chose another."),
    "not_allowed": (403, "The engine at {address}:{port} refuses this machine's address.",
                    "Add this machine's address to QSE_ALLOW_FROM on the engine host, or clear "
                    "that setting."),
    # pairing by code
    "pairing_unsupported": (409, "The engine at {address}:{port} is too old to pair by code.",
                            "Update the engine on its host, or connect with a password or a key "
                            "under Other ways."),
    "no_window": (409, "No pairing code is showing on the engine host.",
                  "Run `tandem pair` on the engine host and type the code it prints."),
    "code_spent": (410, "This code cannot be used any more: it expired, was used, or had five "
                        "wrong tries.",
                   "Run `tandem pair` on the engine host again for a new code."),
    "bad_code": (400, "A pairing code is 6 digits.",
                 "Type the 6 digits `tandem pair` printed on the engine host."),
    "wrong_code": (403, "The code does not match: {tries_left} tries left.",
                   "Type the code the engine host shows; after five wrong tries run `tandem pair` "
                   "there again."),
    "host_key_mismatch": (409, "The engine's SSH host key is {offered}, not the {expected} shown "
                               "for it.",
                          "Compare it with the fingerprint `tandem pair` printed on the engine "
                          "host: if they differ, something on the network changed the answer; do "
                          "not pair."),
    "rate_limited": (429, "The engine host takes no more pairing tries from here for now.",
                     "Wait {retry_after} seconds, then type the code again."),
    "key_present": (409, "TandemApp's key is already in the engine host's authorized_keys with "
                         "another line.",
                    "Remove that line on the engine host, then pair again."),
    "key_refused": (409, "The engine host could not add TandemApp's key: {detail}",
                    "Fix ~/.ssh on the engine host as the message says (the user's own, not a "
                    "link, not writable by others), then pair again."),
    "engine_failed": (502, "The engine host could not finish: {detail}",
                      "Try again; if it fails again, run `tandem doctor` on the engine host."),
    # SSH
    "ssh_unreachable": (502, "TandemApp cannot reach SSH on {host} port {port}.",
                        "Check that the SSH server runs on the engine host and that port {port} "
                        "is open from this machine, then retry."),
    "host_key_changed": (409, "The engine host's SSH host key changed: it is {offered} now, "
                              "{pinned} before.",
                         "If the engine host was reinstalled, pair it again by code (`tandem "
                         "pair`), which proves the new key; otherwise do not connect."),
    "host_key_unconfirmed": (409, "The SSH host key {offered} was not confirmed.",
                             "Compare it with the engine host's own (`ssh-keygen -lf "
                             "/etc/ssh/ssh_host_ed25519_key.pub` there) and send it back to "
                             "confirm."),
    "auth_failed": (403, "The engine host did not accept that user name and password.",
                    "Check the user name and password for {host} and try again."),
    "ssh_failed": (502, "The SSH login to {host} failed: {detail}",
                   "Check the address, port and user, then retry."),
    "key_not_installed": (409, "The engine host does not take TandemApp's key yet.",
                          "Add the line shown to ~/.ssh/authorized_keys of {user} on {host}, then "
                          "check again."),
    "not_paired": (409, "The engine host no longer takes TandemApp's key.",
                   "Pair it again by code (`tandem pair` on the engine host)."),
    "not_installed": (409, "No TandemLLM engine is installed at {dir} on {host}.",
                      "Install the engine there first, or give the folder it is installed in."),
    "bad_engine_dir": (409, "The engine's folder on {host} has characters a key line cannot hold.",
                       "Install the engine in a folder whose path has only letters, digits and "
                       ". _ / + @ -."),
    # control and HTTP after pairing
    "engine_too_old": (409, "The engine on {host} is too old for TandemApp's control.",
                       "Upgrade the engine on its host."),
    "ctl_failed": (502, "The engine host's control command failed: {detail}",
                   "Run `tandem doctor` on the engine host."),
    "engine_down": (503, "The engine is not running.",
                    "Start it."),
    "http_blocked": (502, "TandemApp reaches the engine host over SSH but not the engine's port "
                          "{port}.",
                     "Open port {port} on the engine host's firewall for this machine; test it "
                     "through SSH."),
    "token_missing": (409, "TandemApp holds no HTTP token for this engine.",
                      "Rotate the token; TandemApp does it over SSH."),
    "token_refused": (401, "The engine refused TandemApp's HTTP token.",
                      "Rotate the token; TandemApp does it over SSH."),
    "setup_locked": (403, "The engine's Setup Guide wants its admin password.",
                     "Remove the engine's admin password or use its own Setup page; TandemApp's "
                     "token does not open the Setup Guide."),
    "busy": (409, "The engine is busy: {detail}",
             "Wait until the running job or requests end, then try again."),
    "setup_refused": (409, "The engine's Setup Guide refused: {detail}",
                      "Change the choice as the message says."),
    "unknown_model": (404, "The engine does not offer the model {model}.",
                      "Pick a model from the list."),
    "not_ssh": (409, "{node} is not an engine host TandemApp reaches over SSH.",
                "Pair the engine host by code under Add engine."),
    # the health page (SRV-835): an engine host's state, then the controller's own
    "engine_loading": (503, "The engine on {host} is {state} and does not serve yet.",
                       "Wait until it serves; a large model takes some minutes to load."),
    "no_model": (409, "The engine on {host} serves no model yet.",
                 "Pick a model in Add engine's model step; TandemApp downloads it there."),
    "engine_api_unsupported": (409, "The engine on {host} speaks engine API {engine_api}; this "
                                    "TandemApp speaks {speaks}.",
                               "Install the TandemApp release made for that engine, or the engine "
                               "release this TandemApp names."),
    "no_engine": (409, "No engine is connected yet.",
                  "Install the engine on its host, then pair it under Add engine."),
    "chatd_down": (503, "chatd, the Chat tab's service, is not running.",
                   "Restart it; if it stops again, its log says why ({path})."),
    "chatd_silent": (503, "chatd runs but does not answer.",
                     "Restart it; if it hangs again, its log says why ({path})."),
    "chat_unreachable": (502, "The Chat tab's service at {address} does not answer.",
                         "Start chatd where it runs, or let TandemApp run it here "
                         "(TANDEM_CHAT=own in app.env)."),
    "sandbox_off": (409, "Chat sandboxes are off: no container runtime socket is at {path}.",
                    "Give chatd a socket: install with --with-sandbox, set QSE_SANDBOX_SOCKET in "
                    "chatd.env, or mount a socket into TandemApp's container."),
    "sandbox_unreachable": (502, "The container runtime at {path} does not answer.",
                            "Check that its service runs and that this user may open the socket."),
    "sandbox_no_image": (409, "The sandbox image {image} is not in the container runtime.",
                         "Build it as the runtime's user: bash ops/sandbox/build.sh podman."),
    "no_memory_cgroup": (409, "The kernel does not cap a container's memory here, so a sandbox "
                              "has no memory limit.",
                         "Turn the memory controller on: on a Raspberry Pi add "
                         "cgroup_enable=memory cgroup_memory=1 to /boot/firmware/cmdline.txt and "
                         "reboot."),
    "disk_low": (507, "Only {free_gb} GB are free at {path}.",
                 "Free some space there: TandemApp wants {need_gb} GB."),
    "backend_down": (502, "The backend {backend} does not answer: {detail}",
                     "Check that it runs and that its address and key are right; Backends tests "
                     "it."),
    # the plugin host (SRV-859)
    "plugin_failed": (409, "The plugin {plugin} does not run: {detail}",
                      "Its page's log says why; fix it, then enable or start it again."),
    "plugin_no_sandbox": (409, "The plugin {plugin} waits for a sandbox: {detail}",
                          "Give TandemApp the container runtime chatd uses (QSE_SANDBOX_SOCKET "
                          "in chatd.env, or the Kubernetes sandbox), or set "
                          "TANDEM_PLUGIN_SANDBOX."),
    # plugin signing and trust (SEC-18)
    "plugin_tampered": (409, "The plugin {plugin} changed since its install: {detail}",
                        "Install it again from its source; do not enable a plugin whose files "
                        "changed under it."),
    "plugin_untrusted": (409, "The plugin {plugin} is not trusted to run as it asks: {detail}",
                         "Its trust page says what a grant takes: a trusted publisher key or its "
                         "id typed."),
    "not_offered": (409, "{node} does not need that fix now.",
                    "Check again, then use a button the health page shows."),
    "too_many_fixes": (429, "{node} had too many fixes in the last minute.",
                       "Wait {retry_after} seconds, then try again."),
}

#: the facts a sentence may name (never a secret)
FACTS = ("address", "port", "host", "user", "dir", "tries_left", "state", "retry_after",
         "expected", "offered", "pinned", "model", "job", "node", "alias",
         # the health page (SRV-835)
         "path", "image", "free_gb", "need_gb", "backend", "engine_api", "speaks",
         # the plugin host (SRV-859)
         "plugin")
_GENERIC = {"tries_left": "some", "retry_after": "60", "port": "its port", "address": "the address",
            "host": "the engine host", "user": "the user", "dir": "the engine's folder",
            "detail": "no reason given", "expected": "one", "offered": "another",
            "pinned": "another", "model": "asked for", "node": "This node",
            "state": "starting", "path": "its folder", "image": "tandem-sandbox:1", "free_gb": "a few", "need_gb": "5",
            "backend": "named", "engine_api": "another version", "speaks": "another",
            "plugin": "named"}


class _Facts(dict):
    def __missing__(self, key):
        return _GENERIC.get(key, "?")


def _fill(text: str, facts: dict) -> str:
    keys = {f for _, f, _, _ in string.Formatter().parse(text) if f}
    vals = _Facts({k: facts[k] for k in keys if facts.get(k) is not None})
    return text.format_map(vals)


class OnboardError(Exception):
    """One refusal: `code` from CATALOG, its status, the sentence, the fix and the facts."""

    def __init__(self, code: str, detail: str | None = None, *, status: int | None = None, **facts):
        if code not in CATALOG:
            raise KeyError(f"no engine code {code!r}")
        st, msg, fix = CATALOG[code]
        self.code, self.status = code, status or st
        self.detail = (str(detail).strip()[:300] or None) if detail else None
        self.facts = {k: v for k, v in facts.items() if k in FACTS and v is not None}
        f = dict(self.facts, detail=self.detail)
        self.message, self.fix = _fill(msg, f), _fill(fix, f)
        super().__init__(self.message)

    def finding(self) -> dict:
        out = {"code": self.code, "message": self.message, "fix": self.fix}
        if self.detail:
            out["detail"] = self.detail
        return {**out, **self.facts}

    def body(self) -> dict:
        return {"error": {"type": self.code, **self.finding()}}


def error(code: str, detail: str | None = None, **facts) -> OnboardError:
    return OnboardError(code, detail, **facts)


def finding(code: str, detail: str | None = None, **facts) -> dict:
    return OnboardError(code, detail, **facts).finding()
