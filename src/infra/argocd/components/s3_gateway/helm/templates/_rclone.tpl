{{- /* Compiles provider-native non-secret rclone backend stanza. */ -}}

{{- define "s3-gateway.rcloneBackend" -}}
[{{ .remote }}]
# floci-divergence: Floci cells connect to Floci S3 via AWS provider and Pod Identity.
{{- if or (eq .cell.provider "aws") (eq .cell.provider "floci") }}
type = s3
provider = AWS
env_auth = true
region = {{ .cell.region }}
no_check_bucket = true
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

{{- define "s3-gateway.legacyTopology" -}}
{{- $topology := include "s3-gateway.activeTopology" . | fromJson -}}
{{- $target := dict -}}
{{- range $cell := $topology.cells -}}
  {{- if eq $cell.name $.Values.targetCell -}}{{- $target = $cell -}}{{- end -}}
{{- end -}}
{{- $targetSlug := "" -}}
{{- $remoteLegacies := list -}}
{{- $seenSlugs := dict -}}
{{- range $b := .Values.legacyStorage -}}
  {{- $slug := or $b.regionSlug (regexReplaceAll "-([a-z])[a-z]+-" $b.region "${1}") -}}
  {{- if not (hasKey $seenSlugs $slug) -}}
    {{- $_ := set $seenSlugs $slug true -}}
    {{- $isTarget := or $b.isTarget (eq $b.region $target.region) (eq $b.upstream $target.virtualName) -}}
    {{- if and $isTarget (not $targetSlug) -}}
      {{- $targetSlug = $slug -}}
    {{- else -}}
      {{- $up := or $b.upstream (printf "aws-%s" $slug) -}}
      {{- $remoteLegacies = append $remoteLegacies (dict "slug" $slug "upstream" $up) -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- if and (not $targetSlug) (gt (len $remoteLegacies) 0) -}}
  {{- $targetSlug = (index $remoteLegacies 0).slug -}}
  {{- $remoteLegacies = slice $remoteLegacies 1 -}}
{{- end -}}
{{- dict "targetSlug" $targetSlug "remoteLegacies" $remoteLegacies | toJson -}}
{{- end -}}

{{- define "s3-gateway.legacyResearchStorage" -}}
{{- if .Values.legacyStorage -}}
{{- $seenProviders := dict -}}
{{- range $b := .Values.legacyStorage -}}
  {{- $pRemote := or $b.providerRemote (printf "legacy_%s_provider" ($b.region | replace "-" "_")) -}}
  {{- if not (hasKey $seenProviders $pRemote) -}}
    {{- $_ := set $seenProviders $pRemote true }}
[{{ $pRemote }}]
type = s3
provider = {{ $b.provider | default "AWS" }}
env_auth = true
region = {{ $b.region }}
{{- if $b.roleArn }}
role_arn = {{ $b.roleArn }}
role_external_id = {{ $b.roleExternalId }}
{{- end }}
no_check_bucket = true
no_head_object = true

{{ end -}}
{{- end -}}
{{- range $b := .Values.legacyStorage }}
[{{ $b.name }}]
type = alias
remote = {{ or $b.providerRemote (printf "legacy_%s_provider" ($b.region | replace "-" "_")) }}:{{ $b.name }}

{{ end -}}
{{- $slugs := list -}}
{{- $slugDatasets := dict -}}
{{- $slugDropzones := dict -}}
{{- range $b := .Values.legacyStorage -}}
  {{- $slug := or $b.regionSlug (regexReplaceAll "-([a-z])[a-z]+-" $b.region "${1}") -}}
  {{- if not (hasKey $slugDatasets $slug) -}}
    {{- $slugs = append $slugs $slug -}}
    {{- $_ := set $slugDatasets $slug (list) -}}
  {{- end -}}
  {{- if eq $b.path "dropzone" -}}
    {{- $_ := set $slugDropzones $slug $b.name -}}
  {{- else if hasPrefix "datasets/" $b.path -}}
    {{- $datasetKey := trimPrefix "datasets/" $b.path -}}
    {{- $curr := index $slugDatasets $slug -}}
    {{- $_ := set $slugDatasets $slug (append $curr (dict "key" $datasetKey "bucket" $b.name)) -}}
  {{- end -}}
{{- end -}}
{{- range $slug := $slugs -}}
{{- $datasets := index $slugDatasets $slug }}
[{{ printf "legacy-datasets-%s" $slug }}]
type = combine
upstreams ={{ range $d := $datasets }} {{ $d.key }}={{ $d.bucket }}:{{ end }}

[{{ printf "workspaces-legacy-%s" $slug }}]
type = combine
upstreams = dropzone={{ index $slugDropzones $slug }}: datasets={{ printf "legacy-datasets-%s" $slug }}:

{{ end -}}
{{- end -}}
{{- end -}}
