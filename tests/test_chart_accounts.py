"""OPS-298: the controller started with the environment the chart renders in accounts mode
(tests/test_chart_defaults.py renders it; here `python -m proxy serve` runs with it).

  Gherkin (the scenarios of OPS-298 that need a running controller):
  1  A new release: Given the chart's env and its Secret's setup code as the mounted file, When
     the controller is up, Then every route but /health and the page shell answers 401, from
     another device and from the pod's own loopback (what kubectl port-forward delivers), the
     chart's `helm test` script passes, And the code from the Secret makes the owner, once
                                                                 test_new_release
  2  An upgrade: Given the Secret of the release before holds its admin token, When the
     controller starts in accounts mode, Then it starts, the token works once as the setup code,
     and after the next start it is an API token for scripts (30 days) that never signs in
                                                                 test_upgrade
  3  A rollback: Given the upgraded state, When the legacy mode starts on it with the kept
     Secret's token in TANDEM_ADMIN_TOKEN (the old template), Then it starts and the token signs
     in as before                                                test_rollback
  5  API paths: /v1/models and /v1/messages answer 401 without a token (the ingress's 503 for a
     path set to false is the chart's: test_chart_defaults test_api_paths)
                                                                 test_new_release
  6  A leftover variable: Given extraEnv sets TANDEM_API_KEYS with a bad value, and
     auth.trustedProxy an entry that is neither an address nor a range, Then the controller
     serves /health (the probes) and the health page shows legacy_env_invalid and
     trusted_proxy_invalid naming TANDEM_TRUSTED_PROXY              test_leftover_variable
  7  The API through the ingress: Given the chart's defaults (the ingress routes the API paths)
     and both exposure switches at "this machine", When the signed-in page posts to
     /v1/chat/completions and /v1/messages through the ingress, Then each answers 200, And a
     token from another device gets 403 api_not_exposed on both; at "network" the token's call
     answers 200                                                  test_api_through_the_ingress
  the kubectl exec lines NOTES prints run as written against a volume laid out as the pod's
                                                                 test_notes_exec_lines
  TANDEM_ADMIN_TOKEN_FILE: 32 characters or more count as the admin token, named by its file; a
  shorter value is a setup code only; a missing file is nothing; the environment's own wins
                                                                 test_admin_token_file
  the docs: install-kubernetes.md, install-linux.md, the chart's README, app.env.example and the
  Dockerfile say accounts mode, the setup code, the exec reset, exposure and the trusted proxy;
  every auth.* and ingress.api.* value is in the README's table; the doc's kubectl exec lines are
  the ones NOTES prints                                          test_docs

helm on PATH (skipped without it). Run: python tests/test_chart_accounts.py
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scriptmode import Skip  # noqa: E402

PW = "the owner's long password"
OLD_TOKEN = "an-admin-token-of-the-release-before-0123456789abcdef"
MOUNT = "/etc/tandemapp/auth/setup-code"
#: what the server reads of the chart's env; the rest (paths in the pod) is the test's own
AUTH_VARS = ("TANDEM_AUTH", "TANDEM_DEPLOY", "TANDEM_SETUP_CODE_FILE", "TANDEM_ADMIN_TOKEN_FILE",
             "TANDEM_INGRESS_API", "TANDEM_TRUSTED_PROXY", "TANDEM_API_KEYS", "TANDEM_NODE_AGENT")


def _chart():
    import test_chart_defaults as tcd
    if shutil.which("helm") is None:
        raise Skip("helm is not on PATH")
    return tcd


def rendered(*sets: str, tcd=None) -> tuple[dict, str]:
    """(the controller's env the server reads, the Secret's value or '')."""
    tcd = tcd or _chart()
    rc, out = tcd.render("auth.mode=accounts", "ingress.enabled=true",
                         "ingress.host=app.example.com", *sets)
    assert rc == 0, out[-600:]
    env = {k: v for k, v in tcd.controller_env(out).items() if k in AUTH_VARS}
    assert None not in env.values(), "no valueFrom: no secret in the environment"
    value = tcd.secret_value(out) if "kind: Secret" in out else ""
    return env, value


def with_file(env: dict, value: str, d: str) -> dict:
    """The env with the mounted Secret's path moved to a file of the test's."""
    path = os.path.join(d, "setup-code")
    with open(path, "w") as f:
        f.write(value)
    os.chmod(path, 0o440)
    return {k: (path if v == MOUNT else v) for k, v in env.items()}


def serve(state: str, env: dict):
    import test_auth_setup as tas
    return tas._serve(state, env)


def ask(port, method, path, body=None, headers=None):
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Content-Type": "application/json", **(headers or {})}
    c.request(method, path, body=None if body is None else json.dumps(body), headers=h)
    r = c.getresponse()
    out = r.read()
    hdrs = {k: v for k, v in r.getheaders()}
    c.close()
    return r.status, hdrs, out


def _when(text: str) -> float:
    import datetime
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def stop(proc) -> None:
    proc.terminate()
    proc.wait(20)


def session(port, name="owner", password=PW) -> dict:
    st, h, out = ask(port, "POST", "/v1/auth/session", {"username": name, "password": password})
    assert st in (200, 201), (st, out[:200])
    return {"Cookie": h["Set-Cookie"].split(";", 1)[0], "X-Tandem-CSRF": json.loads(out)["csrf"]}


def findings(port, h) -> set:
    st, _, out = ask(port, "GET", "/v1/fleet/health/controller", headers=h)
    assert st == 200, (st, out[:200])
    return {f["code"] for f in json.loads(out)["findings"]}


CLOSED = (("GET", "/v1/settings"), ("GET", "/v1/fleet/nodes"), ("GET", "/v1/dashboard/summary"),
          ("GET", "/v1/models"), ("POST", "/v1/chat/completions"), ("POST", "/v1/messages"),
          ("POST", "/v1/messages/count_tokens"), ("GET", "/metrics"), ("GET", "/v1/auth/tokens"))


def test_new_release():
    tcd = _chart()
    env, code = rendered(tcd=tcd)
    assert len(code) == 20 and env["TANDEM_DEPLOY"] == "kubernetes"
    d = tempfile.mkdtemp(prefix="o1-new-")
    proc, port, lines = serve(os.path.join(d, "state"), with_file(env, code, d))
    try:
        assert not any("[auth] local mode" in x for x in lines), "never local mode"
        assert ask(port, "GET", "/health")[0] == 200
        # from the pod's own loopback, as kubectl port-forward arrives (127.0.0.1, a loopback
        # Host, no forwarding header), and as another device through the ingress
        for extra in ({}, {"Host": "app.example.com", "X-Forwarded-For": "198.51.100.7"}):
            for method, path in CLOSED:
                body = {"model": "m", "messages": []} if method == "POST" else None
                st = ask(port, method, path, body, extra)[0]
                assert st == 401, (extra, method, path, st)
        st, _, out = ask(port, "GET", "/v1/auth/session")
        assert st == 200 and json.loads(out)["local_mode"] is False, out[:200]
        # `helm test`: the chart's test pod's own script passes with no credential
        r = subprocess.run([sys.executable, "-c", hook_script(tcd, port)], capture_output=True,
                           text=True, timeout=60)
        assert r.returncode == 0 and "health ok" in r.stdout, r.stdout + r.stderr
        # the Secret's code makes the owner, once
        st, _, out = ask(port, "POST", "/v1/auth/setup", {"code": code, "username": "owner",
                                                         "password": PW})
        assert st == 201, out[:200]
        assert ask(port, "POST", "/v1/auth/setup", {"code": code, "username": "two",
                                                     "password": PW})[0] == 404
        h = session(port)
        assert ask(port, "GET", "/v1/settings", headers=h)[0] == 200
        got = findings(port, h)
        assert "bootstrap_secret_left" in got and "auth_setup_pending" not in got, got
    finally:
        stop(proc)
    return "401 everywhere but /health, from loopback too; the Secret's code makes the owner once"


def test_upgrade():
    tcd = _chart()
    env, _ = rendered(tcd=tcd)
    assert env["TANDEM_ADMIN_TOKEN_FILE"] == MOUNT
    d = tempfile.mkdtemp(prefix="o1-up-")
    state = os.path.join(d, "state")
    env = with_file(env, OLD_TOKEN, d)       # the kept Secret: the release before's admin token
    proc, port, lines = serve(state, env)
    try:
        assert ask(port, "GET", "/v1/settings", headers={
            "Authorization": f"Bearer {OLD_TOKEN}"})[0] == 401, "no owner: nothing yet"
        st, _, out = ask(port, "POST", "/v1/auth/setup", {"code": OLD_TOKEN, "username": "owner",
                                                         "password": PW})
        assert st == 201, out[:200]
        assert ask(port, "POST", "/v1/auth/setup", {"code": OLD_TOKEN, "username": "two",
                                                     "password": PW})[0] == 404
    finally:
        stop(proc)
    proc, port, lines = serve(state, env)
    try:
        bearer = {"Authorization": f"Bearer {OLD_TOKEN}"}
        assert ask(port, "GET", "/v1/settings", headers=bearer)[0] == 200, "a token for scripts"
        assert ask(port, "POST", "/v1/auth/session", {"username": "owner",
                                                      "password": OLD_TOKEN})[0] == 401
        h = session(port)
        st, _, out = ask(port, "GET", "/v1/auth/tokens", headers=h)
        rows = [r for r in json.loads(out)["tokens"] if r.get("source") == "admin_token"]
        assert len(rows) == 1 and rows[0]["needs_review"], rows
        days = (_when(rows[0]["expires_at"]) - _when(rows[0]["created_at"])) / 86_400
        assert 29.9 < days <= 30.01, days
        got = findings(port, h)
        assert {"legacy_admin_token", "bootstrap_secret_left"} <= got, got
        assert not any(OLD_TOKEN in x for x in lines), "the token is never in the log"
    finally:
        stop(proc)
    return "the kept admin token: once the setup code, then a 30-day token that never signs in"


def test_rollback():
    tcd = _chart()
    env, _ = rendered(tcd=tcd)
    d = tempfile.mkdtemp(prefix="o1-back-")
    state = os.path.join(d, "state")
    proc, port, _ = serve(state, with_file(env, OLD_TOKEN, d))
    try:
        assert ask(port, "POST", "/v1/auth/setup", {"code": OLD_TOKEN, "username": "owner",
                                                     "password": PW})[0] == 201
    finally:
        stop(proc)
    # the template before: no TANDEM_AUTH, the kept Secret's value in TANDEM_ADMIN_TOKEN
    proc, port, _ = serve(state, {"TANDEM_AUTH": "legacy", "TANDEM_ADMIN_TOKEN": OLD_TOKEN,
                                  "TANDEM_DASHBOARD_LOGIN": "on"})
    try:
        assert ask(port, "GET", "/health")[0] == 200
        st, h, _ = ask(port, "POST", "/v1/dashboard/session", {"token": OLD_TOKEN})
        assert st == 204 and "Set-Cookie" in h, st
        assert ask(port, "GET", "/v1/settings", headers={
            "Authorization": f"Bearer {OLD_TOKEN}"})[0] == 200
    finally:
        stop(proc)
    return "the legacy mode on the upgraded state starts, and the admin token signs in"


def test_leftover_variable():
    tcd = _chart()
    env, code = rendered("controller.extraEnv[0].name=TANDEM_API_KEYS",
                         "controller.extraEnv[0].value=laptop",
                         r"auth.trustedProxy=192.0.2.0/24\,not-an-address", tcd=tcd)
    assert env["TANDEM_API_KEYS"] == "laptop"
    assert env["TANDEM_TRUSTED_PROXY"] == "192.0.2.0/24,not-an-address"
    d = tempfile.mkdtemp(prefix="o1-left-")
    proc, port, lines = serve(os.path.join(d, "state"), with_file(env, code, d))
    try:
        assert ask(port, "GET", "/health")[0] == 200, "the probes pass: the pod is Ready"
        assert ask(port, "POST", "/v1/auth/setup", {"code": code, "username": "owner",
                                                     "password": PW})[0] == 201
        h = session(port)
        got = findings(port, h)
        assert {"legacy_env_invalid", "trusted_proxy_invalid"} <= got, got
        st, _, out = ask(port, "GET", "/v1/fleet/health/controller", headers=h)
        f = next(x for x in json.loads(out)["findings"] if x["code"] == "trusted_proxy_invalid")
        assert f["facts"] == {"variable": "TANDEM_TRUSTED_PROXY", "file": "the environment"}, f
        assert "TANDEM_TRUSTED_PROXY" in f["sentence"] and f["detail"] == "not-an-address", f
        assert any("TANDEM_TRUSTED_PROXY: ignored 'not-an-address'" in x for x in lines), lines
    finally:
        stop(proc)
    return "a bad TANDEM_API_KEYS or proxy entry: the controller serves, each is a finding"


#: a request as the ingress controller forwards it from a browser or a client on another device
INGRESS = {"Host": "app.example.com", "Origin": "https://app.example.com",
           "X-Forwarded-Proto": "https", "X-Forwarded-For": "198.51.100.7",
           "X-Real-IP": "198.51.100.7"}
CHAT = {"model": "small-1", "messages": [{"role": "user", "content": "hi"}]}
MSG = {"model": "small-1", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]}


def test_api_through_the_ingress():
    """OPS-298 decision: the ingress routes the API paths by default; exposure is the
    controller's rule for a token, never for a signed-in page."""
    from proxy_util import fake
    tcd = _chart()
    env, code = rendered(r"auth.trustedProxy=192.0.2.0/24\,2001:db8:42::/64", tcd=tcd)
    assert env["TANDEM_INGRESS_API"] == "openai,anthropic", env
    srv, eport = fake(models=["small-1"], cache=False)
    d = tempfile.mkdtemp(prefix="o1-api-")
    env = {**with_file(env, code, d), "TANDEM_BACKENDS": "engine",
           "TANDEM_BACKEND_ENGINE_KIND": "tandemllm",
           "TANDEM_BACKEND_ENGINE_URL": f"http://127.0.0.1:{eport}/v1",
           "TANDEM_DEFAULT_BACKEND": "engine"}
    proc, port, _ = serve(os.path.join(d, "state"), env)
    try:
        assert ask(port, "POST", "/v1/auth/setup", {"code": code, "username": "owner",
                                                     "password": PW}, INGRESS)[0] == 201
        st, h, out = ask(port, "POST", "/v1/auth/session", {"username": "owner",
                                                            "password": PW}, INGRESS)
        assert st == 200 and h["Set-Cookie"].startswith("__Host-tandem_session="), (st, h)
        page = {**INGRESS, "Cookie": h["Set-Cookie"].split(";", 1)[0],
                "X-Tandem-CSRF": json.loads(out)["csrf"]}
        st, _, out = ask(port, "GET", "/v1/settings/api", headers=page)
        values = {v["key"]: v["value"] for v in json.loads(out)["values"]}
        assert values["openai.exposure"] == values["anthropic.exposure"] == "this-machine", values
        st, _, out = ask(port, "POST", "/v1/auth/tokens", {
            "label": "laptop", "scopes": ["inference:openai", "inference:anthropic"]}, page)
        assert st == 201, out[:200]
        tok = {**INGRESS, "Authorization": f"Bearer {json.loads(out)['token']}"}
        for path, body in (("/v1/chat/completions", CHAT), ("/v1/messages", MSG)):
            # the page's Playground, signed in, through the ingress: 200
            st, _, out = ask(port, "POST", path, body, page)
            assert st == 200, (path, st, out[:300])
            # a token from another device while the protocol is "this machine": the refusal
            st, _, out = ask(port, "POST", path, body, tok)
            assert st == 403 and b"Other devices cannot use this API" in out, (path, st, out)
        # the switch at network (an owner with a password may set it): the token's call answers
        st, _, out = ask(port, "POST", "/v1/settings/api/plan",
                         {"set": {"openai.exposure": "network"}}, page)
        assert st == 200, out[:300]
        plan = json.loads(out)
        st, _, out = ask(port, "POST", "/v1/settings/api/apply",
                         {"set": {"openai.exposure": "network"}, "confirm": plan["confirm"]},
                         page)
        assert st == 200, out[:300]
        assert ask(port, "POST", "/v1/chat/completions", CHAT, tok)[0] == 200
        assert ask(port, "POST", "/v1/messages", MSG, tok)[0] == 403
    finally:
        stop(proc)
        srv.shutdown()
    return "through the ingress: the page 200, a token 403 at this-machine and 200 at network"


