# TandemApp Helm chart

TandemApp's controller (the gateway, the usage ledger, the fleet and the page) in one pod, with its
own chatd once Chat is on. Nothing is built into an image: every container runs a stock Python
image (`image.*`, pinned by digest), and an init container unpacks a release archive into the
volume and makes a virtualenv there from the archive's own wheels.

The defaults are bare (OPS-288): the gateway only, Chat and the conductor off, no MCP bridge, no
sandbox. Each is switched on from the page (Settings, App, What runs here) or with the values below.

## Install

The whole procedure, tested end to end: [docs/install-kubernetes.md](../../docs/install-kubernetes.md).
In short:

1. Build the release archive from a commit and the page built from it (reproducible with
   `WHEELS=<a folder of wheels>`):

   ```
   python3 tools/dashboard_bundle.py build
   bash charts/tandemapp/scripts/make-archive.sh HEAD dashboard/dist dist-chart
   ```

   It prints `release.id` and the archive's sha256.

2. Install. Either the init container fetches the archive (`release.archive.url` and `.sha256`),
   or it waits for it on the volume and NOTES prints the `kubectl cp` line:

   ```
   helm install tandemapp charts/tandemapp -n tandemapp --create-namespace \
     --set release.id=<id> --set release.archive.sha256=<sha256> \
     --set ingress.enabled=true --set ingress.className=<class> --set ingress.host=<host>
   ```

3. `helm test tandemapp -n tandemapp` checks `/health` and the page through the Service.

The admin password is a Secret the chart makes with a random token (kept across upgrades), or one
you name with `auth.existingSecret`. Values files never hold a token.

With `auth.mode: accounts` (user accounts, sessions, API tokens) no auth secret goes into the pod's
environment. The same Secret is mounted into the controller as a file holding a one-time setup code
that makes the first owner; a release that had an admin token keeps it, and the token works once as
the setup code and then, for 30 days, as an API token for scripts. The pod never trusts its own
loopback, so `kubectl port-forward` needs a sign-in like any client. NOTES prints where the code is,
and the `kubectl exec` lines for a new code (`users setup-code`) and a lost password
(`users reset-password NAME`). The ingress routes the API paths; `ingress.api.*` blocks one only when
set to false. Who may call is the page's API exposure switch: a token from another device is refused
while its API is "this machine", a signed-in page always works. `auth.trustedProxy` names the ingress
controller, or its pods' CIDR range, when it ends TLS. docs/install-kubernetes.md has the whole of it.

## Values

