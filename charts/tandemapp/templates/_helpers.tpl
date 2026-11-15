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

{{/* the conductor's folder: an absolute path, a path under the volume, or off */}}
{{- define "tandemapp.conductor" -}}
{{- $c := toString .Values.controller.conductor -}}
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
{{- if not (has .Values.chat.mode (list "external" "own")) -}}
{{- fail "chat.mode is external or own" -}}
{{- end -}}
{{- if not (has (toString .Values.sandbox.mode) (list "off" "podman-socket" "kubernetes")) -}}
{{- fail "sandbox.mode is off, podman-socket or kubernetes" -}}
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
