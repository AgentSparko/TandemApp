"""The pod sandbox backend (SRV-863, SRV-864) against a fake Kubernetes API (tests/fake_k8s.py): no
cluster, CPU only. The cases follow SRV-863's scenarios and its edge cases: a sandbox is one
labelled, locked-down pod and runs a command; the start check refuses a cluster that does not
enforce NetworkPolicy and a runtimeClass that does not exist; an image the node lacks, a pod that
stays Pending and an API error fail the call with their reason; OOMKilled is out of memory
(through chatd's own loop), an eviction is not; orphans are deleted at start and by the sweep and
the running count is the cluster's; exec carries stdin, a TTY, resizes, exit codes and ends; the
egress proxy knows a sandbox by its address. Also the Docker path's OOM reader on any runtime.

Run: python tests/test_chatd_sandbox_k8s.py
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QSE_SANDBOX_MEM_GB", "1")

from chatd_util import Chat, run_all  # noqa: E402
from fake_k8s import FakeK8s  # noqa: E402

from chatd import k8s, sandbox as sb_mod  # noqa: E402
from chatd.egress import TcpProxy  # noqa: E402
from chatd.k8s import Api, KubeSandboxes  # noqa: E402
from chatd.sandbox import Gone, Unavailable, demux  # noqa: E402

LABELS = {"app.kubernetes.io/component": "chat-sandbox", "app.kubernetes.io/instance": "t"}


class Rig:
    """A fake API with the controller's pod, and the backend over a data dir on its 'volume'."""

    def __init__(self, data: str | None = None, check: bool = True, **env):
        self.tmp = tempfile.mkdtemp(prefix="k8s-sbx-")
        self.fake = FakeK8s()
        ctrl = self.fake.add("ctrl-0", {"app.kubernetes.io/component": "controller"},
                             ip="10.9.0.2")
        ctrl["spec"]["nodeName"] = "node-1"
        with open(os.path.join(self.tmp, "token"), "w") as fh:
            fh.write("test-token\n")
        self.data = data or os.path.join(self.tmp, "vol", "data", "chat")
        os.makedirs(self.data, exist_ok=True)
        self.env = {"QSE_K8S_CLAIM": "t-data", "QSE_K8S_POD": "ctrl-0",
                    "QSE_K8S_VOLUME_ROOT": os.path.dirname(os.path.dirname(self.data)),
                    "QSE_K8S_EGRESS_PORT": "0", "QSE_K8S_HOST_USERS": "false",
                    "QSE_K8S_LABELS": json.dumps(LABELS), "QSE_K8S_CONTROLLER_PORT": "8091",
                    "QSE_K8S_SERVICE_ACCOUNT": "t-sandbox"}
        self.env.update(env)
        self.api = Api(url=self.fake.url, token_file=os.path.join(self.tmp, "token"),
                       namespace="ns", env={})
        self.lines: list[str] = []
        self.ks = KubeSandboxes(self.data, api=self.api, env=self.env, log=self.lines.append)
        self.ks.wait_s, self.ks.poll_s, self.ks.check_wait_s = 2.0, 0.2, 5.0
        self.ks.max_running, self.ks.total_gb = 2, 2.0
        if check:
            assert self.ks.run_check()["state"] == "ok", self.ks.check

    def sandbox_pods(self):
        return [p for p in self.fake.pods.values()
                if p["metadata"]["labels"].get("app.kubernetes.io/component") == "chat-sandbox"
                and "tandem.chat.probe" not in p["metadata"]["labels"]]

    def close(self):
        self.ks.stopping.set()
        if self.ks.egress is not None:
            self.ks.egress.close()
        self.fake.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


def _run(ks, cid, cmd, timeout=20):
    out = []
    code, timed = ks.run(cid, ["bash", "-lc", cmd], timeout=timeout, on_output=out.append)
    return code, "".join(out)


