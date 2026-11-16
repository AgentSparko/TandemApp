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

{{/* sign-in: accounts, the only mode (SEC-28) */}}
{{- define "tandemapp.authMode" -}}
{{- toString (.Values.auth.mode | default "accounts") -}}
{{- end -}}

{{/* "true" when the chart makes the Secret <fullname>-admin: auth.bootstrap.create (empty:
auth.create) while no Secret is named */}}
{{- define "tandemapp.secretMade" -}}
{{- $a := .Values.auth -}}
{{- $b := $a.bootstrap | default dict -}}
{{- $create := $a.create -}}
{{- if not (kindIs "invalid" $b.create) -}}{{- $create = $b.create -}}{{- end -}}
{{- if and $create (not $a.existingSecret) (not $b.existingSecret) -}}true{{- end -}}
{{- end -}}

{{/* the Secret whose file holds the setup code, empty when there is none */}}
{{- define "tandemapp.setupSecret" -}}
{{- $b := .Values.auth.bootstrap | default dict -}}
{{- if $b.existingSecret -}}{{- $b.existingSecret -}}
{{- else if .Values.auth.existingSecret -}}{{- .Values.auth.existingSecret -}}
{{- else if include "tandemapp.secretMade" . -}}{{- include "tandemapp.secretName" . -}}
{{- end -}}
{{- end -}}

{{/* the key of that Secret: auth.bootstrap.secretKey belongs to auth.bootstrap.existingSecret */}}
{{- define "tandemapp.setupKey" -}}
{{- $b := .Values.auth.bootstrap | default dict -}}
{{- if $b.existingSecret -}}{{- default .Values.auth.secretKey $b.secretKey -}}
{{- else -}}{{- .Values.auth.secretKey -}}
{{- end -}}
{{- end -}}

{{/* the ingress paths of each API protocol (proxy/service.py PROXY_PATHS and the cache routes;
proxy/tokens.py MESSAGES). /v1/models is never among them: the page reads it */}}
{{- define "tandemapp.apiPaths" -}}
{{- toJson (dict "openai" (list "/v1/chat/completions" "/v1/completions" "/v1/embeddings" "/v1/responses" "/v1/cache") "anthropic" (list "/v1/messages")) -}}
{{- end -}}

{{/* {"openai": bool, "anthropic": bool}: does the ingress route the protocol's paths.
ingress.api.<protocol> false blocks them; true or empty routes them, in both auth modes, on a new
install and an upgrade alike. Who may call is the controller's rule (the API exposure switch: a
token from another device needs the protocol at "network"; a signed-in page always may) */}}
{{- define "tandemapp.apiRouted" -}}
{{- $out := dict -}}
{{- $api := .Values.ingress.api | default dict -}}
{{- range $p := list "openai" "anthropic" -}}
{{- $raw := get $api $p -}}
{{- $_ := set $out $p (ne (toString $raw) "false") -}}
{{- end -}}
{{- toJson $out -}}
{{- end -}}

{{/* TANDEM_INGRESS_API: the protocols the ingress routes, a comma list; "none" when it routes
neither (an empty value would read as "not known") */}}
{{- define "tandemapp.ingressApi" -}}
{{- $r := include "tandemapp.apiRouted" . | fromJson -}}
{{- $on := list -}}
{{- range $p := list "openai" "anthropic" -}}{{- if get $r $p -}}{{- $on = append $on $p -}}{{- end -}}{{- end -}}
{{- if $on -}}{{- join "," $on -}}{{- else -}}none{{- end -}}
{{- end -}}

{{/* ingress.blockedPaths plus the paths of each protocol the ingress does not route, as JSON */}}
{{- define "tandemapp.blockedPaths" -}}
{{- $r := include "tandemapp.apiRouted" . | fromJson -}}
{{- $paths := include "tandemapp.apiPaths" . | fromJson -}}
{{- $out := list -}}
{{- range .Values.ingress.blockedPaths -}}{{- $out = append $out . -}}{{- end -}}
{{- range $p := list "openai" "anthropic" -}}
{{- if not (get $r $p) -}}{{- range get $paths $p -}}{{- $out = append $out . -}}{{- end -}}{{- end -}}
{{- end -}}
{{- toJson (uniq $out) -}}
{{- end -}}

{{/* TANDEM_CHAT and TANDEM_CONDUCTOR of the controller this
release runs now (an upgrade), as JSON, with "running": true; {} on a new install and wherever the
cluster is not asked (helm template, lint) */}}
{{- define "tandemapp.runningEnv" -}}
{{- $out := dict -}}
{{- $d := lookup "apps/v1" "Deployment" .Release.Namespace (include "tandemapp.fullname" .) -}}
{{- if $d -}}
{{- $_ := set $out "running" true -}}
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
{{- $mode := include "tandemapp.authMode" . -}}
{{- if eq $mode "legacy" -}}
{{- fail "auth.mode legacy (the admin token in the environment) is gone: remove auth.mode, or set it to accounts. The Secret is kept and its admin token works once as the setup code" -}}
{{- end -}}
{{- if ne $mode "accounts" -}}
{{- fail "auth.mode is accounts" -}}
{{- end -}}
{{- if hasKey .Values.controller.env "TANDEM_AUTH" -}}
{{- fail "controller.env: TANDEM_AUTH is no longer read; remove it" -}}
{{- end -}}
{{- range .Values.controller.extraEnv -}}
{{- if eq (toString .name) "TANDEM_AUTH" -}}
{{- fail "controller.extraEnv: TANDEM_AUTH is no longer read; remove it" -}}
{{- end -}}
{{- end -}}
{{- range $p := list "openai" "anthropic" -}}
{{- $raw := get ($.Values.ingress.api | default dict) $p -}}
{{- if not (or (kindIs "invalid" $raw) (has (toString $raw) (list "" "true" "false"))) -}}
{{- fail (printf "ingress.api.%s is true, false or empty" $p) -}}
{{- end -}}
{{- end -}}
{{- end -}}
