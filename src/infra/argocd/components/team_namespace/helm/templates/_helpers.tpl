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

{{- define "team-namespace.runtimeSecrets" -}}
{{- $adapter := .Values.runtimeSecrets.adapter -}}
{{- $auth := .Values.runtimeSecrets.auth -}}
{{- if ne $adapter "" -}}
{{- if not (has $adapter (list "aws" "gcp" "kubernetes")) -}}
{{- fail (printf "runtime secrets adapter %q is unsupported" $adapter) -}}
{{- end -}}
{{- $authKeys := keys $auth | sortAlpha -}}
{{- if or (ne (len $authKeys) 1) (ne (first $authKeys) $adapter) -}}
{{- fail (printf "runtime secrets adapter %q requires exactly its matching auth" $adapter) -}}
{{- end -}}
{{- else if .Values.devSecrets.names -}}
{{- fail "dev secrets require a runtime secrets adapter" -}}
{{- end -}}
{{- dict "adapter" $adapter "auth" $auth | toJson -}}
{{- end -}}

{{- define "team-namespace.buildbuddyAuth" -}}
{{- $auth := required "dev workspaces require BuildBuddy auth" .Values.devWorkspaces.buildbuddyAuth -}}
{{- $expectedRecord := printf "buildbuddy-auth-%s" .Values.devWorkspaces.cell -}}
{{- if ne $auth.recordName $expectedRecord -}}
{{- fail (printf "BuildBuddy auth record must be %q" $expectedRecord) -}}
{{- end -}}
{{- $expectedAdapter := .Values.devWorkspaces.origin.provider -}}
{{- /* floci-divergence: Floci clusters adapt BuildBuddy auth to in-cluster Kubernetes ServiceAccount or AWS. */ -}}
{{- if eq $expectedAdapter "floci" -}}
{{- if and (ne $auth.adapter "kubernetes") (ne $auth.adapter "aws") -}}
{{- fail (printf "BuildBuddy auth for provider %q requires adapter kubernetes or aws" .Values.devWorkspaces.origin.provider) -}}
{{- end -}}
{{- else if ne $auth.adapter $expectedAdapter -}}
{{- fail (printf "BuildBuddy auth for provider %q requires adapter %q" .Values.devWorkspaces.origin.provider $expectedAdapter) -}}
{{- end -}}
{{- $authKeys := keys $auth.auth | sortAlpha -}}
{{- if or (ne (len $authKeys) 1) (ne (first $authKeys) $auth.adapter) -}}
{{- fail (printf "BuildBuddy auth adapter %q requires exactly its matching auth" $auth.adapter) -}}
{{- end -}}
{{- $auth | toJson -}}
{{- end -}}

{{- define "team-namespace.storageAuthAdapter" -}}
{{- $auth := required "storage projection requires secretStore.auth" .secretStore.auth -}}
{{- $keys := keys $auth | sortAlpha -}}
{{- if ne (len $keys) 1 -}}
{{- fail "storage projection auth must select exactly one adapter" -}}
{{- end -}}
{{- $adapter := first $keys -}}
{{- if not (has $adapter (list "aws" "gcp" "kubernetes")) -}}
{{- fail (printf "storage projection auth adapter %q is unsupported" $adapter) -}}
{{- end -}}
{{- $adapter -}}
{{- end -}}

{{- define "team-namespace.storageKubernetesAuth" -}}
{{- $auth := .secretStore.auth -}}
{{- required "Kubernetes storage auth requires kubernetes coordinates" (index $auth "kubernetes") | toJson -}}
{{- end -}}

{{- define "team-namespace.validate" -}}
{{- $slug := required "team.slug is required" .Values.team.slug -}}
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
{{- $_ := include "team-namespace.runtimeSecrets" . | fromJson -}}
{{- $workspacesNamespace := printf "team-%s-workspaces" $slug -}}
{{- $submissionSource := .Values.workspaceSubmissions.sourceNamespace -}}
{{- if and $submissionSource (ne $submissionSource $workspacesNamespace) -}}
{{- fail "workspace submissions must originate from the team's dedicated workspaces namespace" -}}
{{- end -}}
{{- if .Values.devWorkspaces.enabled -}}
{{- $_ := include "team-namespace.buildbuddyAuth" . | fromJson -}}
{{- if ne .Values.team.namespaceSuffix "workspaces" -}}
{{- fail "dev workspaces are limited to the workspaces namespace" -}}
{{- end -}}
{{- if not .Values.storageProjection.enabled -}}
{{- fail "dev workspaces require storage projection" -}}
{{- end -}}
{{- $storageContract := required "dev workspaces require the team storage contract" .Values.storageProjection.handoff.storageContract -}}
{{- if or (eq $storageContract.mounts.home.bucket "global") (ne $storageContract.mounts.home.bucket $storageContract.mounts.scratch.bucket) -}}
{{- fail "dev workspace home and scratch mounts must share one non-global virtual cell" -}}
{{- end -}}
{{- end -}}
{{- if and .Values.devWorkspaces.enabled (eq .Values.team.namespaceSuffix "workspaces") $submissionSource -}}
{{- fail "the workspaces namespace cannot bind its workspace submitter back to itself" -}}
{{- end -}}
{{- if .Values.storageProjection.enabled -}}
{{- $handoff := .Values.storageProjection.handoff -}}
{{- $authAdapter := include "team-namespace.storageAuthAdapter" $handoff -}}
{{- /* floci-divergence: Floci clusters project storage credentials using in-cluster Kubernetes ServiceAccount. */ -}}
{{- if eq $handoff.provider "floci" -}}
{{- if ne $authAdapter "kubernetes" -}}
{{- fail (printf "storage projection provider %q requires Kubernetes auth" $handoff.provider) -}}
{{- end -}}
{{- else if ne $authAdapter $handoff.provider -}}
{{- fail (printf "storage projection provider %q requires matching auth, found %q" $handoff.provider $authAdapter) -}}
{{- end -}}
{{- if and (eq .Values.mode "storage-rbac") (ne $authAdapter "kubernetes") -}}
{{- fail "storage-rbac mode requires Kubernetes storage auth" -}}
{{- end -}}
{{- end -}}
{{- end -}}
