{{- /* Renders secret-only rclone environment overrides for one team. */ -}}

{{- define "s3-gateway-team.secretEnv" -}}
{{- $root := .root -}}
{{- if eq $root.Values.authAdapter "kubernetes" }}
- name: RCLONE_CONFIG_LOCAL_HOME_PROVIDER_SECRET_ACCESS_KEY
  valueFrom: {secretKeyRef: {key: writer-secret-key, name: ray-data-local-object-credentials}}
- name: RCLONE_CONFIG_LOCAL_SCRATCH_PROVIDER_SECRET_ACCESS_KEY
  valueFrom: {secretKeyRef: {key: writer-secret-key, name: ray-data-local-object-credentials}}
{{- end }}
{{- range $cell := .remoteCells }}
- name: {{ printf "RCLONE_CONFIG_%s_GATEWAY_SECRET_ACCESS_KEY" ($cell.virtualName | upper | replace "-" "_") }}
  valueFrom:
    secretKeyRef:
      key: {{ printf "%s-secret-access-key" $root.Values.team }}
      name: s3-gateway-credentials
{{- end }}
{{- end -}}

{{- /* Computes deterministic access key ID for a team. */ -}}
{{- define "s3-gateway-team.accessKeyId" -}}
{{- /* # floci-divergence: Floci emulates team storage credentials with static access keys for the examples team. */ -}}
{{- if and (eq .Values.provider "floci") (eq .Values.team "examples") -}}
33333333333333333333
{{- else -}}
{{- printf "OPEN%s" (upper (trunc 16 (sha1sum .Values.team))) -}}
{{- end -}}
{{- end -}}

{{- /* Computes deterministic access key ID for a team reader. */ -}}
{{- define "s3-gateway-team.readerAccessKeyId" -}}
{{- printf "OPEN%s" (upper (trunc 16 (sha1sum (printf "%s-reader" .Values.team)))) -}}
{{- end -}}

{{- /* Matches SigV4 authorization header for a credential. */ -}}
{{- define "s3-gateway-team.credentialRegex" -}}
^AWS4-HMAC-SHA256 Credential={{ regexQuoteMeta . }}/[^,]+,.*$
{{- end -}}

{{- /* Materializes rclone configuration for one team. */ -}}
{{- define "s3-gateway-team.rcloneConfig" -}}
{{- $targetCount := 0 -}}
{{- $target := dict -}}
{{- $remoteCells := list -}}
{{- range $cell := .Values.topology.cells -}}
  {{- if eq $cell.name $.Values.targetCell -}}
    {{- $targetCount = add1 $targetCount -}}
    {{- $target = $cell -}}
  {{- else -}}
    {{- $remoteCells = append $remoteCells $cell -}}
  {{- end -}}
{{- end -}}
{{- if ne $targetCount 1 -}}
{{- fail "targetCell must identify exactly one topology cell" -}}
{{- end -}}
{{- $targetProvider := default .Values.provider (dig "provider" "" $target) -}}
{{- $targetRegion := coalesce (dig "region" "" $target) (ternary "lh1" (ternary "us-west-2" "europe-west4" (hasPrefix "cell-aws" $target.name)) (hasPrefix "cell-eaws" $target.name)) -}}
{{- $homeBucket := coalesce (dig "buckets" "home" "name" "" $target) (printf "cloud-%s-home" $target.name) -}}
{{- $scratchBucket := coalesce (dig "buckets" "scratch" "name" "" $target) (printf "cloud-%s-scratch" $target.name) -}}
{{- $metaBucket := coalesce (dig "buckets" "meta" "name" "" $target) (printf "cloud-%s-meta" $target.name) -}}
{{- $domain := coalesce (dig "clusterDomain" "" $target) (dig "domain" "" $target) .Values.clusterDomain .Values.domain "" -}}
{{- $team := required "team is required" .Values.team -}}
{{- $globalStorage := required "globalStorage is required" .Values.globalStorage -}}
{{- $globalProvider := required "globalStorage.provider is required" $globalStorage.provider -}}
{{- $globalEndpoint := required "globalStorage.endpoint is required" $globalStorage.endpoint -}}
{{- $bucketPrefix := required "globalStorage.bucketPrefix is required" $globalStorage.bucketPrefix -}}
{{- $bucketSuffix := required "globalStorage.bucketSuffix is required" $globalStorage.bucketSuffix -}}
{{- $globalBucket := printf "%s-%s-%s" $bucketPrefix $team $bucketSuffix -}}
# floci-divergence: Floci cells connect to Floci S3 via AWS provider and Pod Identity.
{{- if or (eq $targetProvider "aws") (eq $targetProvider "floci") }}
[local_home_provider]
type = s3
provider = AWS
env_auth = true
region = {{ $targetRegion }}
no_head_object = true
no_check_bucket = true