# ---------------------------------------------------------------------- scenario 1
def test_a_sandbox_starts_as_one_labelled_locked_down_pod_and_runs_a_command():
    r = Rig()
    try:
        def on_exec(ctx):
            if ctx.cmd[-1] == "echo hi; exit 3":
                ctx.out(1, b"hi\n")
                ctx.out(2, b"warn\n")
                return 3
            return 0
        r.fake.on_exec = on_exec
        name = r.ks.ensure("CHAT1")
        pods = r.sandbox_pods()
        assert [p["metadata"]["name"] for p in pods] == [name] == ["tandem-chat-chat1"]
        pod = pods[0]
        md, spec = pod["metadata"], pod["spec"]
        c = spec["containers"][0]
        for k, v in LABELS.items():
            assert md["labels"][k] == v
        assert md["labels"]["tandem.chat"] == "CHAT1" and md["annotations"]["tandem.chat/id"] == "CHAT1"
        assert md["labels"]["tandem.chat.instance"] == r.ks.instance
        assert md["annotations"]["tandem.chat/egress"] == f"10.9.0.2:{r.ks.egress_port}"
        assert md["ownerReferences"] == [{"apiVersion": "v1", "kind": "Pod", "name": "ctrl-0",
                                          "uid": "u-ctrl-0"}]
        assert spec["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][
            "nodeSelectorTerms"][0]["matchFields"][0]["values"] == ["node-1"]
        assert spec["automountServiceAccountToken"] is False and spec["hostUsers"] is False
        assert spec["serviceAccountName"] == "t-sandbox" and spec["enableServiceLinks"] is False
        assert spec["restartPolicy"] == "Never" and spec["shareProcessNamespace"] is True
        assert spec["securityContext"] == {"runAsUser": 1000, "runAsGroup": 1000,
                                           "runAsNonRoot": True,
                                           "seccompProfile": {"type": "RuntimeDefault"}}
        assert c["securityContext"] == {"allowPrivilegeEscalation": False, "privileged": False,
                                        "readOnlyRootFilesystem": True,
                                        "capabilities": {"drop": ["ALL"]}}
        assert c["resources"]["limits"] == {"memory": str(sb_mod.GiB), "cpu": "4"}
        assert c["resources"]["requests"]["memory"] == str(sb_mod.GiB)
        assert c["imagePullPolicy"] == "Never" and "runtimeClassName" not in spec
        mounts = {m["mountPath"]: m.get("subPath") for m in c["volumeMounts"]}
        assert mounts == {"/tmp": None, "/workspace": "data/chat/workspaces/CHAT1",
                          "/home/agent": "data/chat/homes/CHAT1"}
        claim = [v for v in spec["volumes"] if v.get("persistentVolumeClaim")]
        assert claim == [{"name": "data", "persistentVolumeClaim": {"claimName": "t-data"}}]
        env = {e["name"]: e["value"] for e in c["env"]}
        assert env["HTTPS_PROXY"] == f"http://10.9.0.2:{r.ks.egress_port}"
        assert spec["dnsConfig"] == {"nameservers": ["127.0.0.1"]}
        # the workspace's git init ran through exec, wrapped; then a command, stdout and stderr
        assert r.fake.execs[0].cmd[-1].startswith("[ -d .git ]")
        assert r.fake.execs[0].argv[6] == "512"          # a user namespace: the process limit
        code, out = _run(r.ks, "CHAT1", "echo hi; exit 3")
        assert (code, out) == (3, "hi\nwarn\n"), (code, out)
        assert r.ks.box("CHAT1").state == "running"
        # stop deletes the pod; the files stay
        r.ks.stop("CHAT1", "user")
        assert r.sandbox_pods() == [] and os.path.isdir(os.path.join(r.data, "workspaces", "CHAT1"))
    finally:
        r.close()


def test_the_pod_names_are_valid_and_distinct():
    r = Rig(check=False)
    try:
        a, b = "X" * 55 + "_one", "X" * 55 + "_two"
        na, nb = r.ks.name(a), r.ks.name(b)
        assert na != nb and len(na) <= 63 and len(nb) <= 63
        import re
        assert re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", na), na
        assert r.ks.name("01ABCDEF") == "tandem-chat-01abcdef"
    finally:
        r.close()


def test_a_runtime_class_and_host_users_reach_the_pod():
    r = Rig(QSE_K8S_RUNTIME_CLASS="gvisor", QSE_K8S_HOST_USERS="true")
    try:
        r.ks.ensure("C")
        spec = r.sandbox_pods()[0]["spec"]
        assert spec["runtimeClassName"] == "gvisor" and "hostUsers" not in spec
        assert r.fake.execs[-1].argv[6] == "0"          # no user namespace: no RLIMIT_NPROC
    finally:
        r.close()