def test_notes_exec_lines():
    """The `users setup-code` and `users reset-password` lines of NOTES, run as written after the
    `--` against a folder laid out as the pod's volume."""
    tcd = _chart()
    root = tempfile.mkdtemp(prefix="o1-vol-")
    cur = os.path.join(root, "current")
    os.makedirs(os.path.join(cur, "venv", "bin"))
    for pkg in ("proxy", "tandemapp"):
        os.symlink(os.path.join(ROOT, pkg), os.path.join(cur, pkg))
    py = os.path.join(cur, "venv", "bin", "python")
    with open(py, "w") as f:
        f.write(f"#!/bin/sh\nexec '{sys.executable}' \"$@\"\n")
    os.chmod(py, 0o755)
    n = tcd.notes("auth.mode=accounts", f"persistence.mountPath={root}")
    lines = {}
    for line in n.splitlines():
        for k in ("A new setup code:", "A lost password:"):
            if line.strip().startswith(k):
                lines[k] = shlex.split(line.split(" -- ", 1)[1])
    assert len(lines) == 2, n
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TANDEM_", "QSE_"))}
    state = os.path.join(root, "state")
    proc, port, _ = serve(state, {})
    stop(proc)                                # the config database is there, as in the pod
    r = subprocess.run(lines["A new setup code:"], capture_output=True, text=True, env=env,
                       timeout=60, cwd=root)
    assert r.returncode == 0 and "setup code: " in r.stdout, r.stdout + r.stderr
    code = r.stdout.split("setup code: ", 1)[1].split()[0]
    proc, port, _ = serve(state, {authlocal_env(): "off"})
    try:
        assert ask(port, "POST", "/v1/auth/setup", {"code": code, "username": "owner",
                                                     "password": PW})[0] == 201
    finally:
        stop(proc)
    argv = [x if x != "NAME" else "owner" for x in lines["A lost password:"]]
    r = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60, cwd=root)
    assert r.returncode == 0 and "One-time code: " in r.stdout, r.stdout + r.stderr
    return "NOTES' exec lines: a new setup code, a reset of a lost password"


