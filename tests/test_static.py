"""The page's static files under /dashboard/ (tandemapp/static.py), on a CPU (ENG-413 WP14: moved
from the engine, which serves no page any more).

The redirect, the placeholder with no build, content types, the cache rules (index no-cache,
hashed assets immutable), the client-route fallback, the CSP, and no way out of the directory:
`..`, its URL-encoded form, a symlink. The shell is public: it holds no data, and `serve` reads
no header, so no token changes its answer.

Run: python tests/test_static.py
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tandemapp import static  # noqa: E402


class _Handler:
    """What static.serve uses of a BaseHTTPRequestHandler: the status line, the headers, wfile."""

    def __init__(self, headers: dict | None = None):
        self.headers = dict(headers or {})
        self.wfile = io.BytesIO()
        self._head: list[str] = []
        self.close_connection = False

    def send_response(self, code: int) -> None:
        self._head.append(f"HTTP/1.1 {code}")

    def send_header(self, k: str, v: str) -> None:
        self._head.append(f"{k}: {v}")

    def end_headers(self) -> None:
        pass


def _dist() -> str:
    root = tempfile.mkdtemp(prefix="tandemapp-dist-")
    d = os.path.join(root, "dist")
    os.makedirs(os.path.join(d, "assets"))
    open(os.path.join(d, "index.html"), "w").write(
        "<!doctype html><title>dash</title><script>document.documentElement.dataset.theme="
        "localStorage.theme||'dark'</script><script type=\"module\" src=\"./assets/index-3f9a1c0e.js\">"
        "</script>")
    open(os.path.join(d, "assets", "index-3f9a1c0e.js"), "w").write("console.log(1)")
    open(os.path.join(d, "assets", "index-Bx7_kQ2d.css"), "w").write("body{}")
    open(os.path.join(d, "favicon.svg"), "w").write("<svg/>")
    open(os.path.join(root, "secret.txt"), "w").write("outside")
    os.symlink(os.path.join(root, "secret.txt"), os.path.join(d, "link.txt"))
    return d


def get(path: str, dist: str, headers: dict | None = None):
    h = _Handler(headers)
    static.serve(h, path, dist)
    head = "\r\n".join(h._head)
    return int(h._head[0].split(" ", 2)[1]), head, h.wfile.getvalue()


def test_the_redirect_and_the_placeholder():
    code, head, _ = get("/dashboard", "/nonexistent/dist")
    assert code == 301 and "Location: /dashboard/" in head, head
    code, head, body = get("/dashboard/", "/nonexistent/dist")
    assert code == 200 and "text/html" in head and b"not built" in body


def test_files_types_and_caching():
    d = _dist()
    code, head, body = get("/dashboard/", d)
    assert code == 200 and body.startswith(b"<!doctype html>") and "Cache-Control: no-cache" in head
    inline = b"document.documentElement.dataset.theme=localStorage.theme||'dark'"
    h = base64.b64encode(hashlib.sha256(inline).digest()).decode()
    csp = [ln for ln in head.split("\r\n") if ln.startswith("Content-Security-Policy: ")][0]
    assert csp == ("Content-Security-Policy: default-src 'self'; connect-src 'self'; "
                   f"img-src 'self' data:; script-src 'self' 'sha256-{h}'; "
                   "style-src 'self' 'unsafe-inline'"), csp
    assert "unsafe-inline'" not in csp.split("script-src")[1].split(";")[0], "never for scripts"
    assert "X-Content-Type-Options: nosniff" in head
    code, head, body = get("/dashboard/assets/index-3f9a1c0e.js", d)
    assert code == 200 and "text/javascript" in head and "immutable" in head and body == \
        b"console.log(1)"
    code, head, _ = get("/dashboard/assets/index-Bx7_kQ2d.css?v=1", d)
    assert code == 200 and "text/css" in head and "immutable" in head
    code, head, _ = get("/dashboard/favicon.svg", d)
    assert code == 200 and "image/svg+xml" in head and "no-cache" in head
    code, head, body = get("/dashboard/usage/2026", d)
    assert code == 200 and b"<title>dash</title>" in body, "a client route gets the shell"
    code, _, _ = get("/dashboard/assets/missing-00000000.js", d)
    assert code == 404, "a missing asset is not the shell"


def test_nothing_outside_the_directory():
    d = _dist()
    for path in ("/dashboard/../secret.txt", "/dashboard/%2e%2e/secret.txt",
                 "/dashboard/assets/../../secret.txt", "/dashboard/link.txt",
                 "/dashboard/%2e%2e%2fsecret.txt", "/dashboard/..%5csecret.txt",
                 "/dashboard/a%00b.js"):
        code, _, body = get(path, d)
        assert code == 404 and b"outside" not in body, (path, code)


def test_the_shell_is_public_and_holds_no_data():
    d = _dist()
    proxy = {"X-Forwarded-For": "203.0.113.44", "X-Real-IP": "203.0.113.44"}
    for headers in (proxy, dict(proxy, Authorization="Bearer nope"), {}):
        code, _, body = get("/dashboard/", d, headers)       # through a proxy, no token or a wrong one
        assert code == 200 and b"<title>dash</title>" in body, headers
    with open(os.path.join(d, "index.html"), "rb") as f:
        assert get("/dashboard/", d)[2] == f.read(), "the shell is the built file and nothing else"


if __name__ == "__main__":
    from scriptmode import run
    run(globals())
