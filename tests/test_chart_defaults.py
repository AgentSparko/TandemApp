"""OPS-288, OPS-292, OPS-298: the Helm chart's bare defaults, every mode it renders, an upgrade
that keeps what the running release has, the release archive built twice to the same bytes, and the
sign-in of accounts (auth.mode) with the ingress's API paths.

  Gherkin:
  1  Given the chart's default values, When it is rendered, Then the controller runs bare:
     TANDEM_CHAT off, TANDEM_CONDUCTOR off, no MCP bridge, no sandbox Role or NetworkPolicy, and
     chatd.env is there for the page's switch; `helm lint --strict` passes
                                                                test_bare_defaults
  2  Given each chat and sandbox mode, When rendered, Then each renders with its own pieces, and
     the combinations that cannot work are refused with a message
                                                                test_every_mode
  3  Given a running release whose controller has TANDEM_CHAT own and a conductor folder, When it
     is upgraded with values that leave chat.mode and controller.conductor empty, Then both are
     kept; values that set them win; a new install is bare  test_upgrade_keeps_running_values
  4  Given the same tree, page and wheels, When the chart's archive is packed twice, Then the two
     are the same bytes, with one folder <id>/ holding proxy/, the page, wheelhouse/ and RELEASE,
     and no tests or ._ files                                   test_archive_reproducible

  OPS-298 (the server side of the same scenarios: tests/test_chart_accounts.py):
  5  Given auth.mode accounts, When rendered, Then the controller gets TANDEM_AUTH=accounts,
     TANDEM_DEPLOY=kubernetes and the setup code as a file (an optional Secret volume), and no
     container of any mode has an auth secret in its environment; legacy mode keeps the admin
     token as before; auth.bootstrap names another Secret; no Secret at all is allowed; wrong
     values are refused                                         test_accounts_values
  6  Given a release on the chart before accounts, When it is upgraded with auth.mode accounts,
     Then the Secret keeps its name, key and value (a rollback finds it) and is the setup code
                                                                test_upgrade_keeps_the_secret
  7  Given a release whose ingress routes /v1/chat/completions and ingress.api.openai empty, When
     it is upgraded to accounts mode, Then the ingress still routes it, And a new install with the
     same values blocks it; a release in accounts mode keeps what its TANDEM_INGRESS_API says
                                                                test_upgrade_keeps_the_clients_path
  8  Given ingress.api.anthropic false, Then /v1/messages goes to the blocked Service (503) while
     /v1/models stays routed; Given it true, Then /v1/messages is routed; the paths are the
     server's own lists                                         test_api_paths
  9  Given values shaped like a single-node install (tests/fixtures/chart-single-node.yaml), When
     rendered now, Then the objects are those of the chart before (the golden next to it) plus
     only the new auth lines, in legacy mode and in accounts mode
                                                                test_single_node_render
  10 NOTES: legacy mode word for word as before; accounts mode says where the setup code is, the
     kubectl exec lines for a new code and a lost password, the API paths, the trusted proxy and
     an external chatd's key                                    test_notes

helm 3.12 or newer on PATH for 1-3 and 5-10 (skipped without it). Run:
python tests/test_chart_defaults.py
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scriptmode import Skip  # noqa: E402

CHART = os.path.join(ROOT, "charts", "tandemapp")
LOOKUP = 'lookup "apps/v1" "Deployment" .Release.Namespace (include "tandemapp.fullname" .)'
SECRET_LOOKUP = 'lookup "v1" "Secret" .Release.Namespace $name'
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
SINGLE_NODE = os.path.join(FIXTURES, "chart-single-node.yaml")
GOLDEN = os.path.join(FIXTURES, "chart-single-node.golden.yaml")
ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"


def _helm() -> str:
    h = shutil.which("helm")
    if h is None:
        raise Skip("helm is not on PATH")
    return h


def render(*sets: str, chart: str = CHART, values: str | None = None) -> tuple[int, str]:
    argv = [_helm(), "template", "t", chart, "-n", "ns", "--set", "release.id=0123456789ab"]
    for s in sets:
        argv += ["--set", s]
    if values:
        argv += ["-f", values]
    r = subprocess.run(argv, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def env_of(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        s = line.strip()
        for k in ("TANDEM_CHAT", "TANDEM_CONDUCTOR"):
            if s.startswith(f"- {{name: {k}, value: "):
                out[k] = s.split("value: ", 1)[1].rstrip("}").strip().strip('"')
    return out


def test_bare_defaults():
    rc, out = render()
    assert rc == 0, out
    assert env_of(out) == {"TANDEM_CHAT": "off", "TANDEM_CONDUCTOR": "off"}, env_of(out)
    assert "  chatd.env: |" in out, "chatd.env is there for the page's switch"
    assert "name: mcp-" not in out and "kind: Role" not in out, "no bridge, no sandbox Role"
    assert "name: t-tandemapp-sandbox" not in out, "no sandbox account or NetworkPolicy"
    r = subprocess.run([_helm(), "lint", "--strict", CHART, "--set", "release.id=0123456789ab"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    return "bare: chat off, conductor off, no bridge, no sandbox pieces; lint strict passes"


def test_every_mode():
    ok = {
        ("chat.mode=own",): ("own", True),
        ("chat.mode=off",): ("off", True),
        ("chat.mode=external",): ("external", False),
        ("chat.mode=own", "sandbox.mode=kubernetes"): ("own", True),
        ("sandbox.mode=kubernetes",): ("off", True),
        ("chat.mode=own", "sandbox.mode=podman-socket",
         "sandbox.podmanSocket.hostDir=/run/user/1000/podman",
         "persistence.hostPath.path=/var/lib/tandemapp"): ("own", True),
    }
    for sets, (chat, chatd_env) in ok.items():
        rc, out = render(*sets)
        assert rc == 0, (sets, out[-800:])
        assert env_of(out)["TANDEM_CHAT"] == chat, (sets, env_of(out))
        assert ("  chatd.env: |" in out) == chatd_env, sets
        if "sandbox.mode=kubernetes" in sets:
            assert "QSE_SANDBOX_BACKEND=kubernetes" in out and "kind: Role" in out, sets
    rc, out = render("controller.conductor=conductor")
    assert rc == 0 and env_of(out)["TANDEM_CONDUCTOR"] == "/var/lib/tandemapp/conductor", out[-400:]
    rc, out = render("controller.conductor=/srv/cond")
    assert env_of(out)["TANDEM_CONDUCTOR"] == "/srv/cond"
    for sets, words in {
        ("chat.mode=external", "sandbox.mode=kubernetes"): "needs chat.mode own or off",
        ("chat.mode=bogus",): "chat.mode is off, external or own",
        ("sandbox.mode=podman-socket",): "needs sandbox.podmanSocket.hostDir",
        ("mcpBridge.enabled=true",): "mcpBridge.enabled needs mcpBridge.command",
        ("ingress.enabled=true",): "ingress.enabled needs ingress.host",
    }.items():
        rc, out = render(*sets)
        assert rc != 0 and words in out, (sets, out[-600:])
    rc, out = render("mcpBridge.enabled=true", "mcpBridge.command={/var/lib/tandemapp/mcp/bin/x}")
    assert rc == 0 and "readinessProbe" not in out.split("name: mcp-tools")[-1].split("- name:")[0], \
        "the bridge has no probe"
    return "own, off, external, kubernetes, podman-socket, conductor paths; refusals"


def _chart_with_running(tmp: str) -> str:
    """A copy of the chart whose lookup answers `.Values.testRunning` (helm template asks no
    cluster): the running Deployment of an upgrade."""
    dst = os.path.join(tmp, "tandemapp")
    shutil.copytree(CHART, dst)
    p = os.path.join(dst, "templates", "_helpers.tpl")
    with open(p) as f:
        text = f.read()
    assert text.count(LOOKUP) == 1, "the chart's lookup changed: update this test"
    with open(p, "w") as f:
        f.write(text.replace(LOOKUP, "(.Values.testRunning | default dict)"))
    # and the Secret's lookup (templates/secret.yaml): `.Values.testSecret`, the kept Secret
    p = os.path.join(dst, "templates", "secret.yaml")
    with open(p) as f:
        text = f.read()
    assert text.count(SECRET_LOOKUP) == 1, "the Secret's lookup changed: update this test"
    with open(p, "w") as f:
        f.write(text.replace(SECRET_LOOKUP, "(.Values.testSecret | default dict)"))
    return dst


def test_upgrade_keeps_running_values(tmp_path):
    chart = _chart_with_running(str(tmp_path))
    running = os.path.join(str(tmp_path), "running.yaml")
    with open(running, "w") as f:
        json.dump({"testRunning": {"spec": {"template": {"spec": {"containers": [
            {"name": "mcp-tools", "env": [{"name": "TANDEM_CHAT", "value": "external"}]},
            {"name": "controller", "env": [
                {"name": "TANDEM_CHAT", "value": "own"},
                {"name": "TANDEM_CONDUCTOR", "value": "/var/lib/tandemapp/conductor"},
                {"name": "TANDEM_ADMIN_TOKEN", "valueFrom": {"secretKeyRef": {"name": "x",
                                                                              "key": "y"}}}]}]}}}}},
                  f)
    rc, out = render(chart=chart, values=running)
    assert rc == 0, out[-800:]
    assert env_of(out) == {"TANDEM_CHAT": "own",
                           "TANDEM_CONDUCTOR": "/var/lib/tandemapp/conductor"}, env_of(out)
    # values that say it win over the running release
    rc, out = render("chat.mode=off", "controller.conductor=off", chart=chart, values=running)
    assert env_of(out) == {"TANDEM_CHAT": "off", "TANDEM_CONDUCTOR": "off"}, env_of(out)
    # kubernetes sandboxes on a running own release: allowed (the effective mode is own)
    rc, out = render("sandbox.mode=kubernetes", chart=chart, values=running)
    assert rc == 0, out[-600:]
    # a new install (no running release): bare
    rc, out = render(chart=chart)
    assert env_of(out) == {"TANDEM_CHAT": "off", "TANDEM_CONDUCTOR": "off"}
    return "an upgrade keeps own and the conductor folder; values win; a new install is bare"


# ============================================================================ OPS-298: accounts

def controller_env(text: str) -> dict:
    """The controller container's plain env entries ({name: value}); valueFrom ones as None."""
    part = text.split("        - name: controller\n", 1)[1]
    part = part.split("          startupProbe:", 1)[0]
    out: dict = {}
    lines = part.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"\s+- \{name: (\w+), value: (.*)\}$", line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"')
        m = re.match(r"\s+- name: (\w+)$", line)
        if m:
            nxt = re.match(r"\s+value: (.*)$", lines[i + 1]) if i + 1 < len(lines) else None
            out[m.group(1)] = nxt.group(1).strip().strip('"') if nxt else None
    return out