# ---------------------------------------------------------------------- the start check
def test_a_cluster_that_does_not_enforce_network_policy_gets_no_sandboxes():
    r = Rig(check=False)
    try:
        r.fake.probe = {"controller": True, "egress": True, "api": True}
        chk = r.ks.run_check()
        assert chk["state"] == "error" and "does not enforce NetworkPolicy" in chk["why"], chk
        assert "isolation is not enforced" in chk["why"]
        assert r.ks.available() is False
        try:
            r.ks.ensure("C")
            raise AssertionError("a sandbox started")
        except Unavailable as exc:
            assert "does not enforce NetworkPolicy" in str(exc)
        assert r.sandbox_pods() == [] and r.ks.status("C")["error"] == chk["why"]
        assert not any("tandem.chat.probe" in p["metadata"]["labels"]
                       for p in r.fake.pods.values())          # the probe pod is deleted
        assert any("no sandboxes" in ln for ln in r.lines)
    finally:
        r.close()


def test_the_health_report_says_the_backend_and_the_check_state():
    """SRV-835: chatd's /health `sandbox` (KubeSandboxes.report): ready after the check passed,
    unavailable with the check's words, unknown while it has not finished; never waits."""
    r = Rig()
    try:
        assert r.ks.report() == {"backend": "kubernetes", "state": "ready", "why": None}
    finally:
        r.close()
    r = Rig(check=False)
    try:
        r.ks.check["at"] = time.monotonic()            # a check that runs: nothing new starts
        r.ks._check_lock.acquire()
        try:
            assert r.ks.report() == {"backend": "kubernetes", "state": "unknown", "why": None}
        finally:
            r.ks._check_lock.release()
        r.fake.probe = {"controller": True, "egress": True, "api": True}
        r.ks.run_check()
        got = r.ks.report()
        assert got["backend"] == "kubernetes" and got["state"] == "unavailable", got
        assert "does not enforce NetworkPolicy" in got["why"], got
    finally:
        r.close()


def test_the_docker_report_never_waits_on_the_socket():
    """SRV-835: the docker backend's report answers from the last check (60 s), unknown before
    the first, and checks again in the background."""
    calls = []

    class Engine:
        def call(self, method, url, timeout=None, **kw):
            calls.append(url)
            return (200, b"{}") if engine_ok[0] else (404, b"{}")
    engine_ok = [True]
    t = [1000.0]
    sb = sb_mod.Sandboxes(tempfile.mkdtemp(prefix="dk-sbx-"), engine=Engine(), clock=lambda: t[0])
    first = sb.report()
    assert first == {"backend": "docker", "state": "unknown", "why": None}, first
    for _ in range(100):
        if sb._avail is not None:
            break
        time.sleep(0.02)
    assert sb.report() == {"backend": "docker", "state": "ready", "why": None}
    engine_ok[0] = False
    t[0] += 61
    stale = sb.report()                                   # the old answer, then a new check
    assert stale["state"] == "ready", stale
    for _ in range(100):
        if sb._avail is not None and not sb._avail[1]:
            break
        time.sleep(0.02)
    got = sb.report()
    assert got["state"] == "unavailable" and "tandem-sandbox" in got["why"], got


def test_a_runtime_class_that_does_not_exist_is_refused_at_start():
    r = Rig(check=False, QSE_K8S_RUNTIME_CLASS="missing")
    try:
        chk = r.ks.run_check()
        assert chk["state"] == "error" and "runtimeClass 'missing' does not exist" in chk["why"]
        assert r.ks.available() is False
    finally:
        r.close()