| value | default | what |
|-|-|-|
| `image.repository`, `.tag`, `.digest`, `.pullPolicy` | `python` 3.12 slim, pinned | the stock image of every container (Python 3.11+, tar, the CA bundle) |
| `release.id` | required | the folder in the archive (the commit's first 12 characters) |
| `release.archive.url`, `.sha256`, `.waitSeconds` | empty, empty, 1800 | where the init container gets the archive; empty url: it waits for `incoming/tandemapp-<id>.tar.gz` on the volume; sha256 required with a url, checked when set |
| `release.requirements` | cryptography, psutil | what the venv gets from the archive's wheelhouse (no index) |
| `persistence.mountPath` | `/var/lib/tandemapp` | the volume in every container |
| `persistence.existingClaim`, `.storageClass`, `.size`, `.accessModes` | empty, the cluster's, 20Gi, RWO | the claim |
| `persistence.hostPath.path`, `.node` | empty | a single node's folder as a Retain volume |
| `persistence.keep` | true | the claim survives `helm uninstall` |
| `controller.port` | 8091 | the controller's port in the pod |
| `controller.env`, `.extraEnv`, `.extraEnvFrom` | `TANDEM_NODE_AGENT: "off"` | the controller's variables (docs/proxy.md); keys never go here |
| `controller.conductor` | empty | the conductor's documents: a path under the volume, an absolute path, or `off`. Empty: off on a new install, the running release's on an upgrade |
| `controller.resources`, `.probes` | | the controller's requests, limits and probes (`/health`) |
| `auth.mode` | `legacy` | `legacy`: the admin token in the controller's env; `accounts`: TANDEM_AUTH=accounts, the setup code as a file |
| `auth.existingSecret`, `.secretKey`, `.create` | empty, `admin-token`, true | the admin token's Secret; in accounts mode the setup code's (made with 20 characters) |
| `auth.bootstrap.existingSecret`, `.secretKey`, `.create` | empty | accounts mode: a Secret of its own for the setup code; empty: the one above. No Secret at all is allowed: the code is then in the log |
| `auth.trustedProxy` | empty | TANDEM_TRUSTED_PROXY: addresses and CIDR ranges (IPv4, IPv6) of the proxies whose forwarded headers count, e.g. `192.0.2.0/24` |
| `chat.mode` | empty | `off`, `own` (chatd in this pod), `external` (another host's). Empty: off on a new install, the running release's on an upgrade. The page's switch wins once used |
| `chat.dataDir`, `chat.env` | `data/chat`, {} | chatd's data folder under the volume, more lines of its env file |
| `sandbox.mode` | `off` | `off`, `podman-socket`, `kubernetes` (below) |
| `sandbox.podmanSocket.hostDir` | empty | the node's folder with `podman.sock` |
| `sandbox.kubernetes.image`, `.imagePullPolicy` | `tandem-sandbox:1`, Never | the sandbox image, imported on each node |
| `sandbox.kubernetes.podLabels`, `.runtimeClassName`, `.hostUsers`, `.runAsUser` | component label, empty, false, 1000 | the sandbox pods |
| `sandbox.kubernetes.memoryGiB`, `.cpus`, `.maxRunning`, `.egressPort` | 2 (empty), 4, 2 (empty), 3128 | per sandbox, and chatd's egress proxy port |
| `sandbox.kubernetes.pluginEgressPort` | 3129 | the plugin host's egress proxy port, for the plugins' sandbox pods (SRV-859) |
| `mcpBridge.enabled`, `.name`, `.port`, `.path`, `.command`, `.env`, `.resources` | off, `tools`, 8031, `/mcp` | a stdio MCP server as HTTP on the pod's loopback (below) |
| `trust.extraCA`, `.existingConfigMap`, `.configMapKey` | empty | more CA certificates for every container |
| `service.*` | ClusterIP, 80 | the Service |
| `ingress.enabled`, `.className`, `.host`, `.annotations`, `.tls`, `.blockedPaths` | off; streaming annotations; `/chat-engine` and `/plugin-backends` blocked | the Ingress |
| `ingress.api.openai`, `.anthropic` | empty | route (empty or true) or block (false) the OpenAI-compatible paths and the Messages API's `/v1/messages`; `/v1/models` is always routed. Blocking `openai` breaks the page's Playground through the ingress |
| `networkPolicy.enabled`, `.ingressNamespaces`, `.extraFrom` | off | who may reach the controller's port |
| `serviceAccount.*`, `podSecurityContext`, `securityContext` | uid 10001, read-only root, no capabilities | the pod's identity and lockdown |
| `podAnnotations`, `podLabels`, `nodeSelector`, `tolerations`, `affinity`, `terminationGracePeriodSeconds` | | scheduling |

An empty `chat.mode` or `controller.conductor` reads the running Deployment (`lookup`) on an
upgrade. `helm template` and client-side dry runs read no cluster and render the new-install
default, so a release whose renders are checked before an upgrade sets both explicitly.

## Volume

One ReadWriteOnce volume at `persistence.mountPath`: `releases/<id>` and `current`, `state/` (the
config database, the ledger, the fleet history, `secrets.key`, `chatd.env`), `data/chat` (chatd's
data), `conductor/`, `ca/bundle.pem`, `home/`, `incoming/` and `backups/`. The claim is kept on
uninstall (`persistence.keep`). On a single-node cluster `persistence.hostPath.path` pins it to a
host folder with a Retain volume.

## Chat sandboxes

`sandbox.mode`:

- `off`: no sandbox runtime; the chats' sandbox tools say so.
- `podman-socket`: a rootless Podman socket on the node (`ops/sandbox/install.sh`), mounted into
  the pod. chatd hands host paths to the runtime as bind mounts, so the volume must be a host
  folder at the same path (`persistence.hostPath.path` = `persistence.mountPath`), and the
  socket's ACL must admit `podSecurityContext.runAsUser`.
- `kubernetes`: chatd (`chat.mode` own, or off and switched on from the page) makes each chat's
  sandbox a pod in the release's namespace through the Kubernetes API (`chatd/k8s.py`,
  [docs/chat.md](../../docs/chat.md), [docs/install-kubernetes.md](../../docs/install-kubernetes.md)).

### sandbox.mode kubernetes

- **Access.** The release's ServiceAccount gets a Role with pods (get, list, watch, create,
  delete), pods/exec and pods/log in this namespace only, and its token is mounted in the
  controller's container only. The sandbox pods use their own account (`<release>-sandbox`) with no
  Role and no token.
