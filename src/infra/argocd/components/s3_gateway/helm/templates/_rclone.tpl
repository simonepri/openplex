{{- /* Compiles provider-native non-secret rclone backend stanza. */ -}}

{{- define "s3-gateway.rcloneBackend" -}}
[{{ .remote }}]
# floci-divergence: Floci cells connect to Floci S3 via AWS provider and Pod Identity.
{{- if or (eq .cell.provider "aws") (eq .cell.provider "floci") }}
type = s3
provider = AWS
env_auth = true
region = {{ .cell.region }}
{{- if .noHeadObject }}
no_head_object = true
{{- end }}
{{- else if eq .cell.provider "gcp" }}
type = google cloud storage
env_auth = true
{{- end }}
{{- end -}}

{{- define "s3-gateway.rcloneAlias" -}}
[{{ .remote }}]
type = alias
remote = {{ .providerRemote }}:{{ .bucket.name }}{{ .suffix }}
{{- end -}}

{{- define "s3-gateway.rcloneRemoteGateway" -}}
{{- $providerRemote := printf "%s_gateway" (.cell.virtualName | replace "-" "_") -}}
[{{ $providerRemote }}]
type = s3
provider = Rclone
endpoint = https://{{ .cell.crossRegionServer }}
access_key_id = {{ .accessKeyId }}
{{- if .secretAccessKey }}
secret_access_key = {{ .secretAccessKey }}
{{- end }}
force_path_style = true

[{{ .cell.virtualName }}]
type = alias
remote = {{ $providerRemote }}:{{ .cell.virtualName }}
{{- end -}}