def test_admin_token_file():
    from proxy import authlegacy
    d = tempfile.mkdtemp(prefix="o1-file-")
    path = os.path.join(d, "setup-code")
    with open(path, "w") as f:
        f.write(OLD_TOKEN + "\n")
    env = {"TANDEM_ADMIN_TOKEN_FILE": path}
    got, files = authlegacy.token_from_file(env, {})
    assert got["TANDEM_ADMIN_TOKEN"] == OLD_TOKEN and files == {"TANDEM_ADMIN_TOKEN": path}
    assert "TANDEM_ADMIN_TOKEN" not in env, "a copy: the caller's env is not changed"
    _, found = authlegacy.scan(got, files)
    assert [(f["code"], f["file"]) for f in found] == [("legacy_env_set", path)], found
    for env in ({"TANDEM_ADMIN_TOKEN_FILE": path, "TANDEM_ADMIN_TOKEN": "x" * 40},
                {"TANDEM_ADMIN_TOKEN_FILE": path, "QSE_ADMIN_TOKEN": "y" * 40},
                {"TANDEM_ADMIN_TOKEN_FILE": os.path.join(d, "gone")}, {}):
        assert authlegacy.token_from_file(env, {"A": "b"}) == (env, {"A": "b"}), env
    with open(path, "w") as f:
        f.write("ABCDEFGHJKMNPQRSTVWX\n")              # a setup code the chart made
    assert authlegacy.token_from_file({"TANDEM_ADMIN_TOKEN_FILE": path}, {})[0] == \
        {"TANDEM_ADMIN_TOKEN_FILE": path}
    return "a long value is the admin token named by its file; short, missing, env: unchanged"


