"""Chat sandboxes as Kubernetes pods on a real cluster (SRV-863, SRV-864): the e2e cases. Skipped
unless QSE_SANDBOX_K8S_BOX=1. It runs inside the controller container of a throwaway release with
`sandbox.mode: kubernetes` (never a release in use: it makes and deletes sandbox pods, stops and
starts chatd), on a cluster that enforces NetworkPolicy. That release's chatd is driven through
its own HTTP API with a fake model (tests/fake_backend.py on 127.0.0.1:18000, the release's
QSE_CHAT_ENGINE), so every call goes the real way: chatd's loop, the kubernetes backend, the pod.

    QSE_SANDBOX_K8S_BOX=1 $PY tests/test_sandbox_k8s_box.py

The cases are SRV-863's scenarios: a sandbox starts as a pod and runs a command (labels, spec,
a file chatd reads); the Role (no secrets, no other namespace); the network (the API, the
controller, another sandbox, the node and the metadata address refused, the internet through the
proxy, the proxy refusing an address that is no sandbox); the memory limit (out of memory, the
other sandbox and the controller unaffected); at most QSE_SANDBOX_MAX_RUNNING pods; orphans after
a restart; a runtimeClass that does not exist refused by the start check; delete removes the pod.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ON = os.environ.get("QSE_SANDBOX_K8S_BOX") == "1"
CHATD = os.environ.get("QSE_K8S_BOX_CHATD", "127.0.0.1:8016")
CONTROLLER = os.environ.get("QSE_K8S_BOX_CONTROLLER", "127.0.0.1:8091")


def _env_file() -> dict:
    from proxy.appchat import read_env_file
    return read_env_file(os.path.join(os.environ.get("TANDEM_APP_STATE", ""), "chatd.env"))


class Client:
    def __init__(self, data: str):
        with open(os.path.join(data, "forward.key")) as fh:
            self.key = fh.read().strip()
        self.data = data

    def call(self, method, path, body=None):
        host, _, port = CHATD.rpartition(":")
        c = http.client.HTTPConnection(host, int(port), timeout=60)
        c.request(method, "/v1/chat-app" + path, body=json.dumps(body).encode() if body is not None
                  else None, headers={"X-Tandem-Forward": self.key,
                                      "Content-Type": "application/json"})
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, (json.loads(raw) if raw else None)

    def ok(self, method, path, body=None):
        st, doc = self.call(method, path, body)
        assert st in (200, 202, 204), (method, path, st, doc)
        return doc

    def new(self) -> str:
        return self.ok("POST", "/conversations", {})["id"]

    def bash(self, cid, *commands, timeout=240):
        calls = [{"name": "bash", "arguments": {"command": c}} for c in commands]
        self.ok("POST", f"/conversations/{cid}/messages",
                {"client_msg_id": f"cm-{time.monotonic_ns()}",
                 "text": "FAKE_CALLS " + json.dumps(calls)})
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            s = self.ok("GET", f"/conversations/{cid}")
            if s["run"] is None and s["messages"] and s["messages"][-1]["status"] != "streaming":
                parts = [p for m in s["messages"] if m["role"] == "assistant"
                         for p in m["parts"] if p["type"] == "tool_call"]
                return parts[-len(commands):]
            time.sleep(0.5)
        raise AssertionError(f"chat {cid} did not end in {timeout} s")


def _out(part) -> str:
    return ((part.get("output") or {}).get("content") or "")


def _api():
    from chatd.k8s import Api
    env = _env_file()
    return Api(url=env.get("QSE_K8S_API"), namespace=env.get("QSE_K8S_NAMESPACE"))


def _pods(api, selector: str) -> list[dict]:
    import urllib.parse
    st, doc = api.call("GET", api.pods("?labelSelector=" + urllib.parse.quote(selector)))
    assert st == 200, (st, doc)
    return doc["items"]


def _sandbox_pods(api, env) -> list[dict]:
    labels = json.loads(env["QSE_K8S_LABELS"])
    sel = ",".join(f"{k}={v}" for k, v in labels.items())
    return [p for p in _pods(api, sel) if "tandem.chat.probe" not in p["metadata"]["labels"]
            and not p["metadata"].get("deletionTimestamp")]


def _wait(pred, timeout=60.0, step=1.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        v = pred()
        if v:
            return v
        time.sleep(step)
    raise AssertionError("timed out")


def _chatd_restart():
    """chatd restarted through its controller (POST /v1/fleet/chat/restart)."""
    host, _, port = CONTROLLER.rpartition(":")
    tok = os.environ.get("TANDEM_ADMIN_TOKEN", "")
    c = http.client.HTTPConnection(host, int(port), timeout=30)
    c.request("POST", "/v1/fleet/chat/restart", body=b"{}",
              headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    r = c.getresponse()
    r.read()
    assert r.status in (200, 202), r.status
    time.sleep(3)

    def up():
        try:
            h, _, p = CHATD.rpartition(":")
            cc = http.client.HTTPConnection(h, int(p), timeout=5)
            cc.request("GET", "/health")
            ok = cc.getresponse().status == 200
            cc.close()
            return ok
        except OSError:
            return False
    _wait(up, 60)


def test_pod_sandboxes_on_this_cluster():
    if not ON:
        print("skipped: set QSE_SANDBOX_K8S_BOX=1 in a throwaway release's controller")
        return
    env = _env_file()
    assert env.get("QSE_SANDBOX_BACKEND") == "kubernetes", "not a sandbox.mode kubernetes release"
    data = env.get("QSE_CHAT_DATA")
    cl = Client(data)
    api = _api()
    labels = json.loads(env["QSE_K8S_LABELS"])
    caps = cl.ok("GET", "/capabilities")
    _wait(lambda: cl.ok("GET", "/capabilities")["session"]["sandbox"] == "ready", 120, 2)
    print("chatd: the cluster check passed; sandbox ready")

    # ---- 1. a sandbox starts as a pod and runs a command
    a = cl.new()
    t0 = time.monotonic()
    p = cl.bash(a, "id -u; echo from-the-pod > /workspace/k8s.txt; "
                   "grep -E '^(NoNewPrivs|Seccomp|CapEff):' /proc/self/status; "
                   "touch /etc/x 2>&1 | head -1; cat /sys/fs/cgroup/memory.max "
                   "/sys/fs/cgroup/memory.swap.max /sys/fs/cgroup/cpu.max")[0]
    out = _out(p)
    print(f"[1] bash in the pod after {time.monotonic() - t0:.1f} s: {out!r}")
    lines = out.splitlines()
    assert p["status"] == "done" and lines[0] == "1000", out
    assert "NoNewPrivs:\t1" in out and "Seccomp:\t2" in out and "CapEff:\t0000000000000000" in out
    assert "Read-only file system" in out, out
    limit = int(float(env.get("QSE_SANDBOX_MEM_GB") or 2) * 1024 ** 3)
    assert str(limit) in lines and "0" in lines and "400000 100000" in out, out
    with open(os.path.join(data, "workspaces", a, "k8s.txt")) as fh:
        assert fh.read() == "from-the-pod\n"
    pods = _sandbox_pods(api, env)
    mine = [x for x in pods if x["metadata"]["annotations"].get("tandem.chat/id") == a]
    assert len(mine) == 1, [x["metadata"]["name"] for x in pods]
    pod = mine[0]
    for k, v in labels.items():
        assert pod["metadata"]["labels"][k] == v
    spec = pod["spec"]
    c = spec["containers"][0]
    assert spec["automountServiceAccountToken"] is False and spec.get("hostUsers") is False
    assert spec["securityContext"]["runAsUser"] == 1000 and spec["restartPolicy"] == "Never"
    assert spec["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    assert c["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert c["securityContext"]["readOnlyRootFilesystem"] is True
    assert c["securityContext"]["allowPrivilegeEscalation"] is False
    assert c["resources"]["limits"]["memory"] == str(limit) and c["resources"]["limits"]["cpu"] == "4"
    assert not any(v.get("projected") or v.get("secret") for v in spec["volumes"])
    assert [m["subPath"] for m in c["volumeMounts"] if m.get("subPath")] == \
        [f"data/chat/workspaces/{a}", f"data/chat/homes/{a}"]
    print(f"[1] one pod {pod['metadata']['name']} with the chart's labels; spec locked down; "
          f"chatd reads /workspace/k8s.txt")
    p = cl.bash(a, "for i in $(seq 600); do sleep 30 & done 2>/dev/null; sleep 1; "
                   "ls /proc | grep -c '^[0-9]'")[0]
    n = int(_out(p).strip().splitlines()[-1])
    assert n <= 520, n
    cl.bash(a, "pkill sleep; true")
    print(f"[1] a fork storm stops at {n} processes (512 per pod)")

    # ---- 2. the Role: no secrets, no other namespace
    st, _ = api.call("GET", f"/api/v1/namespaces/{api.namespace}/secrets")
    st2, _ = api.call("GET", "/api/v1/namespaces/default/pods")
    st3, _ = api.call("GET", "/api/v1/nodes")
    assert (st, st2, st3) == (403, 403, 403), (st, st2, st3)
    print("[2] the Role: secrets 403, another namespace 403, nodes 403")

    # ---- 3. the network
    b = cl.new()
    cl.bash(b, "true")
    pods = _sandbox_pods(api, env)
    pa = next(x for x in pods if x["metadata"]["annotations"]["tandem.chat/id"] == a)
    pb = next(x for x in pods if x["metadata"]["annotations"]["tandem.chat/id"] == b)
    egress = pa["metadata"]["annotations"]["tandem.chat/egress"]
    ctrl_ip = egress.rpartition(":")[0]
    api_ip = os.environ.get("KUBERNETES_SERVICE_HOST", "")
    node_ip = pa["status"]["hostIP"]
    targets = {"the Kubernetes API": f"https://{api_ip}:443/",
               "the controller": f"http://{ctrl_ip}:{CONTROLLER.rpartition(':')[2]}/health",
               "another sandbox": f"http://{pb['status']['podIP']}:8000/",
               "the node (LAN)": f"http://{node_ip}:80/",
               "the metadata address": "http://169.254.169.254/"}
    cl.bash(b, "nohup python3 -m http.server 8000 --bind 0.0.0.0 >/tmp/s.log 2>&1 & sleep 1")
    for what, url in targets.items():
        p = cl.bash(a, f"curl -sS -k -m 6 --noproxy '*' -o /dev/null -w '%{{http_code}}' {url}; "
                       "echo \" rc=$?\"")[0]
        o = _out(p).strip()
        assert "rc=0" not in o, (what, o)
        print(f"[3] {what}: refused ({o.splitlines()[-1][:80]})")
    for what, url in (("the node (LAN)", f"http://{node_ip}:80/"),
                      ("the controller", f"http://{ctrl_ip}:8091/health")):
        p = cl.bash(a, f"curl -sS -m 10 -o /dev/null -w '%{{http_code}}' {url}")[0]
        assert _out(p).strip().endswith("403"), (what, _out(p))
    print("[3] through the proxy: the node and the controller 403")
    p = cl.bash(a, "curl -sS -m 30 -o /dev/null -w '%{http_code}' https://pypi.org/simple/pip/")[0]
    assert _out(p).strip() == "200", _out(p)
    print("[3] through the proxy: https://pypi.org 200")
    s = socket.create_connection((ctrl_ip, int(env.get("QSE_K8S_EGRESS_PORT") or 3128)), 5)
    s.sendall(b"CONNECT pypi.org:443 HTTP/1.1\r\nHost: pypi.org:443\r\n\r\n")
    got = s.recv(200)
    s.close()
    assert got.startswith(b"HTTP/1.1 403") and b"not a sandbox" in got, got
    print("[3] the proxy refuses an address that is no sandbox (the controller's own)")

    # ---- 4. the memory limit
    p = cl.bash(a, "python3 -c 'x = bytearray(3 * 1024 ** 3)'; echo exit=$?")[0]
    o = _out(p)
    print(f"[4] 3 GiB in a {limit / 1024 ** 3:g} GiB sandbox: {o[-160:]!r}")
    assert "out of memory" in o, o
    pa2 = [x for x in _sandbox_pods(api, env) if x["metadata"]["annotations"]["tandem.chat/id"] == a]
    pb2 = [x for x in _sandbox_pods(api, env) if x["metadata"]["annotations"]["tandem.chat/id"] == b]
    assert pb2 and pb2[0]["status"]["phase"] == "Running", "the other sandbox was touched"
    assert not pa2 or pa2[0]["status"]["phase"] != "Running" or \
        pa2[0]["status"]["containerStatuses"][0]["state"].get("terminated")
    host, _, port = CONTROLLER.rpartition(":")
    hc = http.client.HTTPConnection(host, int(port), timeout=5)
    hc.request("GET", "/health")
    assert hc.getresponse().status == 200
    p = cl.bash(a, "cat /workspace/k8s.txt")[0]
    assert "from-the-pod" in _out(p) and "restarted" in _out(p), _out(p)
    print("[4] out of memory reported; the other sandbox runs, the controller answers; the next "
          "call gets a new pod with the files")

    # ---- 5. at most QSE_SANDBOX_MAX_RUNNING pods
    most = int(env.get("QSE_SANDBOX_MAX_RUNNING") or 2)
    seen = []
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            seen.append(len([x for x in _sandbox_pods(api, env)
                             if x["status"].get("phase") in ("Pending", "Running")]))
            time.sleep(0.5)
    w = threading.Thread(target=watch, daemon=True)
    w.start()
    chats = [a, b] + [cl.new() for _ in range(most)]
    res = {}
    ts = [threading.Thread(target=lambda cid=cid: res.setdefault(
        cid, cl.bash(cid, "sleep 20; echo done-sleeping", timeout=300)[0])) for cid in chats]
    for t in ts:
        t.start()
        time.sleep(0.3)
    for t in ts:
        t.join(320)
    stop.set()
    w.join(5)
    busy = [cid for cid, p in res.items() if "done-sleeping" not in _out(p)]
    print(f"[5] {len(chats)} chats at once, at most {max(seen)} pods seen (limit {most}); "
          f"{len(busy)} call(s) said busy: {[_out(res[c])[:100] for c in busy][:1]}")
    assert max(seen) <= most, seen
    assert busy and all("sandbox" in _out(res[c]) for c in busy)

    # ---- 6. orphans after a restart
    import secrets as _s
    from chatd.k8s import KubeSandboxes
    ghost = "GHOST" + _s.token_hex(4).upper()
    ks = KubeSandboxes(data, api=api, env={**env, "QSE_K8S_EGRESS_PORT": "0"})
    ks.egress_host = egress.rpartition(":")[0]
    ks.egress_port = int(egress.rpartition(":")[2])
    spec = ks.pod_spec(ks.name(ghost), probe=False)
    spec["metadata"]["labels"]["tandem.chat"] = ghost
    spec["metadata"]["annotations"]["tandem.chat/id"] = ghost
    st, doc = api.call("POST", api.pods(), spec)
    assert st in (200, 201), (st, doc)
    _wait(lambda: any(x["metadata"]["name"] == ks.name(ghost) for x in _sandbox_pods(api, env)))
    _chatd_restart()

    def settled():
        names = [x["metadata"]["name"] for x in _sandbox_pods(api, env)]
        return names if ks.name(ghost) not in names else None
    left = _wait(settled, 90, 2)
    print(f"[6] after a chatd restart the orphan pod is gone; pods left {left}")
    running = [cid for cid in chats if cl.ok("GET", f"/conversations/{cid}")["sandbox"]["state"]
               == "running"]
    assert sorted(ks.name(c) for c in running) == sorted(left), (running, left)
    print(f"[6] the running count matches the cluster's pods ({len(left)})")

    # ---- 7. a runtimeClass that does not exist
    bad = KubeSandboxes(data, api=api, env={**env, "QSE_K8S_EGRESS_PORT": "0",
                                            "QSE_K8S_RUNTIME_CLASS": "no-such-class"})
    bad.egress_host = ks.egress_host
    chk = bad.run_check()
    bad.egress.close()
    assert chk["state"] == "error" and "no-such-class" in chk["why"], chk
    print(f"[7] runtimeClass no-such-class: {chk['why'][:140]}")

    # ---- 8. delete removes the pod
    for cid in chats:
        cl.call("DELETE", f"/conversations/{cid}?forever=1")
    _wait(lambda: not _sandbox_pods(api, env), 60, 2)
    print("[8] the chats deleted: no sandbox pod left")
    ks.egress and ks.egress.close()
    assert caps


if __name__ == "__main__":
    test_pod_sandboxes_on_this_cluster()
    print("ok")
