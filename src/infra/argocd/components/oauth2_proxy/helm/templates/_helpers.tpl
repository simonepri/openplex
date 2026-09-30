{{- /* Defines shared naming and labels for the oauth2-proxy integration chart. */ -}}

{{- define "oauth2-proxy-integration.labels" -}}
app.kubernetes.io/component: {{ .component }}
app.kubernetes.io/managed-by: argocd
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/part-of: {{ .partOf }}
{{- end -}}

{{- define "oauth2-proxy-integration.identityLabels" -}}
app.kubernetes.io/component: identity
app.kubernetes.io/managed-by: external-secrets
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/part-of: {{ .partOf }}
{{- end -}}