def hook_script(tcd, port: int) -> str:
    """The Python of the chart's test pod (templates/tests/test-health.yaml), aimed at `port`."""
    rc, out = tcd.render("auth.mode=accounts")
    pod = next(d for d in out.split("\n---\n") if "helm.sh/hook: test" in d)
    body = pod.split("        - |\n", 1)[1]
    code = "\n".join(x[10:] for x in body.splitlines() if x.startswith(" " * 10) or not x.strip())
    want = 'base = "http://t-tandemapp:80"'
    assert want in code, code
    return code.replace(want, f'base = "http://127.0.0.1:{port}"')


def _read(*parts) -> str:
    with open(os.path.join(ROOT, *parts)) as f:
        return f.read()


def test_docs():
    import re
    k8s = _read("docs", "install-kubernetes.md")
    for words in ("auth.mode: accounts", "setup code", "TANDEM_SETUP_CODE_FILE",
                  "kubectl port-forward", "loopback is never trusted", "users setup-code",
                  "users reset-password NAME", "bootstrap_secret_left",
                  "auth.bootstrap.existingSecret",
                  "ingress.api.openai", "ingress.api.anthropic", "/v1/models` is always routed",
                  "TANDEM_INGRESS_API", "exposure_unreachable", "auth.trustedProxy",
                  "TANDEM_TRUSTED_PROXY", "exposure_plain_http", "30 days", "helm rollback",
                  "tokens chatd-key FILE"):
        assert words in k8s, words
    linux = _read("docs", "install-linux.md")
    for words in ("TANDEM_AUTH=accounts", "--behind-proxy", "local mode", "setup code",
                  "tandem-app users setup-code", "tandem-app users reset-password NAME",
                  "API exposure", "TANDEM_TRUSTED_PROXY", "--setup-code", "tandem-app check",
                  "30 days"):
        assert words in linux, words
    readme = _read("charts", "tandemapp", "README.md")
    values = _read("charts", "tandemapp", "values.yaml")
    block = values.split("\nauth:\n", 1)[1].split("\n\n", 1)[0]
    for key in ("mode", "existingSecret", "secretKey", "create", "bootstrap", "trustedProxy"):
        assert re.search(rf"(?m)^  {key}:", block), key
        assert f"`auth.{key}" in readme or f"`.{key}`" in readme or f", `.{key}`" in readme, key
    assert "`auth.bootstrap.existingSecret`, `.secretKey`, `.create`" in readme
    assert "`ingress.api.openai`, `.anthropic`" in readme
    env = _read("packaging", "app.env.example")
    for words in ("#TANDEM_AUTH=accounts", "#TANDEM_TRUSTED_PROXY=", "tandem-app users",
                  "tandem-app tokens"):
        assert words in env, words
    assert "TANDEM_AUTH=accounts" in _read("packaging", "Dockerfile.app")
    # no host of this project's own network in the public text
    for text in (k8s, linux, readme, env):
        assert not re.search(r"\b(192\.168|10\.\d+\.\d+\.\d+)\b", text)
    # the doc's kubectl exec lines are NOTES' own (release and namespace tandemapp)
    tcd = _chart()
    n = tcd.notes("auth.mode=accounts", name="tandemapp", namespace="tandemapp")
    joined = re.sub(r" \\\n\s+", " ", k8s)
    for k in ("A new setup code:", "A lost password:"):
        line = next(x for x in n.splitlines() if x.strip().startswith(k))
        cmd = line.split(k, 1)[1].strip()
        assert cmd in joined, (k, cmd)
    return "the docs say accounts, the code, the reset, exposure, the proxy; NOTES' lines match"


def authlocal_env() -> str:
    from proxy import authlocal
    return authlocal.LOCAL_ENV


if __name__ == "__main__":
    from scriptmode import run
    run(globals())
