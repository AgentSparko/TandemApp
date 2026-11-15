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
- `kubernetes` (planned): chatd makes each chat's sandbox a pod in the release's namespace. The
  chart already renders its Role (pods, exec, logs; this namespace only), the binding to the
  release's ServiceAccount, and a NetworkPolicy that lets a sandbox pod reach only chatd's egress
  proxy. chatd of this release does not use them yet.

## MCP bridge

`mcpBridge` runs a stdio MCP server (a program on the volume, with its own login folder) as
streamable HTTP on the pod's loopback (`files/mcp-stdio-http.py`), for chatd's MCP servers.

## Network

The Ingress routes everything to the controller except `ingress.blockedPaths` (`/chat-engine`,
chatd's engine gateway), which go to a Service with no endpoints. `networkPolicy.enabled` limits
the controller's port to the ingress controller's namespace and this release's pods.
