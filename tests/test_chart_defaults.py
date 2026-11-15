"""OPS-288, OPS-292: the Helm chart's bare defaults, every mode it renders, an upgrade that keeps what
the running release has, and the release archive built twice to the same bytes.

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

helm 3.12 or newer on PATH for 1-3 (skipped without it). Run: python tests/test_chart_defaults.py
"""

from __future__ import annotations

import hashlib
import json
import os
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
