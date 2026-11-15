# TandemApp Helm chart

TandemApp's controller (the gateway, the usage ledger, the fleet and the page) in one pod, with its
own chatd when `chat.mode` is `own`. Nothing is built into an image: every container runs a stock
Python image (`image.*`, pinned by digest), and an init container unpacks a release archive into
the volume and makes a virtualenv there from the archive's own wheels.

## Install

1. Make the release archive from a commit and the page built from it:

   ```
   python3 tools/dashboard_bundle.py build
   bash charts/tandemapp/scripts/make-archive.sh HEAD dashboard/dist dist-chart
   ```

   It prints `release.id` and the archive's sha256.

2. Install. Either the init container fetches the archive (`release.archive.url` and `.sha256`),
   or it waits for it on the volume and NOTES prints the `kubectl cp` line:

   ```
   helm install tandemapp charts/tandemapp -n tandemapp --create-namespace \
     --set release.id=<id> \
     --set ingress.enabled=true --set ingress.className=<class> --set ingress.host=<host>
   ```

3. `helm test tandemapp -n tandemapp` checks `/health` and the page through the Service.

The admin password is a Secret the chart makes with a random token (kept across upgrades), or one
you name with `auth.existingSecret`. Values files never hold a token.

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
- `kubernetes`: chatd (`chat.mode: own`) makes each chat's sandbox a pod in the release's
  namespace through the Kubernetes API (`chatd/k8s.py`, [docs/chat.md](../../docs/chat.md)).

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
  `sandbox.kubernetes.egressPort` (3128, chatd's egress proxy) and nothing reaches them; the
  controller takes that port from this release's sandbox pods only. The cluster's network plugin
  must enforce NetworkPolicy: chatd checks that at start with a probe pod and offers no sandbox
  when it does not.
- **Limits.** `sandbox.kubernetes.memoryGiB` (empty: 2 GiB), `cpus` (4), `maxRunning` (empty:
  2), counted from the cluster's pods.

## MCP bridge

`mcpBridge` runs a stdio MCP server (a program on the volume, with its own login folder) as
streamable HTTP on the pod's loopback (`files/mcp-stdio-http.py`), for chatd's MCP servers.

## Network

The Ingress routes everything to the controller except `ingress.blockedPaths` (`/chat-engine`,
chatd's engine gateway), which go to a Service with no endpoints. `networkPolicy.enabled` limits
the controller's port to the ingress controller's namespace and this release's pods.
