#!/bin/bash
# Build tandem-sandbox:1 inside a Kubernetes cluster and put it into one node's containerd, for
# the chart's sandbox.mode kubernetes (chatd/k8s.py). Nothing is built on the machine that runs
# this script and nothing is pushed to a registry: a kaniko Job on the node builds this folder's
# Containerfile for the node's own architecture and writes the image as a tar to a host folder;
# the tar is then imported into the node's containerd over SSH (`ctr image import`, sudo -n).
# Run it once per node (amd64 and arm64 nodes each build their own). The pods use
# imagePullPolicy Never (or IfNotPresent): the image never comes from a registry.
#
#   bash ops/sandbox/build-k8s.sh NAMESPACE NODE SSH_TARGET [--ctr "microk8s ctr"]
#
#   NAMESPACE   where the Job runs (a namespace you own; the Job and its ConfigMap are deleted
#               at the end)
#   NODE        the node's name (kubectl get nodes): the Job runs there
#   SSH_TARGET  user@host of that node, with sudo -n for the import
#   --ctr CMD   the node's containerd client in the k8s.io namespace (default: microk8s ctr;
#               plain containerd: "ctr -n k8s.io")
#
# Needs kubectl with rights to create a ConfigMap and a Job with a hostPath volume in NAMESPACE.
# The Job pulls the kaniko executor and the Containerfile's base image, and the build fetches
# Debian packages and uv: the node needs the internet for the build (the sandboxes do not).
set -euo pipefail
NS="${1:?namespace}"; NODE="${2:?node}"; TARGET="${3:?ssh target}"; shift 3
CTR="microk8s ctr"
while [ $# -gt 0 ]; do
  case "$1" in
    --ctr) CTR="$2"; shift 2 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
HERE="$(cd "$(dirname "$0")" && pwd)"
TAG="tandem-sandbox:1"
KANIKO="${KANIKO_IMAGE:-gcr.io/kaniko-project/executor:v1.23.2}"
HOSTDIR=/var/tmp/tandem-sandbox-build
TAR="$HOSTDIR/tandem-sandbox-1.tar"
NAME="tandem-sandbox-build"
K=(kubectl -n "$NS")

"${K[@]}" delete job "$NAME" --ignore-not-found --wait=true >/dev/null
"${K[@]}" create configmap "$NAME" --from-file=Containerfile="$HERE/Containerfile" \
  --from-file=relay.py="$HERE/relay.py" --dry-run=client -o yaml | "${K[@]}" apply -f - >/dev/null
"${K[@]}" apply -f - >/dev/null <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: $NAME
  labels: {app.kubernetes.io/name: tandem-sandbox-build}
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 3600
  template:
    metadata:
      labels: {app.kubernetes.io/name: tandem-sandbox-build}
    spec:
      restartPolicy: Never
      nodeName: $NODE
      automountServiceAccountToken: false
      initContainers:
        # the build context: the ConfigMap's files copied to a writable folder (the executor's
        # -debug variant has a shell)
        - name: context
          image: ${KANIKO%-debug}-debug
          command: ["/busybox/sh", "-c", "cp /cm/Containerfile /cm/relay.py /context/"]
          volumeMounts:
            - {name: cm, mountPath: /cm}
            - {name: context, mountPath: /context}
      containers:
        - name: kaniko
          image: $KANIKO
          args:
            - --context=dir:///context
            - --dockerfile=/context/Containerfile
            - --destination=$TAG
            - --no-push
            - --tar-path=/out/tandem-sandbox-1.tar
            - --snapshot-mode=redo
            - --use-new-run
          resources:
            requests: {cpu: "1", memory: 2Gi}
            limits: {cpu: "4", memory: 6Gi}
          volumeMounts:
            - {name: context, mountPath: /context}
            - {name: out, mountPath: /out}
      volumes:
        - name: cm
          configMap: {name: $NAME}
        - name: context
          emptyDir: {}
        - name: out
          hostPath: {path: $HOSTDIR, type: DirectoryOrCreate}
EOF
echo "build: job $NS/$NAME on $NODE (kaniko $KANIKO)"
if ! "${K[@]}" wait --for=condition=complete "job/$NAME" --timeout=3600s >/dev/null 2>&1; then
  "${K[@]}" logs "job/$NAME" -c kaniko --tail=40 >&2 || true
  echo "build: the job did not complete" >&2
  exit 1
fi
"${K[@]}" logs "job/$NAME" -c kaniko --tail=5 || true
echo "import: $TAR into the node's containerd ($CTR)"
ssh -o BatchMode=yes -o ConnectTimeout=15 "$TARGET" \
  "sudo -n $CTR image import $TAR >/dev/null && sudo -n $CTR image ls -q | grep -F 'tandem-sandbox:1' && sudo -n rm -f $TAR"
"${K[@]}" delete job "$NAME" --wait=false >/dev/null
"${K[@]}" delete configmap "$NAME" >/dev/null
echo "done: $TAG on node $NODE (pods: image $TAG, imagePullPolicy Never)"
