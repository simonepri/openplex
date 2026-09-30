{{- /* Defines fail-closed validation and shared names for Kargo promotion pipelines. */ -}}

{{- define "kargo-promotion.projectName" -}}
{{- printf "apps-%s" .Values.teamSlug -}}
{{- end -}}

{{- define "kargo-promotion.images" -}}
{{- $images := .Values.images -}}
{{- if eq .Values.mode "warehouse" -}}
{{- $images = list -}}
{{- range $warehouse := .Values.warehouses -}}
{{- $images = concat $images $warehouse.images -}}
{{- end -}}
{{- end -}}
{{- $images | toJson -}}
{{- end -}}

{{- define "kargo-promotion.validate" -}}
{{- if ne .Values.promotionMode "automatic" -}}
{{- fail (printf "promotionMode %q is unsupported; only the automatic team policy is implemented" .Values.promotionMode) -}}
{{- end -}}
{{- if has .Values.mode (list "stage" "warehouse") -}}
{{- $images := include "kargo-promotion.images" . | fromJsonArray -}}
{{- if ne (len .Values.originRepositories) (len $images) -}}
{{- fail "originRepositories must contain exactly one entry for every image package" -}}
{{- end -}}
{{- if and (eq .Values.mode "stage") (ne (len .Values.stage.deploymentRepositories) (len $images)) -}}
{{- fail "stage.deploymentRepositories must contain exactly one entry for every image package" -}}
{{- end -}}
{{- if eq .Values.mode "warehouse" -}}
{{- $warehouseNames := dict -}}
{{- range $warehouse := .Values.warehouses -}}
{{- if hasKey $warehouseNames $warehouse.name -}}
{{- fail (printf "warehouses contains duplicate name %q" $warehouse.name) -}}
{{- end -}}
{{- $_ := set $warehouseNames $warehouse.name true -}}
{{- end -}}
{{- end -}}
{{- $sources := dict -}}
{{- $manifests := dict -}}
{{- $repositories := dict -}}
{{- range $image := $images -}}
{{- if or (hasKey $sources $image.sourceName) (hasKey $manifests $image.manifestRepository) (hasKey $repositories $image.repository) -}}
{{- fail (printf "images contains a duplicate source, manifest repository, or origin repository path for %q" $image.sourceName) -}}
{{- end -}}
{{- if ne $image.package $image.repository -}}
{{- fail (printf "image %q package and repository must be identical" $image.sourceName) -}}
{{- end -}}
{{- if ne $image.target (printf "//%s:image_push" $image.package) -}}
{{- fail (printf "image %q target must be its package image_push target" $image.sourceName) -}}
{{- end -}}
{{- $_ := required (printf "originRepositories requires image package %q" $image.package) (index $.Values.originRepositories $image.package) -}}
{{- if eq $.Values.mode "stage" -}}
{{- $_ := required (printf "stage.deploymentRepositories requires image package %q" $image.package) (index $.Values.stage.deploymentRepositories $image.package) -}}
{{- end -}}
{{- $_ := set $sources $image.sourceName true -}}
{{- $_ := set $manifests $image.manifestRepository true -}}
{{- $_ := set $repositories $image.repository true -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "kargo-promotion.labels" -}}
app.kubernetes.io/managed-by: argocd
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/part-of: kargo
team: {{ .team | quote }}
{{- end -}}