def test_the_api_unreachable_or_failing_fails_the_call_with_the_reason():
    r = Rig(check=False)
    try:
        r.ks.api = Api(url="http://127.0.0.1:1", token_file=os.path.join(r.tmp, "token"),
                       namespace="ns", env={})
        r.ks.pod_self = ""                 # no own pod to read either
        t0 = time.monotonic()
        chk = r.ks.run_check()
        assert chk["state"] == "error" and time.monotonic() - t0 < 10, chk
        r2 = Rig()
        try:
            r2.fake.fail_create = 500
            t0 = time.monotonic()
            try:
                r2.ks.ensure("C")
                raise AssertionError("started")
            except Unavailable as exc:
                assert "500" in str(exc) and "etcd is down" in str(exc), exc
            assert time.monotonic() - t0 < 5 and r2.ks.box("C").state == "error"
        finally:
            r2.close()
    finally:
        r.close()


# ---------------------------------------------------------------------- refusals while starting
def test_an_image_the_node_lacks_is_a_clear_message_and_the_pod_goes():
    r = Rig()
    try:
        r.fake.mode = "image"
        try:
            r.ks.ensure("C")
            raise AssertionError("started")
        except Unavailable as exc:
            assert "is not on this node" in str(exc) and "build-k8s.sh" in str(exc), exc
        assert r.sandbox_pods() == []
    finally:
        r.close()


def test_a_pod_that_stays_pending_is_capacity_and_is_deleted_after_the_wait():
    r = Rig()
    try:
        r.fake.mode = "pending"
        t0 = time.monotonic()
        try:
            r.ks.ensure("C")
            raise AssertionError("started")
        except Unavailable as exc:
            assert "no room" in str(exc) and "Insufficient memory" in str(exc), exc
            assert exc.data["sandbox"]["reason"] == "capacity"
        assert 2.0 <= time.monotonic() - t0 < 6 and r.sandbox_pods() == []
    finally:
        r.close()


# ---------------------------------------------------------------------- memory
def test_oom_killed_is_reported_as_out_of_memory_through_chatds_loop():
    fake_holder = {}
    c = Chat()
    r = Rig(data=os.path.join(tempfile.mkdtemp(prefix="k8s-vol-"), "data", "chat"))
    try:
        ks = r.ks
        ks.on_change = c.svc._sandbox_changed
        ks.network_mode = c.svc._network_mode
        # chatd's own data is c.data; the backend's folders must be on the claim: use the rig's
        ks.data = r.data
        c.svc.sandboxes = ks
        c.svc._caps = None
        for n in [t["id"] for t in c.ok("GET", "/tools")]:
            c.svc.add_rule(n, None, "allow", None)

        def on_exec(ctx):
            if "bytearray" in ctx.cmd[-1]:
                r.fake.oom(ctx.pod["metadata"]["name"])
                return 137
            if ctx.cmd[-1] == "cat /workspace/a.txt":
                ctx.out(1, b"kept\n")
            return 0
        r.fake.on_exec = on_exec
        fake_holder["r"] = r
        cid = c.new()
        c.send(cid, "FAKE_CALLS " + json.dumps(
            [{"name": "bash", "arguments": {"command": "python3 -c 'x = bytearray(3 << 30)'"}}]))
        s = c.wait_idle(cid, 30)
        part = [p for m in s["messages"] for p in m["parts"] if p["type"] == "tool_call"][-1]
        out = part["output"]["content"]
        assert "out of memory (1 GiB sandbox limit)" in out, out
        # the next call: the pod is gone, a new one starts with the files, and the result says so
        c.send(cid, "FAKE_CALLS " + json.dumps(
            [{"name": "bash", "arguments": {"command": "cat /workspace/a.txt"}}]))
        s = c.wait_idle(cid, 30)
        part = [p for m in s["messages"] for p in m["parts"] if p["type"] == "tool_call"][-1]
        out = part["output"]["content"]
        assert "the sandbox was restarted" in out and "memory limit" in out and "kept" in out, out
    finally:
        c.close()
        r.close()


def test_an_eviction_is_reported_as_such_not_as_out_of_memory():
    r = Rig()
    try:
        name = r.ks.ensure("C")
        before = r.ks.oom_kills("C")
        r.fake.evict(name)
        assert r.ks.oom_kills("C") == before == 0
        r.ks.ensure("C")                           # gone: a new pod
        words = r.ks.take_restarted("C")
        assert "evicted" in words and "not this sandbox's own limit" in words, words
        assert r.fake.pods[name]["status"]["phase"] == "Running"
    finally:
        r.close()


