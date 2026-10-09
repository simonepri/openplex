{{- /* Provides shared validation and routing identifier helpers for gateway config. */ -}}

{{- define "s3-gateway.credentialRegex" -}}
^AWS4-HMAC-SHA256 Credential={{ regexQuoteMeta . }}/[^,]+,.*$
{{- end -}}

{{- define "s3-gateway.activeTopology" -}}
{{- $catalog := dict -}}
{{- range $cell := .Values.cellCatalog -}}
  {{- if hasKey $catalog $cell.name -}}{{- fail "cellCatalog names must be unique" -}}{{- end -}}
  {{- $cellCopy := deepCopy $cell -}}
  {{- if and $.Values.internalDomain (eq $cellCopy.name $.Values.targetCell) -}}
    {{- $_ := set $cellCopy "crossRegionServer" (printf "s3-gateway.%s" $.Values.internalDomain) -}}
    {{- $_ := set $cellCopy "storageStatsServer" (printf "s3-gateway.%s" $.Values.internalDomain) -}}
  {{- end -}}
  {{- $_ := set $catalog $cell.name $cellCopy -}}
{{- end -}}
{{- $cells := list -}}
{{- range $name := .Values.registeredCells -}}
  {{- if not (hasKey $catalog $name) -}}{{- fail (printf "registered cell %q has no storage contract" $name) -}}{{- end -}}
  {{- $cells = append $cells (index $catalog $name) -}}
{{- end -}}
{{- $globalStorage := required "globalStorage is required" .Values.globalStorage -}}
{{- $bucketPrefix := required "globalStorage.bucketPrefix is required" $globalStorage.bucketPrefix -}}
{{- $bucketSuffix := required "globalStorage.bucketSuffix is required" $globalStorage.bucketSuffix -}}
{{- $endpoint := required "globalStorage.endpoint is required" $globalStorage.endpoint -}}
{{- dict "cells" $cells "global" (dict "bucketPrefix" $bucketPrefix "bucketSuffix" $bucketSuffix "endpoint" $endpoint) "version" 1 | toJson -}}
{{- end -}}

{{- define "s3-gateway.validate" -}}
{{- $topology := include "s3-gateway.activeTopology" . | fromJson -}}
{{- $cellNames := dict -}}
{{- $virtualNames := dict -}}
{{- $targetCount := 0 -}}
{{- range $cell := $topology.cells -}}
  {{- if eq $cell.name $.Values.targetCell -}}
  {{- $targetCount = add1 $targetCount -}}
  {{- end -}}
  {{- if or (hasKey $cellNames $cell.name) (hasKey $virtualNames $cell.virtualName) -}}
  {{- fail "topology cell names and virtual names must be unique" -}}
  {{- end -}}
  {{- $_ := set $cellNames $cell.name true -}}
  {{- $_ := set $virtualNames $cell.virtualName true -}}
  {{- if ne $cell.virtualName (trimPrefix "cell-" $cell.name) -}}
  {{- fail "each virtualName must derive from its cell name" -}}
  {{- end -}}
  {{- if ne $cell.crossRegionService (printf "s3-gateway-cross-region-%s" $cell.name) -}}
  {{- fail "each crossRegionService must derive from its cell name" -}}
  {{- end -}}
{{- end -}}
{{- if ne $targetCount 1 -}}
{{- fail "targetCell must identify exactly one topology cell" -}}
{{- end -}}
{{- end -}}
