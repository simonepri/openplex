{{- /* Provides fail-closed selection and stable names for registry-owned routes. */ -}}

{{- define "routing-registry.normalize" -}}
{{- regexReplaceAll "[^a-z0-9-]+" (. | lower) "-" | trimAll "-" | trunc 48 | trimSuffix "-" -}}
{{- end -}}

{{- define "routing-registry.selected" -}}
{{- $root := index . 0 -}}
{{- $route := index . 1 -}}
{{- $matchPhase := or (eq $root.Values.phase "all") (eq $route.phase $root.Values.phase) -}}
{{- $selected := and $matchPhase (eq $route.exposure $root.Values.exposure) (has $root.Values.target.role $route.target.roles) -}}
{{- if and $selected (hasKey $route.target "names") -}}
{{- $selected = has $root.Values.target.name $route.target.names -}}
{{- end -}}
{{- if and $selected (hasKey $route.target "providers") -}}
{{- $selected = has $root.Values.target.provider $route.target.providers -}}
{{- end -}}
{{- if and $selected (or (eq $route.name "atlantis") (eq $route.name "atlantis-webhook")) (not $root.Values.atlantisEnabled) -}}
{{- $selected = false -}}
{{- end -}}
{{- if and $selected (hasKey $route "requiredCapabilities") -}}
{{- range $capability := $route.requiredCapabilities -}}
{{- if not (get $root.Values.capabilities $capability) -}}
{{- $selected = false -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if $selected -}}true{{- else -}}false{{- end -}}
{{- end -}}

{{- define "routing-registry.labels" -}}
{{- if eq .Values.exposure "public" }}
app.kubernetes.io/component: external-dns-source
{{- else }}
app.kubernetes.io/component: private-access-route
{{- end }}
app.kubernetes.io/managed-by: argocd
app.kubernetes.io/name: routing-registry
{{- end -}}

{{- define "routing-registry.validate" -}}
{{- $configured := or (gt (len .Values.routes) 0) (ne .Values.phase "") -}}
{{- if and (gt (len .Values.routes) 0) (not .Values.phase) -}}
{{- fail "phase is required when routes are configured" -}}
{{- end -}}
{{- if and (gt (len .Values.routes) 0) (not .Values.exposure) -}}
{{- fail "exposure is required when routes are configured" -}}
{{- end -}}
{{- if and .Values.phase (not .Values.target.name) -}}
{{- fail "target.name is required when phase is configured" -}}
{{- end -}}
{{- if and .Values.phase (not .Values.gateway.baseDomain) -}}
{{- fail "gateway.baseDomain is required when phase is configured" -}}
{{- end -}}
{{- if and .Values.phase .Values.gateway.aliasDomain (ne .Values.target.role "ctrl") -}}
{{- fail "gateway.aliasDomain is reserved for ctrl routes" -}}
{{- end -}}
{{- if and .Values.phase .Values.gateway.aliasDomain (and (ne .Values.gateway.baseDomain (printf "c.%s" .Values.gateway.aliasDomain)) (ne .Values.gateway.baseDomain (printf "%s.c.%s" .Values.target.name .Values.gateway.aliasDomain))) -}}
{{- fail "ctrl gateway.baseDomain must be c.<gateway.aliasDomain> or <target.name>.c.<gateway.aliasDomain>" -}}
{{- end -}}
{{- $root := . -}}
{{- range $route := .Values.routes -}}
{{- with $route.wildcardHostnameLabel -}}
{{- if not (regexMatch "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$" .) -}}
{{- fail (printf "route %s wildcardHostnameLabel must be a lowercase DNS label" $route.name) -}}
{{- end -}}
{{- if ne $root.Values.gateway.canonicalScheme "https" -}}
{{- fail (printf "route %s wildcardHostnameLabel requires an HTTPS gateway" $route.name) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