def test_the_pods_own_oom_count_is_read_while_it_runs():
    r = Rig()
    try:
        r.ks.ensure("C")

        def on_exec(ctx):
            if ctx.cmd[:1] == ["cat"] and ctx.cmd[-1].endswith("memory.events"):
                ctx.out(1, b"low 0\nhigh 0\nmax 4\noom 2\noom_kill 2\n")
            elif ctx.cmd[:1] == ["cat"]:
                ctx.out(1, b"104857600\nanon 1\ninactive_file 4857600\n")
            return 0
        r.fake.on_exec = on_exec
        assert r.ks.oom_kills("C") == 2
        r.ks.box("C").mem = (0.0, None)            # past the 15 s the reading is kept
        assert r.ks.memory_used(r.ks.box("C")) == 100_000_000
    finally:
        r.close()


# ---------------------------------------------------------------------- orphans and limits
def test_orphans_go_at_start_and_by_the_sweep_and_the_count_is_the_clusters():
    r = Rig(check=False)
    try:
        r.ks._whoami()
        r.ks.start_egress()
        here = f"10.9.0.2:{r.ks.egress_port}"
        inst = r.ks.instance
        mine = dict(LABELS, **{"tandem.chat.instance": inst})
        r.fake.add("tandem-chat-a", dict(mine, **{"tandem.chat": "A"}),
                   {"tandem.chat/id": "A", "tandem.chat/egress": here}, ip="10.9.1.1")
        r.fake.add("tandem-chat-b", dict(mine, **{"tandem.chat": "B"}),
                   {"tandem.chat/id": "B", "tandem.chat/egress": "10.9.0.99:3128"})
        r.fake.add("tandem-chat-c", dict(mine, **{"tandem.chat": "C"}),
                   {"tandem.chat/id": "C", "tandem.chat/egress": here}, phase="Failed",
                   terminated={"reason": "OOMKilled"})
        r.fake.add("tandem-probe-x", dict(mine, **{"tandem.chat.probe": "1"}))
        other = dict(LABELS, **{"tandem.chat.instance": "someone-else", "tandem.chat": "D"})
        r.fake.add("tandem-chat-d", other, {"tandem.chat/id": "D"})
        r.fake.mode = "running"
        assert r.ks.adopt() == 1
        assert r.ks.box("A").state == "running" and r.ks.box("A").ip == "10.9.1.1"
        assert sorted(r.fake.pods) == ["ctrl-0", "tandem-chat-a", "tandem-chat-d"] or \
            sorted(n for n in r.fake.pods if not n.startswith("tandem-probe-")) == \
            ["ctrl-0", "tandem-chat-a", "tandem-chat-d"], sorted(r.fake.pods)
        # the cluster's pods count: two of this chatd's pods the table does not know
        r.ks.check.update(state="ok")
        r.fake.add("tandem-chat-e", dict(mine, **{"tandem.chat": "E"}),
                   {"tandem.chat/id": "E", "tandem.chat/egress": here})
        r.ks._forget_pods()
        r.ks.box("A").busy = 1
        why = r.ks.admit()
        assert why and "2 sandbox(es)" in why, why
        # the sweep: E has no running sandbox here; A stops being one
        r.ks.box("A").busy = 0
        r.ks.box("A").state = "stopped"
        gone = r.ks.sweep()
        assert sorted(gone) == ["tandem-chat-a", "tandem-chat-e"], gone
        assert "tandem-chat-d" in r.fake.pods                 # another chatd's: untouched
        assert r.ks.admit() is None
    finally:
        r.close()


def test_the_running_limit_stops_an_idle_sandbox_to_make_room():
    r = Rig()
    try:
        r.ks.ensure("A")
        r.ks.ensure("B")
        r.ks.box("A").last_used -= 100
        r.ks.ensure("C")
        assert r.ks.box("A").state == "stopped" and r.ks.box("A").stopped_reason == "idle"
        assert sorted(p["metadata"]["name"] for p in r.sandbox_pods()) == \
            ["tandem-chat-b", "tandem-chat-c"]
    finally:
        r.close()


