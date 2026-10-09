{{- /* Provides fail-closed naming and value validation for one managed team lane. */ -}}

{{- define "team-namespace.namespace" -}}
{{- if eq .Values.mode "cell" -}}
{{- printf "team-%s-%s" (required "team.slug is required" .Values.team.slug) (required "team.namespaceSuffix is required in cell mode" .Values.team.namespaceSuffix | replace "_" "-") -}}
{{- else -}}
{{- printf "team-%s" (required "team.slug is required" .Values.team.slug) -}}
{{- end -}}
{{- end -}}

{{- define "team-namespace.labels" -}}
app.kubernetes.io/managed-by: argocd
{{/* LINT.IfChange(team-lane-identity-label) */}}
app.kubernetes.io/part-of: team-lane
{{/* LINT.ThenChange(//src/infra/argocd/components/kyverno/kustomize/team-pod-security-policy.yaml:team-lane-identity-label) */}}
{{- end -}}

{{- define "team-namespace.nonCompileCachePodSelector" -}}
podSelector:
  matchExpressions:
    - key: app.kubernetes.io/name
      operator: NotIn
      values: [torch-compile-cache]
{{- end -}}

{{- define "team-namespace.storageAuthAdapter" -}}
{{- if and (hasKey . "secretStore") .secretStore (hasKey .secretStore "auth") .secretStore.auth -}}
{{- $auth := .secretStore.auth -}}
{{- $keys := keys $auth | sortAlpha -}}
{{- if ne (len $keys) 1 -}}
{{- fail "storage projection auth must select exactly one adapter" -}}
{{- end -}}
{{- $adapter := first $keys -}}
{{- if not (has $adapter (list "aws" "gcp" "kubernetes")) -}}
{{- fail (printf "storage projection auth adapter %q is unsupported" $adapter) -}}
{{- end -}}
{{- $adapter -}}
{{- else if eq .provider "aws" -}}
aws
{{- else -}}
{{- fail "storage projection requires secretStore.auth" -}}
{{- end -}}
{{- end -}}

{{- define "team-namespace.storageKubernetesAuth" -}}
{{- $auth := .secretStore.auth -}}
{{- required "Kubernetes storage auth requires kubernetes coordinates" (index $auth "kubernetes") | toJson -}}
{{- end -}}

{{- define "team-namespace.validate" -}}
{{- $slug := required "team.slug is required" .Values.team.slug -}}
{{- $submitters := .Values.team.submitters | default "members" -}}
{{- if not (has $submitters (list "all" "members")) -}}
{{- fail (printf "team.submitters must be 'all' or 'members', got %q" $submitters) -}}
{{- end -}}
{{- $expectedGroup := printf "cluster:group:team:%s" $slug -}}
{{- if ne .Values.team.group $expectedGroup -}}
{{- fail (printf "team.group must be the derived group %q" $expectedGroup) -}}
{{- end -}}
{{- $classes := dict -}}
{{- range $class := .Values.team.classes -}}
{{- if hasKey $classes $class -}}
{{- fail (printf "team.classes contains duplicate class %q" $class) -}}
{{- end -}}
{{- $_ := set $classes $class true -}}
{{- end -}}
{{- $resources := dict -}}
{{- range $resource := .Values.resources -}}
{{- $key := printf "%s/%s" $resource.apiGroup $resource.resource -}}
{{- if hasKey $resources $key -}}
{{- fail (printf "resources contains duplicate RBAC resource %q" $key) -}}
{{- end -}}
{{- $_ := set $resources $key true -}}
{{- end -}}
{{- if .Values.storageProjection.enabled -}}
{{- $handoff := .Values.storageProjection.handoff -}}
{{- if eq $handoff.provider "aws" -}}
{{- if and (hasKey $handoff "secretStore") $handoff.secretStore (hasKey $handoff.secretStore "auth") $handoff.secretStore.auth -}}
{{- $authAdapter := include "team-namespace.storageAuthAdapter" $handoff -}}
{{- if ne $authAdapter "aws" -}}
{{- fail (printf "storage projection provider %q requires matching auth, found %q" $handoff.provider $authAdapter) -}}
{{- end -}}
{{- end -}}
{{- else -}}
{{- $authAdapter := include "team-namespace.storageAuthAdapter" $handoff -}}
{{- /* floci-divergence: Floci emulates team storage handoff credentials via AWS auth. */ -}}
{{- if and (ne .Values.mode "storage-rbac") (eq $handoff.provider "floci") -}}
{{- if ne $authAdapter "aws" -}}
{{- fail (printf "storage projection provider %q requires AWS auth" $handoff.provider) -}}
{{- end -}}
{{- else if and (ne .Values.mode "storage-rbac") (ne $authAdapter $handoff.provider) -}}
{{- fail (printf "storage projection provider %q requires matching auth, found %q" $handoff.provider $authAdapter) -}}
{{- end -}}
{{- if and (eq .Values.mode "storage-rbac") (ne $authAdapter "kubernetes") -}}
{{- fail "storage-rbac mode requires Kubernetes storage auth" -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