- **The image.** `sandbox.kubernetes.image` (`tandem-sandbox:1`) is built inside the cluster and
  imported into each node's containerd, never pushed to or pulled from a registry
  (`imagePullPolicy: Never`):

  ```
  bash ops/sandbox/build-k8s.sh <namespace> <node> <user@node>
  ```

  A kaniko Job on that node builds `ops/sandbox/Containerfile` for the node's architecture and
  writes a tar to a host folder; the script then runs `ctr image import` on the node over SSH
  (`sudo -n`; MicroK8s: `microk8s ctr`, else `--ctr "ctr -n k8s.io"`). Run it on each node, amd64
  and arm64 alike. A node without the image fails the chat's call with that message.
- **The files: a subPath of the release's volume.** `/workspace` and `/home/agent` of a sandbox are
  `data/chat/workspaces/<chat>` and `data/chat/homes/<chat>` of the release's claim, mounted with
  `subPath`. chatd reads and writes those folders all the time (the file tools and pane, uploads,
  snapshots and undo, the purge), so a copy in and out (emptyDir) would add a copy to every one of
  them and lose the files with the pod, and a claim per chat would need a storage class that makes
  volumes on demand and still one node for chatd to read them. The limit: a ReadWriteOnce volume is
  one node's, so the sandbox pods run on the controller's node (node affinity). chatd and the
  sandbox are different uids (`podSecurityContext.runAsUser`, 10001, and
  `sandbox.kubernetes.runAsUser`, 1000); chatd gives both access with POSIX ACLs on the chat's two
  folders (the volume's file system must take ACLs: ext4 and XFS do).
- **Isolation.** `sandbox.kubernetes.hostUsers: false` (the default) gives each sandbox pod a user
  namespace (Kubernetes 1.33 and later, containerd 2 and a recent kernel), and with it a limit of
  512 processes per pod. Kubernetes has no per-pod process limit of its own: with `hostUsers: true`
  the node's `podPidsLimit` is the only one. `sandbox.kubernetes.runtimeClassName` (gVisor, Kata)
  where the cluster has one; chatd's start check refuses a class that does not exist.
- **Network.** Two NetworkPolicies: sandbox pods reach only the controller's
  `sandbox.kubernetes.egressPort` (3128, chatd's egress proxy) and `.pluginEgressPort` (3129,
  the plugin host's; each proxy refuses a pod that is not its own) and nothing reaches them; the
  controller takes those ports from this release's sandbox pods only. The cluster's network plugin
  must enforce NetworkPolicy: chatd checks that at start with a probe pod and offers no sandbox
  when it does not.
- **Limits.** `sandbox.kubernetes.memoryGiB` (empty: 2 GiB), `cpus` (4), `maxRunning` (empty:
  2), counted from the cluster's pods.

## MCP bridge

`mcpBridge` runs a stdio MCP server (a program on the volume, with its own login folder) as
streamable HTTP on the pod's loopback (`files/mcp-stdio-http.py`), for chatd's MCP servers.

The bridge must never take the controller down with it. Kubernetes counts a pod Ready only while
every one of its containers runs (a native sidecar too), and the Service sends traffic only to Ready
pods: a sidecar that restarts, and waits in CrashLoopBackOff for up to 5 minutes between tries,
makes the page and the API answer 503 for that time, whatever the controller's own state. No probe
setting avoids that, so the bridge's container never exits: a shell loop starts the bridge again
5 s after it ends (a port in use, a crash), and TERM ends the loop at once. It has no liveness or
readiness probe; only the controller's readiness probe decides whether the pod gets traffic. The
bridge starts its MCP program on the first request and again after it exits, so a slow or failed
login there shows as an MCP error in the chats, not as a dead pod. What a probe would have healed
and this does not: a bridge that hangs while its port stays open. Check it with an MCP
`initialize` and `tools/list` from the controller's container after an install or upgrade.

## Network

The Ingress routes everything to the controller except `ingress.blockedPaths` (`/chat-engine`,
chatd's engine gateway, and `/plugin-backends`, a plugin's backend for the controller's own relay),
which go to a Service with no endpoints, as do the API paths `ingress.api` blocks.
`networkPolicy.enabled` limits
the controller's port to the ingress controller's namespace and this release's pods.
