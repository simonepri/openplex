{{- /* Defines shared labels and names consumed by Envoy Gateway instance chart templates. */ -}}

{{- define "envoy-gateway-instance.labels" -}}
app.kubernetes.io/managed-by: argocd
app.kubernetes.io/name: envoy-gateway-instance
{{- end -}}
