{{- define "tandemapp.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "tandemapp.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "tandemapp.selectorLabels" -}}
app.kubernetes.io/name: {{ include "tandemapp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "tandemapp.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{ include "tandemapp.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/* the labels of this release's sandbox pods (chatd/k8s.py puts them on each pod) */}}
{{- define "tandemapp.sandboxLabels" -}}
{{- toYaml (merge (dict "app.kubernetes.io/instance" .Release.Name) .Values.sandbox.kubernetes.podLabels) -}}
{{- end -}}

{{- define "tandemapp.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "tandemapp.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "tandemapp.image" -}}
{{- if .Values.image.digest -}}
{{- printf "%s:%s@%s" .Values.image.repository .Values.image.tag .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository .Values.image.tag -}}
{{- end -}}
{{- end -}}

{{- define "tandemapp.claimName" -}}
{{- default (printf "%s-data" (include "tandemapp.fullname" .)) .Values.persistence.existingClaim -}}
{{- end -}}

{{- define "tandemapp.secretName" -}}
{{- default (printf "%s-admin" (include "tandemapp.fullname" .)) .Values.auth.existingSecret -}}
{{- end -}}

{{/* TANDEM_CHAT and TANDEM_CONDUCTOR of the controller this release runs now (an upgrade), as
JSON; {} on a new install and wherever the cluster is not asked (helm template, lint) */}}
{{- define "tandemapp.runningEnv" -}}
{{- $out := dict -}}
{{- $d := lookup "apps/v1" "Deployment" .Release.Namespace (include "tandemapp.fullname" .) -}}
{{- if $d -}}
{{- range $d.spec.template.spec.containers -}}
{{- if eq .name "controller" -}}
{{- range .env -}}
{{- if and (has .name (list "TANDEM_CHAT" "TANDEM_CONDUCTOR")) (hasKey . "value") -}}
{{- $_ := set $out .name .value -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- toJson $out -}}
{{- end -}}

{{/* who runs chatd (OPS-288): chat.mode when set; empty, the running controller's TANDEM_CHAT on
an upgrade, else off (a new install starts bare) */}}
{{- define "tandemapp.chatMode" -}}
{{- $m := toString (.Values.chat.mode | default "") -}}
{{- if not $m -}}
{{- $run := include "tandemapp.runningEnv" . | fromJson -}}
{{- $m = toString (get $run "TANDEM_CHAT" | default "off") -}}
{{- if not (has $m (list "off" "external" "own")) -}}
{{- $m = "off" -}}
{{- end -}}
{{- end -}}
{{- $m -}}
{{- end -}}

{{/* the conductor's folder: an absolute path, a path under the volume, or off. Empty
(OPS-288): the running controller's TANDEM_CONDUCTOR on an upgrade, else off */}}
{{- define "tandemapp.conductor" -}}
{{- $c := toString (.Values.controller.conductor | default "") -}}
{{- if not $c -}}
{{- $run := include "tandemapp.runningEnv" . | fromJson -}}
{{- $c = toString (get $run "TANDEM_CONDUCTOR" | default "off") -}}
{{- end -}}
{{- if or (eq $c "off") (hasPrefix "/" $c) -}}
{{- $c -}}
{{- else -}}
{{- printf "%s/%s" .Values.persistence.mountPath $c -}}
{{- end -}}
{{- end -}}

{{/* the checks a bad combination of values fails on */}}
{{- define "tandemapp.validate" -}}
{{- if not .Values.release.id -}}
{{- fail "release.id is required (the folder name in the release archive)" -}}
{{- end -}}
{{- if and .Values.release.archive.url (not .Values.release.archive.sha256) -}}
{{- fail "release.archive.sha256 is required with release.archive.url" -}}
{{- end -}}
{{- if not (has (toString (.Values.chat.mode | default "")) (list "" "off" "external" "own")) -}}
{{- fail "chat.mode is off, external or own (empty: off on a new install, kept on an upgrade)" -}}
{{- end -}}
{{- $chat := include "tandemapp.chatMode" . -}}
{{- if not (has (toString .Values.sandbox.mode) (list "off" "podman-socket" "kubernetes")) -}}
{{- fail "sandbox.mode is off, podman-socket or kubernetes" -}}
{{- end -}}
{{- if eq (toString .Values.sandbox.mode) "kubernetes" -}}
{{- if eq $chat "external" -}}
{{- fail "sandbox.mode kubernetes needs chat.mode own or off (chatd in this pod makes the sandbox pods)" -}}
{{- end -}}
{{- if not .Values.sandbox.kubernetes.image -}}
{{- fail "sandbox.mode kubernetes needs sandbox.kubernetes.image" -}}
{{- end -}}
{{- if hasKey .Values.sandbox.kubernetes.podLabels "app.kubernetes.io/instance" -}}
{{- fail "sandbox.kubernetes.podLabels: app.kubernetes.io/instance is set by the chart" -}}
{{- end -}}
{{- if eq (int .Values.sandbox.kubernetes.egressPort) (int .Values.controller.port) -}}
{{- fail "sandbox.kubernetes.egressPort must differ from controller.port" -}}
{{- end -}}
{{- end -}}
{{- if eq (toString .Values.sandbox.mode) "podman-socket" -}}
{{- if not .Values.sandbox.podmanSocket.hostDir -}}
{{- fail "sandbox.mode podman-socket needs sandbox.podmanSocket.hostDir" -}}
{{- end -}}
{{- if ne .Values.persistence.hostPath.path .Values.persistence.mountPath -}}
{{- fail "sandbox.mode podman-socket needs persistence.hostPath.path equal to persistence.mountPath (the runtime binds host paths)" -}}
{{- end -}}
{{- end -}}
{{- if and .Values.ingress.enabled (not .Values.ingress.host) -}}
{{- fail "ingress.enabled needs ingress.host" -}}
{{- end -}}
{{- if and .Values.mcpBridge.enabled (not .Values.mcpBridge.command) -}}
{{- fail "mcpBridge.enabled needs mcpBridge.command" -}}
{{- end -}}
{{- if and (not .Values.auth.existingSecret) (not .Values.auth.create) -}}
{{- fail "auth: name an existingSecret or set create (a controller without an admin password answers anyone)" -}}
{{- end -}}
{{- end -}}