def section(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0] if start in text else ""


def blocked(text: str) -> list:
    """The Ingress's paths that go to the blocked Service (a 503), in order."""
    ing = next((d for d in re.split(r"(?m)^---\s*$", text) if "kind: Ingress" in d), "")
    out = []
    for m in re.finditer(r"- path: (\S+)\n\s+pathType: Prefix\n\s+backend:\n\s+service:\n"
                         r"\s+name: (\S+)", ing):
        if m.group(2).endswith("-blocked"):
            out.append(m.group(1))
    return out


def routed_root(text: str) -> bool:
    ing = next((d for d in re.split(r"(?m)^---\s*$", text) if "kind: Ingress" in d), "")
    return "- path: /\n" in ing


def secret_value(text: str, key: str = "admin-token") -> str:
    import base64
    doc = next(d for d in re.split(r"(?m)^---\s*$", text) if "kind: Secret" in d)
    m = re.search(rf"(?m)^  {re.escape(key)}: (\S+)$", doc)
    return base64.b64decode(m.group(1)).decode()


def no_env_secret(text: str) -> None:
    """No container takes an auth secret from its environment."""
    assert "secretKeyRef" not in text, "a Secret in a container's environment"
    assert not re.search(r"name: (TANDEM_ADMIN_TOKEN|QSE_ADMIN_TOKEN|QSE_METRICS_TOKEN)[,\n]",
                         text), "an admin token in the environment"


