{{- /* Renders secret-only rclone environment overrides for one team. */ -}}

{{- define "s3-gateway-team.secretEnv" -}}
{{- $root := .root -}}
{{- if eq $root.Values.authAdapter "kubernetes" }}
- name: RCLONE_CONFIG_LOCAL_HOME_PROVIDER_SECRET_ACCESS_KEY
  valueFrom: {secretKeyRef: {key: writer-secret-key, name: ray-data-local-object-credentials}}
- name: RCLONE_CONFIG_LOCAL_SCRATCH_PROVIDER_SECRET_ACCESS_KEY
  valueFrom: {secretKeyRef: {key: writer-secret-key, name: ray-data-local-object-credentials}}
- name: RCLONE_CONFIG_LOCAL_META_PROVIDER_SECRET_ACCESS_KEY
  valueFrom: {secretKeyRef: {key: storage-stats-reader-secret-key, name: ray-data-local-object-credentials}}
{{- end }}
{{- range $cell := .remoteCells }}
- name: {{ printf "RCLONE_CONFIG_%s_GATEWAY_SECRET_ACCESS_KEY" ($cell.virtualName | upper | replace "-" "_") }}
  valueFrom:
    secretKeyRef:
      key: {{ printf "%s-secret-access-key" $root.Values.team }}
      name: s3-gateway-credentials
{{- end }}
{{- end -}}