# ---------------------------------------------------------------------- exec
def test_exec_carries_stdin_a_tty_resizes_exit_codes_and_ends():
    r = Rig()
    try:
        r.ks.ensure("C")
        ended = threading.Event()

        def on_exec(ctx):
            if ctx.cmd[:2] == ["python3", "-c"] and ctx.cmd[-1] in ("one", "tree"):
                assert ctx.cmd[-2].startswith(k8s.PIDDIR + "/")
                ended.set()
                return 0
            if ctx.cmd == ["cat"]:
                ctx.out(1, ctx.stdin()[::-1])
                return 0
            if ctx.cmd == ["bash", "-l"]:
                assert ctx.tty
                ctx.out(1, b"\x1b[32msandbox\x1b[0m$ ")
                ctx.frames(1.0)
                return 0
            if ctx.cmd[-1] == "serve forever":
                ended.wait(20)
                return 143
            return 0
        r.fake.on_exec = on_exec
        eid, s, first = r.ks.open_exec("C", ["cat"], stdin=True)
        s.sendall(b"hello")
        s.shutdown(socket.SHUT_WR)
        got = []
        demux(s, first, lambda st, d: got.append(d) if st == 1 else None)
        assert b"".join(got) == b"olleh" and r.ks.exit_code(eid) == (False, 0, None)
        # a TTY: raw bytes, and a resize reaches the stream
        eid, s, first = r.ks.open_exec("C", ["bash", "-l"], tty=True, stdin=True)
        r.ks.resize(eid, 120, 40)
        s.settimeout(5)
        buf = s.recv(100)
        assert buf.startswith(b"\x1b[32msandbox"), buf
        while s.recv(100):
            pass
        assert r.fake.execs[-1].resizes == [{"Width": 120, "Height": 40}]
        # end_exec: a second exec signals the first one's pid; its stream then ends
        eid, s, first = r.ks.open_exec("C", ["bash", "-lc", "serve forever"])
        time.sleep(0.2)
        assert r.ks.exit_code(eid)[0] is True
        r.ks.end_exec(eid)
        demux(s, first, lambda st, d: None)
        assert ended.is_set() and r.ks.exit_code(eid) == (False, 143, None)
        # a run past its timeout is killed (the tree) and says it timed out
        ended.clear()
        code, timed = r.ks.run("C", ["bash", "-lc", "serve forever"], timeout=0.5)
        assert timed and code == 143 and ended.is_set()
    finally:
        r.close()


def test_an_exec_into_a_gone_or_ended_pod_is_gone():
    r = Rig()
    try:
        name = r.ks.ensure("C")
        r.fake.oom(name)
        try:
            r.ks.open_exec("C", ["true"])
            raise AssertionError("an exec ran")
        except Gone:
            pass
        assert r.ks.box("C").state == "stopped" and r.ks.box("C").stopped_reason == "gone"
        r.ks.ensure("C")
        del r.fake.pods[name]
        try:
            r.ks.open_exec("C", ["true"])
            raise AssertionError("an exec ran")
        except Gone:
            pass
    finally:
        r.close()


