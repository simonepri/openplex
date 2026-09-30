{{- /* Generates shared Kubernetes resource labels for the snapshot portal chart. */ -}}

{{- define "coder-snapshot-portal.labels" -}}
app.kubernetes.io/component: snapshot-portal
app.kubernetes.io/managed-by: argocd
app.kubernetes.io/name: coder-snapshot-portal
{{- end -}}