ING = ("ingress.enabled=true", "ingress.host=app.example.com")
FILE = "/etc/tandemapp/auth/setup-code"
OPENAI_PATHS = ["/v1/chat/completions", "/v1/completions", "/v1/embeddings", "/v1/responses",
                "/v1/cache"]


def test_accounts_values():
    rc, out = render("auth.mode=accounts")
    assert rc == 0, out[-800:]
    e = controller_env(out)
    assert e["TANDEM_AUTH"] == "accounts" and e["TANDEM_DEPLOY"] == "kubernetes", e
    assert e["TANDEM_SETUP_CODE_FILE"] == FILE and e["TANDEM_ADMIN_TOKEN_FILE"] == FILE, e
    assert "TANDEM_INGRESS_API" not in e, "no ingress: not known"
    assert "TANDEM_TRUSTED_PROXY" not in e
    no_env_secret(out)
    vol = section(out, "        - name: auth\n", "\n        - name:") or \
        section(out, "        - name: auth\n", "\n---")
    assert "secretName: t-tandemapp-admin" in vol and "optional: true" in vol, vol
    assert "{key: admin-token, path: setup-code}" in vol and "defaultMode: 0440" in vol, vol
    mount = "{name: auth, mountPath: /etc/tandemapp/auth, readOnly: true}"
    assert out.count(mount) == 1, "the controller only"
    assert mount in section(out, "        - name: controller\n", "      volumes:")
    code = secret_value(out)
    assert len(code) == 20 and set(code) <= set(ALPHABET), "a setup code of the app's alphabet"
    assert "helm.sh/resource-policy: keep" in out
    # no auth secret in any container's environment, in every mode the chart renders
    for sets in (("chat.mode=own", "sandbox.mode=kubernetes"), ("chat.mode=external",),
                 ("chat.mode=own", "mcpBridge.enabled=true", "mcpBridge.command={/x}"),
                 ING, ("auth.existingSecret=mine",), ("auth.bootstrap.existingSecret=code",),
                 ("auth.create=false",)):
        rc, got = render("auth.mode=accounts", *sets)
        assert rc == 0, (sets, got[-600:])
        no_env_secret(got)
        assert controller_env(got)["TANDEM_DEPLOY"] == "kubernetes", sets
    # legacy (the default until the switch): the admin token as before, beside TANDEM_DEPLOY
    rc, out = render()
    e = controller_env(out)
    assert e["TANDEM_ADMIN_TOKEN"] is None and "secretKeyRef" in out and "TANDEM_AUTH" not in e
    assert e["TANDEM_DEPLOY"] == "kubernetes" and "TANDEM_SETUP_CODE_FILE" not in e
    assert len(secret_value(out)) == 48 and "name: auth\n" not in out
    # a Secret of its own for the setup code: mounted with its key, never read as an admin token
    rc, out = render("auth.mode=accounts", "auth.bootstrap.existingSecret=first-code",
                     "auth.bootstrap.secretKey=code")
    e = controller_env(out)
    assert "kind: Secret" not in out and "secretName: first-code" in out, out[-600:]
    assert "{key: code, path: setup-code}" in out
    assert e["TANDEM_SETUP_CODE_FILE"] == FILE and "TANDEM_ADMIN_TOKEN_FILE" not in e
    # the admin token's Secret named by auth.existingSecret: mounted, its token imported later
    rc, out = render("auth.mode=accounts", "auth.existingSecret=old-admin")
    e = controller_env(out)
    assert "kind: Secret" not in out and "secretName: old-admin" in out
    assert e["TANDEM_ADMIN_TOKEN_FILE"] == FILE
    # no Secret at all: allowed (the code is in the log, or `users setup-code`)
    for sets in (("auth.create=false",), ("auth.bootstrap.create=false",)):
        rc, out = render("auth.mode=accounts", *sets)
        e = controller_env(out)
        assert rc == 0 and "kind: Secret" not in out and "name: auth\n" not in out, sets
        assert "TANDEM_SETUP_CODE_FILE" not in e and "TANDEM_ADMIN_TOKEN_FILE" not in e, sets
    rc, out = render("auth.mode=accounts", "auth.create=false", "auth.bootstrap.create=true")
    assert "kind: Secret" in out and controller_env(out)["TANDEM_SETUP_CODE_FILE"] == FILE
    # the proxy in front
    rc, out = render("auth.mode=accounts", "auth.trustedProxy=192.0.2.10")
    assert controller_env(out)["TANDEM_TRUSTED_PROXY"] == "192.0.2.10"
    # refusals
    for sets, words in {
        ("auth.mode=open",): "auth.mode is legacy or accounts",
        ("auth.create=false",): "name an existingSecret or set create",
        ("controller.env.TANDEM_AUTH=accounts",): "set auth.mode, not TANDEM_AUTH",
        ("controller.extraEnv[0].name=TANDEM_AUTH", "controller.extraEnv[0].value=accounts"):
            "set auth.mode, not TANDEM_AUTH",
        ("ingress.api.openai=maybe",): "ingress.api.openai is true, false or empty",
    }.items():
        rc, out = render(*sets)
        assert rc != 0 and words in out, (sets, out[-600:])
    r = subprocess.run([_helm(), "lint", "--strict", CHART, "--set", "release.id=0123456789ab",
                        "--set", "auth.mode=accounts", "--set", "ingress.enabled=true",
                        "--set", "ingress.host=app.example.com"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    return "accounts: the setup code a file, no auth secret in any env; legacy as before"


def _running(tmp: str, env: list, secret: str | None = None) -> str:
    path = os.path.join(tmp, f"running-{len(os.listdir(tmp))}.yaml")
    doc: dict = {"testRunning": {"spec": {"template": {"spec": {"containers": [
        {"name": "controller", "env": env}]}}}}}
    if secret is not None:
        import base64
        doc["testSecret"] = {"data": {"admin-token": base64.b64encode(secret.encode()).decode()}}
    with open(path, "w") as f:
        json.dump(doc, f)
    return path


#: the controller's env of a release on the chart before accounts mode
BEFORE = [{"name": "TANDEM_CHAT", "value": "own"},
          {"name": "TANDEM_CONDUCTOR", "value": "/var/lib/tandemapp/conductor"},
          {"name": "TANDEM_ADMIN_TOKEN", "valueFrom": {"secretKeyRef": {
              "name": "t-tandemapp-admin", "key": "admin-token"}}}]
OLD_TOKEN = "an-admin-token-of-the-release-before-0123456789abcdef"


def test_upgrade_keeps_the_secret(tmp_path):
    chart = _chart_with_running(str(tmp_path))
    running = _running(str(tmp_path), BEFORE, secret=OLD_TOKEN)
    rc, out = render("auth.mode=accounts", chart=chart, values=running)
    assert rc == 0, out[-800:]
    assert secret_value(out) == OLD_TOKEN, "the kept value, not a new code"
    assert "name: t-tandemapp-admin" in out and "helm.sh/resource-policy: keep" in out
    e = controller_env(out)
    assert e["TANDEM_SETUP_CODE_FILE"] == e["TANDEM_ADMIN_TOKEN_FILE"] == FILE
    assert "{key: admin-token, path: setup-code}" in out
    no_env_secret(out)
    # the way back: the chart before reads the same Secret and key into TANDEM_ADMIN_TOKEN
    with open(GOLDEN) as f:
        old = f.read()
    assert ("- name: TANDEM_ADMIN_TOKEN\n              valueFrom:\n                secretKeyRef:\n"
            "                  name: t-tandemapp-admin\n                  key: admin-token") in old
    # legacy mode on the same release: unchanged
    rc, out = render(chart=chart, values=running)
    assert secret_value(out) == OLD_TOKEN and controller_env(out)["TANDEM_ADMIN_TOKEN"] is None
    return "an upgrade keeps the Secret (name, key, value); the chart before reads it back"


def test_upgrade_keeps_the_clients_path(tmp_path):
    chart = _chart_with_running(str(tmp_path))
    before = _running(str(tmp_path), BEFORE)
    rc, out = render("auth.mode=accounts", *ING, chart=chart, values=before)
    assert rc == 0, out[-800:]
    assert blocked(out) == ["/chat-engine", "/plugin-backends"], blocked(out)
    assert controller_env(out)["TANDEM_INGRESS_API"] == "openai,anthropic"
    # values that say it win
    rc, out = render("auth.mode=accounts", *ING, "ingress.api.openai=false", chart=chart,
                     values=before)
    assert blocked(out)[2:] == OPENAI_PATHS and controller_env(out)["TANDEM_INGRESS_API"] == \
        "anthropic"
    # a new install with the same values blocks both
    rc, out = render("auth.mode=accounts", *ING, chart=chart)
    assert blocked(out) == ["/chat-engine", "/plugin-backends", *OPENAI_PATHS, "/v1/messages"]
    assert controller_env(out)["TANDEM_INGRESS_API"] == "none" and routed_root(out)
    # a release in accounts mode keeps what its TANDEM_INGRESS_API says
    for said, want in {"openai": ["/v1/messages"], "anthropic": OPENAI_PATHS,
                       "openai,anthropic": [], "none": OPENAI_PATHS + ["/v1/messages"]}.items():
        run = _running(str(tmp_path), [{"name": "TANDEM_AUTH", "value": "accounts"},
                                       {"name": "TANDEM_INGRESS_API", "value": said}])
        rc, out = render("auth.mode=accounts", *ING, chart=chart, values=run)
        assert blocked(out)[2:] == want, (said, blocked(out))
    # in accounts mode with no ingress before (no TANDEM_INGRESS_API): a new ingress blocks both
    run = _running(str(tmp_path), [{"name": "TANDEM_AUTH", "value": "accounts"}])
    rc, out = render("auth.mode=accounts", *ING, chart=chart, values=run)
    assert blocked(out)[2:] == OPENAI_PATHS + ["/v1/messages"]
    # legacy mode: the ingress as before, whatever runs
    rc, out = render(*ING, chart=chart)
    assert blocked(out) == ["/chat-engine", "/plugin-backends"]
    assert "TANDEM_INGRESS_API" not in controller_env(out)
    return "an upgrade keeps routing the API; a new install blocks it; accounts keep their own"


def test_api_paths():
    from proxy import deployments
    from proxy import tokens as tokens_mod
    from proxy.service import PROXY_PATHS
    # the chart's lists are the server's
    assert OPENAI_PATHS[:-1] == list(PROXY_PATHS)
    assert all(p.startswith(OPENAI_PATHS[-1] + "/") for p in deployments.CACHE_PATHS)
    assert tokens_mod.MESSAGES[1].startswith(tokens_mod.MESSAGES[0] + "/")
    rc, out = render("auth.mode=accounts", *ING, "ingress.api.openai=true",
                     "ingress.api.anthropic=false")
    assert rc == 0, out[-600:]
    assert "/v1/messages" in blocked(out) and "/v1/chat/completions" not in blocked(out)
    assert controller_env(out)["TANDEM_INGRESS_API"] == "openai"
    rc, out = render("auth.mode=accounts", *ING, "ingress.api.openai=true",
                     "ingress.api.anthropic=true")
    assert blocked(out) == ["/chat-engine", "/plugin-backends"]
    assert controller_env(out)["TANDEM_INGRESS_API"] == "openai,anthropic"
    # /v1/models is never blocked; the page's own routes neither
    for sets in ((), ("ingress.api.openai=false", "ingress.api.anthropic=false")):
        for mode in ("legacy", "accounts"):
            rc, out = render(f"auth.mode={mode}", *ING, *sets)
            assert not any(p.startswith(("/v1/models", "/v1/library", "/v1/laya", "/v1/fleet",
                                         "/v1/auth", "/dashboard", "/health"))
                           for p in blocked(out)), blocked(out)
            assert routed_root(out)
    # a value set in legacy mode is applied too
    rc, out = render(*ING, "ingress.api.anthropic=false")
    assert blocked(out)[2:] == ["/v1/messages"]
    # the blocked Service is there for the API paths alone
    rc, out = render("auth.mode=accounts", *ING, "ingress.blockedPaths=null")
    assert blocked(out) == OPENAI_PATHS + ["/v1/messages"] and "name: t-tandemapp-blocked" in out
    rc, out = render("auth.mode=accounts", *ING, "ingress.blockedPaths=null",
                     "ingress.api.openai=true", "ingress.api.anthropic=true")
    assert blocked(out) == [] and "t-tandemapp-blocked" not in out
    return "per protocol: blocked or routed; /v1/models always routed; the server's own lists"


def normalized(text: str) -> list:
    """A render with what changes on each run blanked: the Secret's random value, the config
    checksum, and the bodies of the two files the ConfigMap carries (files/)."""
    text = re.sub(r"(?m)^(  admin-token: )\S+$", r"\1<random>", text)
    text = re.sub(r"(?m)^(\s+checksum/config: )\S+$", r"\1<sha256>", text)
    out, skip = [], False
    for line in text.splitlines():
        if skip and (line.startswith("    ") or not line.strip()):
            continue
        skip = False
        out.append(line)
        if line in ("  init.sh: |-", "  mcp-stdio-http.py: |-"):
            out.append("    <file>")
            skip = True
    return out


def golden_lines() -> list:
    """The golden render without its three lines of header."""
    with open(GOLDEN) as f:
        return f.read().splitlines()[3:]


def changes(old: list, new: list) -> tuple[list, list]:
    """(lines gone, lines added)."""
    gone, added = [], []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=old, b=new, autojunk=False).get_opcodes():
        if op in ("delete", "replace"):
            gone += old[i1:i2]
        if op in ("insert", "replace"):
            added += new[j1:j2]
    return gone, added


DEPLOY_LINES = [" " * 12 + "# never local mode: kubectl port-forward reaches the pod on its loopback",
                " " * 12 + "- {name: TANDEM_DEPLOY, value: kubernetes}"]
ENV_SECRET = ["            - name: TANDEM_ADMIN_TOKEN", "              valueFrom:",
              "                secretKeyRef:", "                  name: t-tandemapp-admin",
              "                  key: admin-token"]
#: what accounts mode may add to a render (the auth bits), each line matching one of these
AUTH_BITS = re.compile(
    r"^\s+(# .*|- \{name: TANDEM_(DEPLOY|AUTH|SETUP_CODE_FILE|ADMIN_TOKEN_FILE|INGRESS_API), "
    r"value: .*\}|- \{name: auth, mountPath: /etc/tandemapp/auth, readOnly: true\}|- name: auth|"
    r"secret:|secretName: t-tandemapp-admin|optional: true|defaultMode: 0440|items:|"
    r"- \{key: admin-token, path: setup-code\}|- path: /v1/(chat/completions|completions|"
    r"embeddings|responses|cache|messages)|pathType: Prefix|backend:|service:|"
    r"name: t-tandemapp-blocked|port:|number: 80)$")


def test_single_node_render(tmp_path):
    old = golden_lines()
    rc, out = render(values=SINGLE_NODE)
    assert rc == 0, out[-800:]
    gone, added = changes(old, normalized(out))
    assert gone == [] and added == DEPLOY_LINES, (gone, added)
    # accounts mode: the admin token leaves the env; only auth lines come in
    rc, out = render("auth.mode=accounts", values=SINGLE_NODE)
    gone, added = changes(old, normalized(out))
    assert gone == ENV_SECRET, gone
    assert added and all(AUTH_BITS.match(x) for x in added), \
        [x for x in added if not AUTH_BITS.match(x)]
    assert "            - {name: TANDEM_INGRESS_API, value: \"none\"}" in added
    # the same values as an upgrade of the release before: its ingress keeps the API paths
    chart = _chart_with_running(str(tmp_path))
    running = _running(str(tmp_path), BEFORE, secret=OLD_TOKEN)
    argv = [_helm(), "template", "t", chart, "-n", "ns", "-f", SINGLE_NODE, "-f", running,
            "--set", "auth.mode=accounts"]
    r = subprocess.run(argv, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-600:]
    gone, added = changes(old, normalized(r.stdout))
    assert gone == ENV_SECRET and not any("- path: /v1/" in x for x in added), added
    assert "            - {name: TANDEM_INGRESS_API, value: \"openai,anthropic\"}" in added
    return "the single-node values: the objects of before plus only the auth lines"


def notes(*sets: str) -> str:
    argv = [_helm(), "install", "t", CHART, "-n", "ns", "--dry-run=client", "--set",
            "release.id=0123456789ab"]
    for s in sets:
        argv += ["--set", s]
    r = subprocess.run(argv, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-600:]
    return r.stdout.split("NOTES:\n", 1)[1]


def test_notes():
    n = notes()
    assert ("4. The admin password (TANDEM_ADMIN_TOKEN) is in Secret t-tandemapp-admin, key "
            "admin-token.\n   Read it into your password manager, never into a log:\n") in n
    assert "Sign-in: accounts" not in n and "\n\n4. The admin" in n
    n = notes("auth.mode=accounts", *ING, "ingress.tls[0].secretName=tls", "chat.mode=external")
    cli = ("kubectl -n ns exec deploy/t-tandemapp -c controller -- env PYTHONPATH="
           "/var/lib/tandemapp/current /var/lib/tandemapp/current/venv/bin/python -m proxy")
    for words in ("4. Sign-in: accounts.", "never trusts its own loopback",
                  "kubectl -n ns get secret t-tandemapp-admin -o jsonpath='{.data.admin-token}' "
                  "| base64 -d; echo",
                  f"{cli} users setup-code --config-db /var/lib/tandemapp/state/config.sqlite3",
                  f"{cli} users reset-password NAME --config-db "
                  "/var/lib/tandemapp/state/config.sqlite3",
                  "API token for scripts that ends after 30 days", "bootstrap_secret_left",
                  "roll back", "OpenAI-compatible API: no; the Messages API: no",
                  "auth.trustedProxy", f"{cli} tokens chatd-key /tmp/chatd.key",
                  "/v1/messages is not routed"):
        assert words in n, (words, n)
    assert "TANDEM_ADMIN_TOKEN" not in n
    n = notes("auth.mode=accounts", "auth.bootstrap.existingSecret=code", *ING,
              "ingress.api.openai=true", "auth.trustedProxy=192.0.2.10")
    assert "Secret code, key admin-token" in n and "30 days" not in n
    assert "OpenAI-compatible API: yes" in n and "auth.trustedProxy" not in n
    n = notes("auth.mode=accounts", "auth.create=false")
    assert "the controller's log has a code" in n and "kubectl -n ns get secret" not in n
    return "legacy NOTES as before; accounts: the code, kubectl exec lines, API paths, chatd key"


def _tree(tmp: str) -> str:
    src = os.path.join(tmp, "src")
    shutil.copytree(ROOT, src, ignore=shutil.ignore_patterns(
        ".git", "node_modules", "__pycache__", "tests", "bench", "dist", ".venv", "venv"))
    from tandemapp import bundle
    dist = os.path.join(src, "dashboard", "dist")
    os.makedirs(os.path.join(dist, "assets"), exist_ok=True)
    with open(os.path.join(dist, "index.html"), "w") as f:
        f.write("<!doctype html><title>TandemApp</title><script src=./assets/a.js></script>\n")
    with open(os.path.join(dist, "assets", "a.js"), "w") as f:
        f.write("1;\n")
    bundle.write_manifest(dist, "record")
    return src


def test_archive_reproducible(tmp_path):
    src = _tree(str(tmp_path))
    wheels = os.path.join(str(tmp_path), "wheels")
    os.makedirs(wheels)
    for n in ("psutil-7.2.2-cp36-abi3-manylinux_2_28_x86_64.whl",
              "cryptography-50.0.2-cp311-abi3-manylinux_2_28_x86_64.whl"):
        with open(os.path.join(wheels, n), "wb") as f:
            f.write(n.encode())
    rid, commit = "0123456789ab", "0123456789ab" + "c" * 28
    shas = []
    for n in (1, 2):
        out = os.path.join(str(tmp_path), f"out{n}")
        r = subprocess.run([sys.executable, "-I", os.path.join(CHART, "scripts", "pack-archive.py"),
                            src, rid, commit, "1763200000", wheels, out],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
        doc = json.loads(r.stdout.strip().splitlines()[-1])
        arc = os.path.join(out, f"tandemapp-{rid}.tar.gz")
        with open(arc, "rb") as f:
            shas.append(hashlib.sha256(f.read()).hexdigest())
        assert doc["sha256"] == shas[-1]
    assert shas[0] == shas[1], shas
    with tarfile.open(arc) as t:
        names = t.getnames()
        rel = t.extractfile(f"{rid}/RELEASE").read().decode()
        members = t.getmembers()
    assert {n.split("/")[0] for n in names} == {rid}, "one folder <id>/"
    for need in ("proxy/__main__.py", "packaging/linux/tandem-app.sh", "dashboard/dist/index.html",
                 "dashboard/dist/bundle.json", "wheelhouse/psutil-7.2.2-cp36-abi3-manylinux_2_28_x86_64.whl"):
        assert f"{rid}/{need}" in names, need
    assert not any("/tests/" in n or os.path.basename(n).startswith("._") for n in names)
    assert not any(n.startswith(f"{rid}/dashboard/src/") for n in names)
    assert f"commit={commit}" in rel and "shipped=2025-11-15T" in rel, rel
    assert all(m.mtime == 1763200000 and m.uid == 0 for m in members)
    return "same bytes twice; <id>/ with proxy, the page, wheelhouse, RELEASE; no tests or ._"


if __name__ == "__main__":
    from scriptmode import run
    run(globals())
