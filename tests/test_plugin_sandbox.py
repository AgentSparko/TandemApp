"""SRV-859 (QA SRV-860), the F2 gate: a sandboxed plugin cannot reach the LAN, the controller, the
Kubernetes API or another plugin's secret.

  1  The egress filter's host list (chatd/egress.py `Allow`): only a granted host (and port), a
     granted LAN address allowed, a loopback, link-local or the Kubernetes API's address refused
     even when granted                                          test_egress_allow
  2  The real container runtime (Docker on the box with QSE_SANDBOX_BOX=1, as
     tests/test_sandbox_box.py; skipped without it, its socket or the sandbox image): the
     kitchen-sink plugin in its own container, uid 1000, no network, its tree read-only; from
     inside it a direct connect to a LAN address or the controller fails, the proxy refuses an
     address it was not granted, this box and the Kubernetes API even when granted, and passes
     the one it was granted; another plugin's secret is neither in its
     environment nor given on request, the config database and the other plugin's folders are
     not there; stopped, its container is gone                  test_docker_sandbox_gate
  3  The examples run sandboxed: hello-tool's greet answers from its container  (with 2)
  4  The pod path against the fake Kubernetes API (tests/fake_k8s.py): the plugin's pod (the
     chat sandbox pod's spec, its tree a read-only subPath, its own labels, the plugin egress
     port), its entry through pods/exec with its environment in a 0600 file the launcher
     removes, hello-tool answering MCP through it, the egress proxy giving that pod only its
     granted hosts, the pod deleted at stop                     test_kubernetes_pod_path

Run: python tests/test_plugin_sandbox.py; on the box with Docker: QSE_SANDBOX_BOX=1 python
tests/test_plugin_sandbox.py
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from plugin_host_util import Rig, folder, kitchen, wait  # noqa: E402
from scriptmode import Skip  # noqa: E402

from chatd import egress  # noqa: E402
from proxy import pluginrt  # noqa: E402

SOCK = os.environ.get("QSE_SANDBOX_SOCKET") or "/var/run/docker.sock"
IMAGE = os.environ.get("QSE_SANDBOX_IMAGE") or "tandem-sandbox:1"


def lan_ip() -> str | None:
    """This box's own address on its network (no packet is sent), or None."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    return None if ip.startswith("127.") or ip == "0.0.0.0" else ip