# ---------------------------------------------------------------------- egress
def test_the_egress_proxy_knows_a_sandbox_by_its_address():
    r = Rig()
    try:
        r.ks.ensure("C")
        port = r.ks.egress_port

        def ask(line: bytes) -> bytes:
            s = socket.create_connection(("127.0.0.1", port), 5)
            s.sendall(line)
            s.settimeout(5)
            got = b""
            while True:
                d = s.recv(4096)
                if not d:
                    break
                got += d
            s.close()
            return got
        got = ask(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        assert got.startswith(b"HTTP/1.1 403") and b"not a sandbox" in got, got
        r.ks.box("C").ip = "127.0.0.1"
        got = ask(b"CONNECT 10.0.0.1:443 HTTP/1.1\r\nHost: 10.0.0.1\r\n\r\n")
        assert got.startswith(b"HTTP/1.1 403") and b"local network address" in got, got
        r.ks.network_mode = lambda cid: "off"
        got = ask(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        assert b"the network is off" in got, got
        r.ks.stop("C")
        got = ask(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        assert b"not a sandbox" in got, got
        assert r.ks.egress.stats["unknown"] == 2
    finally:
        r.close()


def test_the_tcp_proxy_alone():
    p = TcpProxy("127.0.0.1", 0, lambda ip: None)
    p.start()
    try:
        s = socket.create_connection(("127.0.0.1", p.port), 5)
        s.settimeout(5)
        assert s.recv(200).startswith(b"HTTP/1.1 403")
        s.close()
    finally:
        p.close()


# ---------------------------------------------------------------------- files
def test_the_chat_folders_get_acls_for_both_uids():
    d = tempfile.mkdtemp(prefix="k8s-acl-")
    try:
        os.makedirs(os.path.join(d, "ws", "sub"))
        with open(os.path.join(d, "ws", "sub", "f.txt"), "w") as fh:
            fh.write("x")
        n = k8s.grant_tree(os.path.join(d, "ws"), (1000, os.getuid()))
        if not k8s.has_grant(os.path.join(d, "ws"), 1000):
            print("  (this file system takes no POSIX ACLs: only the walk is checked)")
            assert n == 0
            return
        assert n == 3
        raw = os.getxattr(os.path.join(d, "ws", "sub", "f.txt"), "system.posix_acl_access")
        assert (2, 6, 1000) in [__import__("struct").unpack("<HHI", raw[i:i + 8])
                                for i in range(4, len(raw), 8)]
        os.makedirs(os.path.join(d, "ws", "new"))
        assert k8s.has_grant(os.path.join(d, "ws", "new"), 1000)       # inherited
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------- the Docker path
class _Eng:
    def __init__(self, pid):
        self.pid = pid

    def call(self, method, path, body=None, timeout=60.0):
        return 200, {"State": {"Pid": self.pid, "Running": True}}


def test_the_docker_paths_oom_reader_finds_any_runtimes_cgroup():
    tmp = tempfile.mkdtemp(prefix="cg-")
    old = sb_mod.CGROUP
    sb_mod.CGROUP = tmp
    try:
        # by the container's first process: whatever the runtime named its cgroup
        rel = sb_mod._proc_cgroup(os.getpid())
        sb = sb_mod.Sandboxes(os.path.join(tmp, "data"), engine=_Eng(os.getpid()))
        b = sb.box("C")
        b.container = "abc123"
        if rel is not None:
            os.makedirs(os.path.join(tmp, rel.lstrip("/")), exist_ok=True)
            with open(os.path.join(tmp, rel.lstrip("/"), "memory.events"), "w") as fh:
                fh.write("low 0\nhigh 0\nmax 1\noom 1\noom_kill 3\n")
            assert sb.oom_kills("C") == 3
        else:
            print("  (no cgroup v2 path for this process: the pid lookup is not checked)")
        # rootful Podman's layout, with no pid to go by
        sb2 = sb_mod.Sandboxes(os.path.join(tmp, "data2"), engine=_Eng(0))
        b2 = sb2.box("C")
        b2.container = "def456"
        p = os.path.join(tmp, "machine.slice", "libpod-def456.scope")
        os.makedirs(p)
        with open(os.path.join(p, "memory.events"), "w") as fh:
            fh.write("oom_kill 1\n")
        assert sb2.oom_kills("C") == 1
        with open(os.path.join(p, "memory.events"), "w") as fh:
            fh.write("oom_kill 2\n")
        assert sb2.oom_kills("C") == 2                 # the path is kept, the count read again
        b3 = sb2.box("D")
        b3.container = "nothing-here"
        assert sb2.oom_kills("D") is None
    finally:
        sb_mod.CGROUP = old
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_backend_is_chosen_by_the_variable():
    assert sb_mod.backend_name({}) == "docker"
    assert sb_mod.backend_name({"QSE_SANDBOX_BACKEND": "kubernetes"}) == "kubernetes"
    assert sb_mod.backend_name({"QSE_SANDBOX_BACKEND": "podman"}) == "docker"


def test_the_exit_code_of_a_status():
    assert k8s.exit_of({"status": "Success"}) == 0
    assert k8s.exit_of({"status": "Failure", "reason": "NonZeroExitCode", "details": {
        "causes": [{"reason": "ExitCode", "message": "137"}]}}) == 137
    assert k8s.exit_of({"status": "Failure", "message": "container not found"}) is None


if __name__ == "__main__":
    run_all(globals())
