"""API exposure (SRV-871), with TANDEM_AUTH=accounts only: one switch per protocol saying whether
other devices may use it with a token.

    api.openai.exposure, api.anthropic.exposure     this-machine (the default) | network

    this-machine   service identities, a signed-in page (the Playground) and callers on this
                   machine: tokenless in local mode, with a token otherwise
    network        the above, plus any device with a token that holds the protocol's scope

No value lets another device in without a token: the request gate answers 401 before this rule
runs. The rule runs in the gate for an API token only (AccountsAuth.exposure), after the token's
scope was checked, so it only ever narrows a token that is valid already. "On this machine" is
proxy/authlocal.py this_machine (a loopback peer, a loopback Host, no forwarding header); in a
container or on Kubernetes nothing counts as this machine, so a token caller there needs network.
A call from another device while the switch is this-machine is 403 `api_not_exposed`, in the
protocol's own error shape (proxy/tokens.py refusal). The rule is per route; one listener stays.

WHICH ROUTES. A route whose token scope (tokens.scope_for) is an inference scope: the OpenAI
routes, the cache routes and the gateway (openai), /v1/messages and its count_tokens (anthropic),
/v1/models (either: open when one of the protocols the token holds is exposed). Management routes
(settings, fleet, metrics, the ledger's views) follow the token's scopes alone.

THE FIRST VALUE. An install whose migration imported legacy API keys (auth_state
`had_legacy_keys`) starts with openai at network, so its clients keep working (decision 7 of
2026-11-16); anthropic, and every other install, at this-machine. A value set on the page wins.

SINGLE COMPUTER. The switch is the rule plus the listen address: `api.listen` (the api settings
scope, proxy/settingscopes.py) writes TANDEM_HOST to the env file, applied at the next restart.
KUBERNETES. The pod listens on all addresses; the switch is the rule. The chart says which
protocols its ingress routes in TANDEM_INGRESS_API (a comma list: openai, anthropic; unset: not
known, the chart's part is OPS-298).

FINDINGS (proxy/fleethealth.py, codes of proxy/enginecodes.py), each with the facts it names:
    exposure_no_tokens     a protocol is network and no live token holds its scope   {protocol}
    exposure_unreachable   network, but the listener is on loopback or the ingress does not
                           route it                                         {protocol, reason}
    exposure_plain_http    a token came from another device without https, within the last
                           PLAIN_HTTP_S (it clears a day after the last one)
"""

from __future__ import annotations

import time

from proxy import authlocal
from proxy import tokens as tokens_mod

PROTOCOLS = ("openai", "anthropic")
NAMES = {"openai": "OpenAI", "anthropic": "Anthropic"}
VALUES = ("this-machine", "network")
INGRESS_ENV = "TANDEM_INGRESS_API"
PLAIN_HTTP_S = 86_400.0
NOT_EXPOSED = "Other devices cannot use this API. Switch it on under API exposure."
LOOPBACK_ONLY = "listening on this machine only"
INGRESS_BLOCKS = "the ingress does not route it"


def key(protocol: str) -> str:
    return f"api.{protocol}.exposure"


def protocols_of(method: str, path: str, scopes) -> tuple:
    """The protocols a route serves for a token holding `scopes` (none: not a protocol route)."""
    need = tokens_mod.scope_for(method, path) or ()
    got = tuple(s.split(":", 1)[1] for s in need if s in tokens_mod.INFERENCE)
    if len(got) > 1:
        held = tuple(p for p in got if f"inference:{p}" in (scopes or ()))
        got = held or got
    return got


class Exposure:
    """AccountsAuth.exposure: (principal, method, path, peer, headers) -> refusal | None."""

    def __init__(self, auth, *, clock=time.time):
        self.auth = auth
        self.clock = clock
        self.plain_http: float | None = None        # the last token from another device on http

    # ------------------------------------------------------------------ the switches
    def default(self, protocol: str) -> str:
        if protocol == "openai" and self.auth.store.state("had_legacy_keys"):
            return "network"
        return "this-machine"

    def value(self, protocol: str) -> str:
        v = self.auth.policy.get(key(protocol))
        return v if v in VALUES else self.default(protocol)

    def here(self, peer: str, headers) -> bool:
        """A caller on this machine: never in a container or on Kubernetes."""
        if authlocal.deploy_kind(self.auth.env, self.auth.local.exists) != "bare":
            return False
        return authlocal.this_machine(peer, headers)

    # ------------------------------------------------------------------ the rule
    def __call__(self, p, method: str, path: str, peer: str, headers):
        if p is None or p.kind == "service":
            return None
        here = self.here(peer, headers)
        if not here and not self.auth.secure(peer, headers):
            self.plain_http = self.clock()
        protos = protocols_of(method, path, p.scopes)
        if not protos or here or any(self.value(x) == "network" for x in protos):
            return None
        return 403, "api_not_exposed", NOT_EXPOSED

    # ------------------------------------------------------------------ findings
    def unreachable(self, protocol: str) -> str | None:
        bind = self.auth.local.bind
        if bind is not None and authlocal.loopback_bind(bind):
            return LOOPBACK_ONLY
        ingress = self.auth.env.get(INGRESS_ENV)
        if ingress is not None:
            routed = {x.strip().lower() for x in ingress.split(",") if x.strip()}
            if protocol not in routed:
                return INGRESS_BLOCKS
        return None

    def findings(self) -> list[dict]:
        out: list[dict] = []
        live = None
        for proto in PROTOCOLS:
            if self.value(proto) != "network":
                continue
            if live is None:
                live = self._live_scopes()
            if f"inference:{proto}" not in live:
                out.append({"code": "exposure_no_tokens", "facts": {"protocol": NAMES[proto]}})
            why = self.unreachable(proto)
            if why:
                out.append({"code": "exposure_unreachable",
                            "facts": {"protocol": NAMES[proto], "reason": why}})
        if self.plain_http is not None and self.clock() - self.plain_http < PLAIN_HTTP_S:
            out.append({"code": "exposure_plain_http", "facts": {}})
        return out

    def _live_scopes(self) -> set:
        ts = self.auth.tokens
        got: set = set()
        for row in ts.rows("WHERE kind != 'service' AND revoked_at IS NULL"):
            if ts.state(row) == "active":
                got.update(row["scopes"])
        return got