[local-home]
type = alias
remote = local_home_provider:{{ $homeBucket }}

[local_scratch_provider]
type = s3
provider = AWS
env_auth = true
region = {{ $targetRegion }}
no_head_object = true
no_check_bucket = true

[local-scratch]
type = alias
remote = local_scratch_provider:{{ $scratchBucket }}

[local_meta_provider]
type = s3
provider = AWS
env_auth = true
region = {{ $targetRegion }}
no_head_object = true
no_check_bucket = true

[local-meta]
type = alias
remote = local_meta_provider:{{ $metaBucket }}
{{- else if eq $targetProvider "gcp" }}
[local_home_provider]
type = google cloud storage
env_auth = true

[local-home]
type = alias
remote = local_home_provider:{{ $homeBucket }}

[local_scratch_provider]
type = google cloud storage
env_auth = true

[local-scratch]
type = alias
remote = local_scratch_provider:{{ $scratchBucket }}

[local_meta_provider]
type = google cloud storage
env_auth = true

[local-meta]
type = alias
remote = local_meta_provider:{{ $metaBucket }}
{{- end }}

[local-cell]
type = combine
upstreams = home=local-home:home scratch=local-scratch:scratch meta=local-meta:meta

[global_store]
type = s3
provider = {{ $globalProvider }}
endpoint = {{ $globalEndpoint }}

[global-home]
type = combine
upstreams = {{ .Values.team }}=global_store:{{ $globalBucket }}/home

[global-scratch]
type = combine
upstreams = {{ .Values.team }}=global_store:{{ $globalBucket }}/scratch

[global-meta]
type = alias
remote = global_store:{{ $globalBucket }}/meta

[global]
type = combine
upstreams = home=global-home: scratch=global-scratch: meta=global-meta:

[local]
type = combine
upstreams = {{ $target.virtualName }}=local-cell: global=global:

[cross-region]
type = combine
upstreams = {{ $target.virtualName }}=local-cell: global=global:{{ range $cell := $remoteCells }} {{ $cell.virtualName }}={{ $cell.virtualName }}:{{ end }}
{{- range $cell := $remoteCells }}
{{- $remoteProvider := printf "%s_gateway" ($cell.virtualName | replace "-" "_") }}
{{- $crossRegionServer := coalesce (dig "crossRegionServer" "" $cell) (and $domain (printf "s3-gateway.%s.%s" $cell.name $domain)) (printf "s3-gateway.%s" $cell.name) }}
{{- $teamAccessKey := "" }}
{{- if and (hasKey $cell "callers") (hasKey $cell.callers "teams") (hasKey $cell.callers.teams $.Values.team) }}
  {{- $teamAccessKey = (index $cell.callers.teams $.Values.team).accessKeyId }}
  # floci-divergence: Floci emulates remote gateway caller credentials with static keys when not configured.
{{- else if eq (default "" $cell.provider) "floci" }}
  {{- $teamAccessKey = "33333333333333333333" }}
{{- else }}
  {{- $teamAccessKey = printf "OPEN%s" (upper (trunc 16 (sha1sum $.Values.team))) }}
{{- end }}

[{{ $remoteProvider }}]
type = s3
provider = Rclone
endpoint = https://{{ $crossRegionServer }}
access_key_id = {{ $teamAccessKey }}
force_path_style = true

[{{ $cell.virtualName }}]
type = alias
remote = {{ $remoteProvider }}:{{ $cell.virtualName }}
{{- end }}
{{- end -}}
