{{- /* Provides the fixed namespace identity and the provider-derived origin, credential, and queue contracts for the shared workspaces namespace. */ -}}

{{- define "coder-workspaces.namespace" -}}
workspaces
{{- end -}}

{{- define "coder-workspaces.labels" -}}
app.kubernetes.io/managed-by: argocd
{{/* LINT.IfChange(coder-workspaces-identity-label) */}}
app.kubernetes.io/part-of: coder-workspaces
{{/* LINT.ThenChange(//src/infra/argocd/components/kyverno/kustomize/team-pod-security-policy.yaml:coder-workspaces-identity-label) */}}
{{- end -}}

{{- define "coder-workspaces.workspacePodSelector" -}}
podSelector:
  matchLabels:
    app.kubernetes.io/name: coder-workspace
    app.kubernetes.io/part-of: coder
    com.coder.resource: "true"
{{- end -}}

{{- /* Derives the ConfigMap origin contract the dev template compares against; the template only knows the aws, floci, and gcp origin providers. */ -}}
{{- define "coder-workspaces.origin" -}}
{{- $provider := .Values.provider -}}
{{- $origin := .Values.origin -}}
{{- /* floci-divergence: Floci cells pull from the local origin registry instead of ECR. */ -}}
{{- if and (ne $provider "floci") (not (regexMatch (printf `^[0-9]{12}\.dkr\.ecr\.%s\.amazonaws\.com$` $origin.region) $origin.registry)) -}}
{{- fail (printf "origin registry %q must be an ECR registry in origin region %q" $origin.registry $origin.region) -}}
{{- end -}}
{{- if eq $provider "aws" -}}
{{- dict "authMode" "eks-pod-identity" "provider" "aws" "tokenAudience" "" "tokenFile" "" | toJson -}}
{{- else if eq $provider "gcp" -}}
{{- dict "authMode" "web-identity" "provider" "gcp" "tokenAudience" "sts.amazonaws.com" "tokenFile" "/var/run/secrets/workload-origin/token" | toJson -}}
{{- else -}}
{{- /* floci-divergence: Floci workspaces push to the local origin registry without cloud credentials. */ -}}
{{- if not (hasSuffix (printf "/%s" $origin.region) $origin.registry) -}}
{{- fail (printf "Floci origin registry %q must end with origin region %q" $origin.registry $origin.region) -}}
{{- end -}}
{{- dict "authMode" "floci" "provider" "floci" "tokenAudience" "" "tokenFile" "" | toJson -}}
{{- end -}}
{{- end -}}
