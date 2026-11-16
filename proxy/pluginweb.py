"""A running plugin's web routes on the controller (SRV-859, proxy/pluginhost.py).

    GET  /plugin-panels/<plugin>/<panel>/[file]
                       a panel's HTML (no file) or a file beside it, under the dashboard's rule.
                       Every answer carries `Content-Security-Policy: sandbox allow-scripts ...`:
                       opened in the page's iframe or on its own, the panel has an opaque origin,
                       so it reads no cookie, storage or route of the controller's; it talks to
                       the page only through the bridge (/v1/fleet/plugins/<id>/bridge, which
                       the page relays and the host checks against the plugin's capabilities)
    *    /plugin-backends/<plugin>/<backend>/...
                       a plugin's backend as the gateway's relay reaches it: the relay's own
                       in-process secret (SRV-869, checked by the gate), from this machine's
                       loopback only; GET /v1/models answers the manifest's models, any other request
                       goes to the plugin as backend/request
"""

from __future__ import annotations

import json
import mimetypes
import os
from urllib.parse import unquote

from proxy import config as config_mod
from proxy.pluginhost import PANEL_CSP
from proxy.pluginstore import PluginError

PANELS = "/plugin-panels/"
BACKENDS = "/plugin-backends/"
FILE_MAX = 8 << 20


def _err(handler, status: int, kind: str, message: str) -> None:
    handler._json(status, {"error": {"type": kind, "message": message}},
                  extra_headers=(("Cache-Control", "no-store"),))


def panel(handler, svc, path: str) -> None:
    if not handler._allowed("dashboard"):
        return None
    parts = [unquote(x) for x in path[len(PANELS):].split("/")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return _err(handler, 404, "not_found", "no such panel")
    pid, name, rel = parts[0], parts[1], "/".join(parts[2:])
    host = getattr(svc, "plugin_host", None)
    full = host.panel_file(pid, name, rel) if host is not None else None
    if full is None:
        return _err(handler, 404, "not_found", f"no panel {name} of a running plugin {pid}")
    try:
        if os.path.getsize(full) > FILE_MAX:
            return _err(handler, 413, "too_large", "a panel's file is at most 8 MiB")
        with open(full, "rb") as f:
            data = f.read()
    except OSError:
        return _err(handler, 404, "not_found", "the panel's file cannot be read")
    ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
        ctype += "; charset=utf-8"
    handler.send_response(200)
    for k, v in (("Content-Type", ctype), ("Content-Length", str(len(data))),
                 ("Content-Security-Policy", PANEL_CSP),
                 ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
                 ("Cross-Origin-Resource-Policy", "same-origin"),
                 ("Cache-Control", "no-store")):
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(data)
    return None


def backend(handler, svc, method: str, path: str, raw: bytes | None) -> None:
    if not config_mod.is_loopback_host(handler._peer()):
        return _err(handler, 403, "forbidden", "a plugin's backend is reached through this "
                                               "controller's gateway, not directly")
    parts = path[len(BACKENDS):].split("/", 2)
    if len(parts) < 2:
        return _err(handler, 404, "not_found", "no such backend")
    pid, name = unquote(parts[0]), unquote(parts[1])
    rest = "/" + (parts[2] if len(parts) > 2 else "")
    host = getattr(svc, "plugin_host", None)
    b = host.backend_of(pid, name) if host is not None else None
    if b is None or not host.running(pid):
        return _err(handler, 503, "plugin_down", f"the plugin {pid} is not running")
    if method == "GET" and rest.rstrip("/") in ("/v1/models", "/models"):
        return handler._json(200, {"object": "list", "data": [
            {"id": m, "object": "model", "owned_by": f"plugin:{pid}"}
            for m in b.get("models") or []]})
    if method == "GET" and rest.rstrip("/") in ("/health", ""):
        return handler._json(200, {"status": "ok"})
    body = None
    if raw:
        try:
            body = json.loads(raw)
        except (ValueError, RecursionError):
            return _err(handler, 400, "invalid_request_error", "the body is not JSON")
    try:
        r = host.backend_request(pid, name, method, rest, body)
    except PluginError as exc:
        return handler._json(exc.status, exc.body())
    status = r["status"] if 100 <= r["status"] <= 599 else 502
    if isinstance(r.get("events"), list):
        handler.send_response(status)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.close_connection = True
        for ev in r["events"]:
            handler.wfile.write(b"data: " + json.dumps(ev, separators=(",", ":")).encode() +
                                b"\n\n")
        handler.wfile.write(b"data: [DONE]\n\n")
        handler.wfile.flush()
        return None
    return handler._json(status, r.get("body") if r.get("body") is not None else {})