class Listener:
    """A TCP port that accepts and closes (a LAN service, the controller)."""

    def __init__(self, host: str):
        self.sock = socket.socket()
        self.sock.bind((host, 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.hits = 0
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.hits += 1
            c.close()

    def close(self):
        self.sock.close()


def _connect_via(allow, line: bytes) -> bytes:
    a, b = socket.socketpair()
    stats = {"connections": 0, "refused": 0}
    t = threading.Thread(target=egress.serve, args=(b, lambda: allow, stats), daemon=True)
    t.start()
    a.sendall(line)
    a.settimeout(10)
    got = a.recv(400)
    a.close()
    t.join(5)
    return got


# ---------------------------------------------------------------------------- 1
def test_egress_allow():
    lan = Listener("0.0.0.0")
    try:
        allow = egress.Allow([f"10.66.0.1:{lan.port}", "localhost", "127.0.0.1",
                              "169.254.169.254"])
        assert allow.permits("10.66.0.1", lan.port) and not allow.permits("10.66.0.1", 80)
        assert not allow.permits("10.66.0.2", lan.port) and allow.permits("LOCALHOST", 1)
        got = _connect_via(allow, b"CONNECT 10.66.0.2:443 HTTP/1.1\r\n\r\n")
        assert got.startswith(b"HTTP/1.1 403") and b"not a host this plugin was granted" in got
        got = _connect_via(allow, b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        assert got.startswith(b"HTTP/1.1 403"), got
        for target in (f"127.0.0.1:{lan.port}", f"localhost:{lan.port}", "169.254.169.254:80"):
            got = _connect_via(allow, f"CONNECT {target} HTTP/1.1\r\n\r\n".encode())
            assert got.startswith(b"HTTP/1.1 403") and b"no plugin may reach" in got, \
                (target, got)
        got = _connect_via(allow, b"GET http://10.66.0.2/x HTTP/1.1\r\nHost: 10.66.0.2\r\n\r\n")
        assert b"not a host this plugin was granted" in got, got
        ip = lan_ip()
        if ip:
            allow = egress.Allow([f"{ip}:{lan.port}"])
            got = _connect_via(allow, f"CONNECT {ip}:{lan.port} HTTP/1.1\r\n\r\n".encode())
            assert got.startswith(b"HTTP/1.1 200"), got          # a granted LAN address
            old = os.environ.get("KUBERNETES_SERVICE_HOST")
            os.environ["KUBERNETES_SERVICE_HOST"] = ip
            try:
                got = _connect_via(allow, f"CONNECT {ip}:{lan.port} HTTP/1.1\r\n\r\n".encode())
                assert got.startswith(b"HTTP/1.1 403"), got    # the Kubernetes API, granted
            finally:
                if old is None:
                    os.environ.pop("KUBERNETES_SERVICE_HOST", None)
                else:
                    os.environ["KUBERNETES_SERVICE_HOST"] = old
        assert egress.never("::1") and egress.never("::ffff:127.0.0.1") and \
            egress.never("fe80::1") and egress.never("0.0.0.0") and not egress.never("10.0.0.1")
        # a chat's proxy is unchanged: on, off
        assert b"network is off" in _connect_via("off", b"CONNECT example.com:443 HTTP/1.1"
                                                        b"\r\n\r\n")
        got = _connect_via("on", b"CONNECT 10.0.0.1:443 HTTP/1.1\r\n\r\n")
        assert b"local network address" in got, got
    finally:
        lan.close()
    return "only granted hosts; loopback, link-local and the cluster's API never"


# ---------------------------------------------------------------------------- 2, 3
def _docker() -> None:
    if os.environ.get("QSE_SANDBOX_BOX") != "1":
        raise Skip("set QSE_SANDBOX_BOX=1 on the box (a real container runtime)")
    if not os.path.exists(SOCK):
        raise Skip(f"no container runtime socket at {SOCK}")
    from chatd.sandbox import Engine
    try:
        st, _ = Engine(SOCK).call("GET", f"/images/{urllib.parse.quote(IMAGE, safe='')}/json",
                                  timeout=10)
    except OSError as exc:
        raise Skip(f"the container runtime does not answer ({type(exc).__name__})")
    if st != 200:
        raise Skip(f"the sandbox image {IMAGE} is not there")


def test_docker_sandbox_gate():
    _docker()
    ip = lan_ip()
    lan = Listener(ip or "0.0.0.0")
    other_lan = Listener(ip or "0.0.0.0")
    ctl = Listener("127.0.0.1")
    r = Rig("docker")
    hosts = [f"{ip}:{lan.port}", f"127.0.0.1:{ctl.port}"] if ip else []
    src = kitchen(hosts=hosts)
    other = kitchen(name="other-sink")
    hello = folder("hello-tool")
    try:
        cfg = pluginrt.sandbox_config({"QSE_SANDBOX_SOCKET": SOCK, "QSE_SANDBOX_IMAGE": IMAGE})
        assert cfg["backend"] == "docker", cfg
        rt = pluginrt.make_sandbox(cfg, os.path.join(r.host.state_dir, "plugin-sandboxes"),
                                   env={}, log=r.lines.append)
        r.host.set_runtime("sandboxed", rt)
        oid = r.add(other, grant=["prompts:meta", "secrets:TOKEN"],
                    secrets_set={"TOKEN": "theirs-secret-777"})
        pid = r.add(src, grant=["prompts:meta", "secrets:TOKEN"] +
                    [f"network:{h}" for h in hosts],
                    confirmed=[f"network:{h}" for h in hosts],
                    secrets_set={"TOKEN": "mine-secret-111"})
        hid = r.add(hello)
        st = r.running(pid, 120)
        r.running(oid, 120)
        r.running(hid, 120)
        name = rt.boxes.name(pid)
        assert st["runtime"] == "docker" and st["where"] == name, st
        assert name.startswith("tandem-plugin-kitchen-sink-"), name
        # 3: an example answers from its container
        got = r.host.tool_call(hid, "greet", {"name": "Ada"})
        assert got["content"][0]["text"] == "Hello, Ada.", got
        # the container: locked down as a chat sandbox, no network, the tree read-only
        from chatd.sandbox import Engine
        st_, info = Engine(SOCK).call("GET", f"/containers/{name}/json")
        hc = info["HostConfig"]
        assert st_ == 200 and hc["NetworkMode"] == "none" and hc["ReadonlyRootfs"] is True
        assert hc["CapDrop"] == ["ALL"] and "no-new-privileges" in hc["SecurityOpt"]
        assert hc["Memory"] == pluginrt.MEM_MB * 1024 * 1024 and hc["PidsLimit"] == pluginrt.PIDS
        assert info["Config"]["User"] == "1000:1000"
        binds = sorted(b.split(":", 1)[1] for b in hc["Binds"])
        assert binds == ["/home/agent:rw", "/plugin:ro", "/run/tandem:ro", "/workspace:rw"], \
            binds
        assert r.probe(pid, what="read", path="/plugin/plugin.json") == "readable"
        assert r.probe(pid, what="exists", path=r.cfg.path) == "False"
        assert r.probe(pid, what="exists", path=r.store.get(oid)["path"]) == "False"
        assert r.probe(pid, what="exists", path="/workspace/../../run/tandem/proxy.sock") \
            == "True"
        # the LAN and the controller, directly: no network at all
        for host, port in ((ip or "10.0.0.1", lan.port), ("127.0.0.1", ctl.port),
                           ("10.0.0.1", 80), ("192.168.1.1", 80)):
            out = r.probe(pid, what="connect", host=host, port=port)
            assert out.startswith("refused"), (host, port, out)
        assert ctl.hits == 0
        # through the proxy: a LAN address it was not granted, this box, the cluster's API
        for host, port in (("10.0.0.1", 443), ("192.168.1.1", 80),
                           (ip or "10.0.0.2", other_lan.port)):
            out = r.probe(pid, what="proxy", host=host, port=port)
            assert out.startswith("HTTP/1.1 403"), (host, port, out)
        out = r.probe(pid, what="proxy", host="127.0.0.1", port=ctl.port)
        assert out.startswith("HTTP/1.1 403") and "no plugin may reach" in out, out
        assert ctl.hits == 0 and other_lan.hits == 0
        if ip:
            before = lan.hits
            out = r.probe(pid, what="proxy", host=ip, port=lan.port)
            assert out.startswith("HTTP/1.1 200"), out            # the one it was granted
            assert wait(lambda: lan.hits > before, 5)
            os.environ["KUBERNETES_SERVICE_HOST"] = ip
            try:
                out = r.probe(pid, what="proxy", host=ip, port=lan.port)
                assert out.startswith("HTTP/1.1 403"), out        # the cluster's API, granted
            finally:
                os.environ.pop("KUBERNETES_SERVICE_HOST", None)
        # another plugin's secret: not in its environment, refused on request, journaled
        assert r.probe(pid, what="getenv", name="TOKEN") == "mine-secret-111"
        names = json.loads(r.probe(pid, what="env"))
        assert not any("theirs" in r.probe(pid, what="getenv", name=n) for n in names)
        got = json.loads(r.probe(pid, what="tandem/secret", name="TOKEN", plugin="other-sink"))
        assert got["error"]["code"] == -32001 and "theirs" not in json.dumps(got), got
        assert r.host.status(pid)["refusals"][-1]["capability"] == "secrets:other-sink/TOKEN"
        # stopped: the container is gone
        r.host.stop_plugin(pid)
        st_, _ = Engine(SOCK).call("GET", f"/containers/{name}/json")
        assert st_ == 404, st_
    finally:
        r.close()
        for x in (lan, other_lan, ctl):
            x.close()
        for d in (src, other, hello):
            shutil.rmtree(d, ignore_errors=True)
    return (f"LAN {'(' + ip + ') ' if ip else ''}and controller unreachable, the cluster's API "
            f"refused, only the granted host passes, another plugin's secret refused")


# ---------------------------------------------------------------------------- 4
def test_kubernetes_pod_path():
    from chatd.k8s import Api
    from fake_k8s import FakeK8s
    tmp = tempfile.mkdtemp(prefix="plugin-k8s-")
    fake = FakeK8s()
    ctrl = fake.add("ctrl-0", {"app.kubernetes.io/component": "controller"}, ip="10.9.0.2")
    ctrl["spec"]["nodeName"] = "node-1"
    with open(os.path.join(tmp, "token"), "w") as fh:
        fh.write("test-token\n")
    r = Rig("k8s")
    src = folder("hello-tool")
    procs = []
    try:
        vol = os.path.dirname(r.host.state_dir)
        labels = {"app.kubernetes.io/component": "chat-sandbox", "app.kubernetes.io/instance": "t"}
        env = {"QSE_K8S_CLAIM": "t-data", "QSE_K8S_POD": "ctrl-0", "QSE_K8S_VOLUME_ROOT": vol,
               "QSE_K8S_HOST_USERS": "false", "QSE_K8S_LABELS": json.dumps(labels),
               "QSE_K8S_CONTROLLER_PORT": "8091", "QSE_K8S_SERVICE_ACCOUNT": "t-sandbox"}
        api = Api(url=fake.url, token_file=os.path.join(tmp, "token"), namespace="ns", env={})
        pods = pluginrt._kube_pods()(os.path.join(r.host.state_dir, "plugin-sandboxes"),
                                     api=api, env=env, image="tandem-sandbox:1",
                                     mem_bytes=512 * 1024 * 1024, egress_port=0,
                                     log=r.lines.append)
        pods.check_wait_s = 5.0
        assert pods.run_check()["state"] == "ok", pods.check
        rt = pluginrt.BoxRuntime("kubernetes", pods, r.lines.append)
        r.host.set_runtime("sandboxed", rt)
        seen = {}

        def on_exec(ctx):
            cmd = ctx.cmd
            if cmd[:3] != ["python3", "-c", pluginrt.LAUNCH]:
                return 0
            home = pods.paths(ctx.pod["metadata"]["annotations"]["tandem.chat/id"])[1]
            path = cmd[3].replace("/home/agent", home, 1)
            seen["mode"] = oct(os.stat(path).st_mode & 0o777)
            with open(path) as f:
                penv = json.load(f)
            os.remove(path)                                # what the launcher does
            seen["env"] = penv
            seen["workdir"] = ctx.argv[5]
            tree = pods.trees[ctx.pod["metadata"]["annotations"]["tandem.chat/id"]]
            cwd = ctx.argv[5].replace("/plugin", tree, 1)
            p = subprocess.Popen(cmd[4:], cwd=cwd, env=dict(penv, PATH=os.environ["PATH"]),
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL)
            procs.append(p)

            def out():
                for ln in p.stdout:
                    try:
                        ctx.out(1, ln)
                    except OSError:
                        return
            threading.Thread(target=out, daemon=True).start()
            while True:
                f = ctx._read_frame()
                if f is None or f[0] == 8:
                    break
                data = f[1]
                if data[:1] == b"\x00" and len(data) > 1:
                    p.stdin.write(data[1:])
                    p.stdin.flush()
                elif data[:1] == b"\xff":
                    break
            p.kill()
            p.wait(5)
            return None
        fake.on_exec = on_exec
        hid = r.add(src)
        st = r.running(hid, 30)
        assert st["runtime"] == "kubernetes" and st["where"] == "tandem-plugin-hello-tool", st
        got = r.host.tool_call(hid, "greet", {"name": "Pod"})
        assert got["content"][0]["text"] == "Hello, Pod.", got
        pod = fake.pods["tandem-plugin-hello-tool"]
        md, spec = pod["metadata"], pod["spec"]
        c = spec["containers"][0]
        assert md["labels"]["tandem.plugin"] == "hello-tool"
        assert md["labels"]["app.kubernetes.io/component"] == "chat-sandbox"
        assert spec["automountServiceAccountToken"] is False and spec["hostUsers"] is False
        assert c["securityContext"]["readOnlyRootFilesystem"] is True
        assert c["securityContext"]["capabilities"] == {"drop": ["ALL"]}
        assert c["resources"]["limits"] == {"memory": str(512 * 1024 * 1024), "cpu": "1"}
        tree = os.path.realpath(r.store.get(hid)["path"])
        mounts = {m["mountPath"]: m for m in c["volumeMounts"]}
        assert mounts["/plugin"]["readOnly"] is True and \
            mounts["/plugin"]["subPath"] == os.path.relpath(tree, os.path.realpath(vol))
        envs = {e["name"]: e["value"] for e in c["env"]}
        assert envs["HTTPS_PROXY"] == f"http://10.9.0.2:{pods.egress_port}"
        # the environment went in a 0600 file, never on the exec's command line
        assert seen["mode"] == "0o600" and seen["workdir"] == "/plugin"
        assert seen["env"]["TANDEM_PLUGIN"] == "hello-tool"
        assert seen["env"]["TANDEM_PLUGIN_DATA"] == "/workspace"
        assert not any("TANDEM_PLUGIN_SETTINGS" in a for e in fake.execs for a in e.argv)
        # the egress proxy knows the plugin's pod by its address: only its grants (none here)
        pods.box(hid).ip = "127.0.0.1"
        s = socket.create_connection(("127.0.0.1", pods.egress_port), 5)
        s.sendall(b"CONNECT 10.0.0.1:443 HTTP/1.1\r\n\r\n")
        s.settimeout(5)
        got = s.recv(300)
        s.close()
        assert got.startswith(b"HTTP/1.1 403") and b"not a host this plugin was granted" in got, \
            got
        r.host.stop_plugin(hid)
        assert "tandem-plugin-hello-tool" not in fake.pods
    finally:
        r.close()
        for p in procs:
            if p.poll() is None:
                p.kill()
        fake.close()
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(src, ignore_errors=True)
    return "the pod, its read-only tree, the env file, MCP through exec, its egress, deleted"


if __name__ == "__main__":
    from scriptmode import run
    run(globals())
