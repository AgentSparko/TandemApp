# TandemApp on Kubernetes with Helm

The chart `charts/tandemapp` runs TandemApp's controller in one pod. No image is built: every
container runs a stock Python image pinned by digest, and an init container unpacks a release
archive into the pod's volume and makes a virtualenv there from the archive's own wheels. The values
are listed in the [chart's README](../charts/tandemapp/README.md); the overview, the first page and
backups are in [install.md](install.md).

The procedure below is the one tested end to end (OPS-292) on a single-node cluster: Kubernetes
1.35 (MicroK8s, amd64) with Calico, the hostpath storage class on ext4, the nginx ingress
controller behind a reverse proxy that ends TLS, Chat with sandbox pods, and an MCP bridge sidecar.

## 1. What the cluster needs

| what | why |
|-|-|
| Kubernetes 1.27 or newer; 1.33 or newer with containerd 2 for sandbox pods | `sandbox.kubernetes.hostUsers: false` (a user namespace per sandbox pod) needs 1.33 |
| a storage class with ReadWriteOnce volumes, or a host folder (`persistence.hostPath.path`) | one volume holds the state, the releases and chat's data |
| a file system on that volume that takes POSIX ACLs (ext4, XFS) | sandbox pods only: chatd and the sandbox are different users on the same folders |
| an ingress controller (`ingress.className`) | the page and the API; or reach the Service another way |
| a network plugin that enforces NetworkPolicy (Calico, Cilium) | sandbox pods only: without it chatd offers no sandbox |
| `kubectl` and Helm 3.12 or newer where you run the steps | |

The nodes pull the stock Python image (`image.*`). Nothing else is pulled at run time: the release
comes on the volume, and the sandbox image is built in the cluster.

## 2. The release archive

The pod runs `tandemapp-<id>.tar.gz`: one folder `<id>/` (the commit's first 12 characters) with the
release's files (`tools/app_release.py`: no tests, no page sources), the built page in
`dashboard/dist`, a `wheelhouse/` for the image's Python and the nodes' architecture, and a
`RELEASE` file (commit, page hash, date). The init container refuses an archive without
`<id>/proxy`.

Build it on a Linux build host, from a fresh copy of the commit, so the archive depends on the
commit and the pinned inputs only:

    COMMIT=$(git rev-parse <tag or branch>)
    mkdir build-1 && git archive "$COMMIT" | tar -x -C build-1
    cd build-1
    # Node 24.11.1 (check its tarball against nodejs.org's SHASUMS256.txt) on PATH
    python3 tools/dashboard_bundle.py build --commit "$COMMIT" --prune-modules
    python3 tools/dashboard_bundle.py verify
    cd ..
    WHEELS=$PWD/wheels bash <checkout>/charts/tandemapp/scripts/make-archive.sh \
        "$COMMIT" build-1/dashboard/dist out-1

`WHEELS` is a folder that holds exactly the wheels for the image's Python (3.12) and the nodes'
platform: `cryptography`, `psutil` and their dependencies (`cffi`, `pycparser`,
`typing_extensions`). Fetch them once with `pip download --only-binary=:all: --python-version 3.12
--platform manylinux_2_28_x86_64 -d wheels cryptography psutil` (arm64 nodes:
`manylinux_2_28_aarch64`) and keep that folder; without `WHEELS` the script fetches the newest ones.
`make-archive.sh` prints `release.id` and the archive's sha256.

Every entry has the commit's time and owner 0, and the entries are sorted, so the same commit, page
and wheels give the same bytes. Build a second time from a second fresh copy (`build-2`, `out-2`)
and compare the two sha256: they must be equal. Copy trees between a Mac and the build host with
`COPYFILE_DISABLE=1 tar --no-xattrs`: a Mac's tar otherwise adds `._` files, which the packer
refuses.

## 3. Before `helm install`

    kubectl create namespace tandemapp

**The admin password.** Without one every route answers anyone who reaches the pod. Make the
Secret from a pipe, never from a values file or a shell history:

    python3 -c 'import secrets; print(secrets.token_urlsafe(32), end="")' | \
      kubectl -n tandemapp create secret generic tandemapp-admin --from-file=admin-token=/dev/stdin

Keep the token in a password manager: it signs in to the page and is the admin token of the API.
(Leaving `auth.existingSecret` empty makes the chart create a Secret with a random token, kept
across upgrades.)

**Sign-in with accounts (`auth.mode: accounts`).** User accounts with roles, sessions and API
tokens replace the admin token. The chart then puts no auth secret into the pod's environment:

- The Secret is mounted into the controller as a file (`TANDEM_SETUP_CODE_FILE`) that holds a
  one-time setup code. Leave `auth.existingSecret` empty and the chart makes it with a 20-character
  code; or name your own with `auth.bootstrap.existingSecret` (and `.secretKey`). Read it once:

      kubectl -n tandemapp get secret tandemapp-admin -o jsonpath='{.data.admin-token}' | base64 -d; echo

  Open the page, enter the code with the first owner's name and password. The code is single use
  and spent after 5 wrong tries. With no Secret at all (`auth.create: false`) each start logs a
  fresh code while no owner exists.
- On a new install, do not make the admin token's Secret above. A Secret named by
  `auth.existingSecret` (or the one the chart made in legacy mode) is the old admin token as well
  (`TANDEM_ADMIN_TOKEN_FILE`): a value of 32 characters or more there becomes an API token for
  scripts for 30 days once the owner exists. Let the chart make the code, or give your own in a
  Secret named by `auth.bootstrap.existingSecret`, which is only ever a setup code.
- Nobody signs in before the owner exists: every route but `/health` and the page shell answers
  401. That includes `kubectl port-forward`, which reaches the pod on its own loopback: on
  Kubernetes (`TANDEM_DEPLOY=kubernetes`, which the chart sets) loopback is never trusted.
- A new code, and a lost password, through `kubectl exec` (`helm install` prints both lines):

      kubectl -n tandemapp exec deploy/tandemapp -c controller -- env PYTHONPATH=/var/lib/tandemapp/current \
        /var/lib/tandemapp/current/venv/bin/python -m proxy users setup-code --config-db /var/lib/tandemapp/state/config.sqlite3
      kubectl -n tandemapp exec deploy/tandemapp -c controller -- env PYTHONPATH=/var/lib/tandemapp/current \
        /var/lib/tandemapp/current/venv/bin/python -m proxy users reset-password NAME --config-db /var/lib/tandemapp/state/config.sqlite3

  `reset-password` prints a one-time code, valid 24 hours: sign in with it as the password, then
  set one.
- Once the owner exists the health page asks to delete the Secret (`bootstrap_secret_left`). The
  mount is optional, so the pod starts without it; set `auth.create: false` (or
  `auth.bootstrap.create: false`) so an upgrade does not make it again.
- A chatd on another host (`chat.mode: external`) needs a key of its own: `tokens chatd-key FILE`
  through the same `kubectl exec`, then give that chatd the key as `QSE_CHAT_ENGINE_KEY`.

**Extra CAs.** When the controller calls services with a private CA (engines over https, MCP
servers), give it the CA: `--set-file trust.extraCA=ca.pem`.

## 4. Install

A minimal values file, `values.yaml`:

    release:
      id: "0123456789ab"                 # make-archive.sh's release.id
      archive:
        url: ""                          # or an https URL the pod can fetch
        sha256: "<the archive's sha256>"
    persistence:
      storageClass: <your class>
      size: 20Gi
    auth:
      existingSecret: tandemapp-admin
      create: false
    ingress:
      enabled: true
      className: nginx
      host: tandemapp.example.com
    networkPolicy:
      enabled: true
      ingressNamespaces: [ingress-nginx] # the ingress controller's namespace

    helm install tandemapp charts/tandemapp -n tandemapp -f values.yaml

This is a bare TandemApp: the gateway only, Chat and the conductor off, no MCP bridge (OPS-288).
Switch parts on from the page (Settings → App → What runs here) or with the values in section 6.

**Putting the archive on the volume.** Either:

- `release.archive.url` and `.sha256`: the init container fetches it and checks the sha256; or
- `url` empty: the pod waits in its init container `release` (up to `waitSeconds`, then it fails and
  retries), and `helm install` prints the line that copies it in:

      POD=$(kubectl -n tandemapp get pod -l app.kubernetes.io/instance=tandemapp,app.kubernetes.io/component=controller -o jsonpath='{.items[0].metadata.name}')
      kubectl -n tandemapp cp tandemapp-<id>.tar.gz $POD:/var/lib/tandemapp/incoming/tandemapp-<id>.tar.gz -c release

  The init container waits until the file's size holds still, checks the sha256 when it is set,
  unpacks the release to `releases/<id>`, makes its venv offline from the wheelhouse and points
  `current` at it. A release that is unpacked already is used as it is.

Then:

    kubectl -n tandemapp rollout status deploy/tandemapp
    helm test tandemapp -n tandemapp       # /health and the page through the Service

and open `https://tandemapp.example.com/`: the setup wizard (pairing an engine host:
[install.md](install.md#the-first-page)).

## 5. Ingress, TLS and a reverse proxy in front

- The Ingress sends everything to the controller except `ingress.blockedPaths` (`/chat-engine`,
  chatd's way to the gateway inside the pod), which go to a Service with no endpoints (503). Check
  it after the install: `/chat-engine/health` must answer 503 from outside.
- A reverse proxy in front that forwards only some paths must still send the controller every
  prefix it owns: the list is in [install.md](install.md#behind-a-reverse-proxy).
- TLS at the ingress: `ingress.tls` with a Secret for the host. TLS at a reverse proxy in front:
  leave `ingress.tls` empty, have the proxy forward plain HTTP to the ingress with the original
  `Host`, `X-Forwarded-Proto` and `X-Forwarded-For`, and let the ingress controller trust them
  (ingress-nginx: `use-forwarded-headers: "true"`). The session cookie is marked Secure from
  `X-Forwarded-Proto: https`.
- The chart's annotations turn buffering off and allow an hour per request: answers stream, and a
  long one takes minutes. A proxy in front needs the same.
- Behind an ingress every client reaches the controller from the ingress pod's address, so the
  sign-in limit (5 wrong passwords a minute) is shared by all clients until `auth.trustedProxy`
  names that address (below).
- **The API paths.** The ingress routes the OpenAI-compatible routes (`/v1/chat/completions`,
  `/v1/completions`, `/v1/embeddings`, `/v1/responses`, `/v1/cache`) and the Messages API
  (`/v1/messages`) by default, in both auth modes, on a new install and an upgrade alike.
  `/v1/models` is always routed: the page reads it. `ingress.api.openai: false` or
  `ingress.api.anthropic: false` sends that protocol's paths to the Service with no endpoints
  (503). The page's Playground, its model windows and the developer view post to
  `/v1/chat/completions`, so `ingress.api.openai: false` breaks them through the ingress.
  Who may call is the controller's rule, the page's API exposure switch (accounts mode): while a
  protocol is "this machine", an API token from another device gets 403 `api_not_exposed`;
  "network" lets a token with the protocol's scope in. A signed-in page session always reaches
  the routes its role allows, whatever the switch says. On Kubernetes no caller counts as this
  machine, so a client with a token needs its protocol at "network". The chart tells the
  controller what it routes (`TANDEM_INGRESS_API`), and the health page says when a switch is on
  for a path the ingress blocks (`exposure_unreachable`).
- **The trusted proxy.** With TLS ended at the ingress, set `auth.trustedProxy` to the ingress
  controller (`TANDEM_TRUSTED_PROXY`: a comma list of addresses and CIDR ranges, IPv4 and IPv6).
  Only then do its `X-Forwarded-Proto` and `X-Real-IP` count; without it every API token through
  the ingress is taken as sent over plain HTTP (`exposure_plain_http`) and every client shares one
  sign-in limit. An ingress controller's pod gets a new address when it restarts, so name the
  cluster's pod range rather than one address, for example with `--set`
  `auth.trustedProxy=192.0.2.0/24\,2001:db8:42::/64` (write your cluster's own pod CIDR). An
  entry that is neither an address nor a range is ignored, and the health page names the variable
  (`trusted_proxy_invalid`).
- `networkPolicy.enabled` admits the controller's port from `ingressNamespaces` and this release's
  pods only (`extraFrom` adds more). The kubelet's probes still pass.

## 6. Values that matter

The full table is the [chart's README](../charts/tandemapp/README.md). The ones to decide:

| value | default | what |
|-|-|-|
| `release.id`, `release.archive.url`, `.sha256` | required, empty, empty | the release the pod runs |
| `persistence.storageClass`, `.size`, `.existingClaim`, `.hostPath.path` | the cluster's default, 20Gi | the volume; kept on uninstall (`persistence.keep`) |
| `auth.mode` | `legacy` | `legacy` (the admin token in the env) or `accounts` (section 3) |
| `auth.existingSecret`, `.secretKey` | a Secret the chart makes | the admin token; in accounts mode the setup code |
| `auth.bootstrap.existingSecret`, `.secretKey`, `.create` | empty: the Secret above | accounts mode: the setup code's own Secret |
| `auth.trustedProxy` | empty | the ingress controller's address or its pods' CIDR range when it ends TLS (section 5) |
| `ingress.api.openai`, `.anthropic` | empty: routed | `false` blocks that API's paths (section 5) |
| `chat.mode` | empty: off on a new install, the running release's on an upgrade | `off`, `own` (chatd in this pod), `external` (another host's chatd) |
| `sandbox.mode` | `off` | `off`, `podman-socket`, `kubernetes` (section 7) |
| `controller.conductor` | empty: off on a new install, the running release's on an upgrade | a folder under the volume, an absolute path, or `off` |
| `controller.env` | `TANDEM_NODE_AGENT: "off"` | the controller's variables ([proxy.md](proxy.md)): backends declared here are locked on the page |
| `mcpBridge.*` | off | a stdio MCP server as HTTP on the pod's loopback (section 8) |
| `ingress.*`, `networkPolicy.*`, `trust.*` | off | section 5 |

The page's switches (Chat, the conductor) are stored in the config database on the volume. Once one
was used, it wins over `chat.mode` and `controller.conductor`. An empty `chat.mode` or
`controller.conductor` keeps what the running release has, so an upgrade to a chart with other
defaults flips nothing. `helm template` and client-side dry runs cannot read the running release:
they render the new-install default. Set both values explicitly in a release's values file when its
renders are checked before an upgrade.

## 7. Chat and its sandboxes

`chat.mode`:

- `off` (a new install): no chatd; the Chat tab says Chat is off. Switched on from the page, chatd
  runs in this pod with the chart's `chatd.env` (data in `data/chat`).
- `own`: chatd runs in this pod from the start, a child of the controller. A pod restart ends the
  chat runs that are open (they come back `interrupted`; Continue resumes them).
- `external`: another host runs chatd; the Chat tab forwards to `QSE_CHAT_URL`
  (`controller.env`).

`sandbox.mode`:

| mode | what | needs |
|-|-|-|
| `off` | no sandbox runtime: chat runs without its sandbox tools and says so | nothing |
| `podman-socket` | a rootless Podman socket on the node, mounted into the pod | `ops/sandbox/install.sh` on the node; `persistence.hostPath.path` equal to `persistence.mountPath` (the runtime binds host paths); the socket's ACL open to uid 10001 |
| `kubernetes` | each chat's sandbox is a pod in this namespace (`chatd/k8s.py`) | `chat.mode` own or off; the items below |

For `kubernetes`:

1. **Access.** The chart makes a Role (pods: get, list, watch, create, delete; pods/exec; pods/log;
   this namespace only) and gives its token to the controller container only. The sandbox pods run
   as their own account, with no Role and no token.
2. **Network.** Two NetworkPolicies: a sandbox pod reaches only chatd's egress proxy port
   (`sandbox.kubernetes.egressPort`, 3128) on the controller's pod, and nothing reaches it. At each
   start chatd runs a probe pod that tries the controller's port and the API; when either answers,
   the cluster does not enforce NetworkPolicy and chatd offers no sandbox. Its log says
   `the cluster check passed`, or why not.
3. **Isolation.** `hostUsers: false` (the default) gives each sandbox pod a user namespace and a
   512-process limit per pod. Set `true` where the cluster cannot (the node's `podPidsLimit` is then
   the only process limit). `runtimeClassName` takes gVisor or Kata where the cluster has one.
4. **The image.** `tandem-sandbox:1` is built in the cluster and imported into each node's
   containerd; nothing is pushed or pulled (`imagePullPolicy: Never`). Per node, amd64 and arm64
   alike:

       bash ops/sandbox/build-k8s.sh tandemapp <node name> <user@node>                 # MicroK8s
       bash ops/sandbox/build-k8s.sh tandemapp <node name> <user@node> --ctr "ctr -n k8s.io"

   A kaniko Job on that node builds `ops/sandbox/Containerfile` for its architecture and writes a
   tar to a host folder; the script imports it over SSH (`sudo -n`). The node needs the internet for
   the build only. Check it: `sudo microk8s ctr images ls | grep tandem-sandbox`.
5. **Files.** A sandbox's `/workspace` and `/home/agent` are subPaths of the release's volume
   (`data/chat/workspaces/<chat>`, `data/chat/homes/<chat>`), so the sandbox pods run on the
   controller's node. chatd (uid 10001) and the sandbox (uid 1000) both get access through POSIX
   ACLs on the chat's two folders.
6. **Limits.** `memoryGiB` (2), `cpus` (4) and `maxRunning` (2) per release, counted from the
   cluster's pods.

The values for sandbox pods:

    chat:
      mode: own
    sandbox:
      mode: kubernetes
      kubernetes:
        image: tandem-sandbox:1
        imagePullPolicy: Never
        hostUsers: false
        memoryGiB: "2"
        maxRunning: "2"

After the upgrade, check one sandbox end to end: a chat that runs a shell command, a fetch from the
internet through the egress proxy, and no sandbox pod left after the chat is deleted for good.

**Moving chat's data in.** `tandem-app chat-move` brings an older chatd's data folder into
`data/chat` (each file checked by sha256). A tar copy carries no ACLs: the first sandbox start of
each moved chat sets them on its workspace and home once, which takes a while for a chat with tens
of thousands of files; later starts set none.

## 8. The MCP bridge sidecar

`mcpBridge` serves a stdio MCP server (a program you put on the volume, with its own login folder)
as streamable HTTP on the pod's loopback, for chatd's MCP servers
(`http://127.0.0.1:<port><path>`):

    mcpBridge:
      enabled: true
      name: tools
      port: 8031
      path: /mcp
      command: [/var/lib/tandemapp/mcp/bin/server, mcp]
      env:
        SERVER_HOME: /var/lib/tandemapp/mcp/home

The bridge must never take the controller down. Kubernetes counts a pod Ready only while all of its
containers run, and the Service sends traffic to Ready pods only: a sidecar in CrashLoopBackOff
makes the page and the API answer 503 for minutes, whatever the controller's state. So the bridge's
container never exits (a loop starts the bridge again 5 s after it ends) and has no probe. Check it
after an install or upgrade with an MCP `initialize` and `tools/list` from the controller container:

    kubectl -n tandemapp exec deploy/tandemapp -c controller -- python3 -c '
    import json, urllib.request as u
    def call(m, i, p=None, s=None):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if s: h["Mcp-Session-Id"] = s
        r = u.urlopen(u.Request("http://127.0.0.1:8031/mcp", json.dumps({"jsonrpc": "2.0", "id": i, "method": m, "params": p or {}}).encode(), h), timeout=60)
        return r.headers.get("Mcp-Session-Id"), r.read().decode()
    s, _ = call("initialize", 1, {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "check", "version": "1"}})
    print(call("tools/list", 2, s=s)[1][:300])'

## 9. Upgrades and rollback

1. Build the new archive (section 2).
2. Put it on the volume while the old release runs: copy it into `incoming/` through the running
   controller container and check it there:

       kubectl -n tandemapp cp tandemapp-<new id>.tar.gz <pod>:/var/lib/tandemapp/incoming/tandemapp-<new id>.tar.gz -c controller
       kubectl -n tandemapp exec <pod> -c controller -- sha256sum /var/lib/tandemapp/incoming/tandemapp-<new id>.tar.gz

   (Or set `release.archive.url`; or copy it into the waiting init container after the upgrade, as
   on install.)
3. Note the revision: `helm history tandemapp -n tandemapp`.
4. `helm upgrade tandemapp charts/tandemapp -n tandemapp -f values.yaml` with the new `release.id`
   and `sha256`. The Deployment is Recreate (one volume, one pod): the API is down from the old
   pod's stop until the new one is Ready (longer when the init container unpacks a new release).
5. Check: `rollout status`, `helm test`, the pod's containers all Ready with 0 restarts, and watch it
   for a few minutes (the same pod, restarts unchanged, the page answering).

**From the admin token to accounts.** Upgrade with `auth.mode: accounts` and leave `auth.bootstrap`
empty. The Secret keeps its name, key and value: the old admin token works once as the setup code,
and once an owner exists (made with it, or from the controller's admin password) it is an API token
for scripts that ends after 30 days, marked for review. It never signs in to the page. Keep the
Secret while you may roll back: `helm rollback` to the revision before brings back the template
that reads it into `TANDEM_ADMIN_TOKEN`, and the admin token signs in there as before. A network-
reachable release with no admin password answers 401 everywhere after this upgrade until the
setup code is used.

`helm rollback tandemapp <revision> -n tandemapp` goes back. The old release's folder is still on
the volume, so no archive is needed. Releases are never removed by the chart: remove old
`releases/<id>` folders by hand once you will not roll back to them.

## 10. Troubleshooting

| what you see | why, and what to do |
|-|-|
| the pod stays `Init:0/1` | the init container waits for the archive: `kubectl logs <pod> -c release` says the path; copy it in (section 4). `sha256 differs`: the file or the value is wrong |
| the page and the API answer 503, the controller's log is fine | a container of the pod is not running, so the pod is not Ready: `kubectl get pod` (READY, RESTARTS) and `kubectl describe pod`. Never give a sidecar a `tcpSocket` or `httpGet` probe on a port it binds on loopback only: the kubelet probes the pod's address, the probe fails forever, and the sidecar's restarts take the Service down |
| Chat says "Chat is off" | it is: switch it on in Settings → App, or set `chat.mode: own` (the page's switch wins once used) |
| chats run without sandbox tools | `sandbox.mode` is off, or chatd's start check found isolation missing: the controller's log and `<volume>/state/logs/chatd.log` say which (NetworkPolicy not enforced, a runtimeClass that does not exist, the image not on the node) |
| a moved chat's first sandbox start is slow | the one ACL pass over its files (section 7) |
| every client is throttled after a few wrong passwords | they share the ingress's address (section 5) |
| accounts mode: every route answers 401, also through `kubectl port-forward` | no owner yet: use the setup code (section 3); `users setup-code` through `kubectl exec` makes a new one |
| the health page says `exposure_plain_http` behind a TLS ingress | `auth.trustedProxy` does not cover the ingress controller's address (section 5) |
| an OpenAI client gets 503 through the ingress | `ingress.api.openai` is false: unset it |
| an OpenAI client with a token gets 403 `api_not_exposed` | the OpenAI API is "this machine" on the page's API exposure switch: set it to "network" |
| `/chat-engine` answers from outside | the Ingress is not the chart's, or a proxy in front routes around it: block the path there too |
