{{- /* Derives Kueue quotas from canonical per-cell compute sizing records. */ -}}

{{/* Convert whole CPU/accelerator and whole-Gi memory quantities to integers. */}}
{{- define "kueue-admission.quantityInt" -}}
{{- $quantity := printf "%v" (index . 0) -}}
{{- if hasSuffix "Gi" $quantity -}}
{{- trimSuffix "Gi" $quantity | int -}}
{{- else -}}
{{- $quantity | int -}}
{{- end -}}
{{- end -}}

{{/* Return one class floor, defaulting an omitted accelerator to zero. */}}
{{- define "kueue-admission.floor" -}}
{{- $compute := index . 0 -}}
{{- $class := index . 1 -}}
{{- $resource := index . 2 -}}
{{- default "0" (index (index $compute.floors $class) $resource) -}}
{{- end -}}

{{/* Return the ceiling not reserved by class floors. */}}
{{- define "kueue-admission.elastic" -}}
{{- $compute := index . 0 -}}
{{- $resource := index . 1 -}}
{{- $ceiling := include "kueue-admission.quantityInt" (list (default "0" (index $compute.ceiling $resource))) | int -}}
{{- $reserved := 0 -}}
{{- range $class := list "ha" "ma" "wa" "be" -}}
{{- $floor := include "kueue-admission.floor" (list $compute $class $resource) -}}
{{- $reserved = add $reserved (include "kueue-admission.quantityInt" (list $floor) | int) -}}
{{- end -}}
{{- $available := sub $ceiling $reserved -}}
{{- if lt $available 0 -}}
{{- fail (printf "compute %s ceiling is smaller than its class floors" $resource) -}}
{{- end -}}
{{- $available -}}
{{- end -}}

{{/* Render integer memory as a Kubernetes Gi quantity and other resources literally. */}}
{{- define "kueue-admission.resourceQuantity" -}}
{{- $resource := index . 0 -}}
{{- $quantity := include "kueue-admission.quantityInt" (list (index . 1)) | int -}}
{{- if and (eq $resource "memory") (gt $quantity 0) -}}
{{- printf "%dGi" $quantity -}}
{{- else -}}
{{- printf "%d" $quantity -}}
{{- end -}}
{{- end -}}

{{/* Return one team's class quota, defaulting an omitted accelerator to zero. */}}
{{- define "kueue-admission.teamQuota" -}}
{{- $team := index . 0 -}}
{{- $class := index . 1 -}}
{{- $resource := index . 2 -}}
{{- $val := dig "quota" "classes" $class "accelerators" $resource "" $team -}}
{{- if eq $val "" -}}
{{- $val = dig "quota" "classes" $class $resource "0" $team -}}
{{- end -}}
{{- $val -}}
{{- end -}}

{{/* Sum team-owned nominal quota for one class and resource. */}}
{{- define "kueue-admission.teamAllocated" -}}
{{- $teams := index . 0 -}}
{{- $class := index . 1 -}}
{{- $resource := index . 2 -}}
{{- $allocated := 0 -}}
{{- range $team := $teams -}}
{{- $quota := include "kueue-admission.teamQuota" (list $team $class $resource) -}}
{{- $allocated = add $allocated (include "kueue-admission.quantityInt" (list $quota) | int) -}}
{{- end -}}
{{- $allocated -}}
{{- end -}}

{{/* Leave unassigned class floor as a shared pool for that class's teams. */}}
{{- define "kueue-admission.classShared" -}}
{{- $compute := index . 0 -}}
{{- $teams := index . 1 -}}
{{- $class := index . 2 -}}
{{- $resource := index . 3 -}}
{{- $floor := include "kueue-admission.quantityInt" (list (include "kueue-admission.floor" (list $compute $class $resource))) | int -}}
{{- $allocated := include "kueue-admission.teamAllocated" (list $teams $class $resource) | int -}}
{{- $shared := sub $floor $allocated -}}
{{- if lt $shared 0 -}}
{{- fail (printf "team %s %s quota exceeds the cell class floor" $class $resource) -}}
{{- end -}}
{{- $shared -}}
{{- end -}}

{{/* Sum TPU chips across provider-owned fixed pools. */}}
{{- define "kueue-admission.fixedTpuTotal" -}}
{{- $compute := index . 0 -}}
{{- $total := 0 -}}
{{- range $pool := (default dict $compute.fixed_pools) -}}
{{- $offer := default dict (index $compute.tpu_classes $pool.class) -}}
{{- $chips := dig "max_count" 0 $offer | int -}}
{{- $total = add $total (mul $pool.nodes $chips) -}}
{{- end -}}
{{- $total -}}
{{- end -}}

{{/* Report whether a whole TPU quota is divisible by an offered class size. */}}
{{- define "kueue-admission.tpuQuantityFits" -}}
{{- $quantity := index . 0 | int -}}
{{- $classes := index . 1 -}}
{{- $fit := dict -}}
{{- if eq $quantity 0 -}}
{{- $_ := set $fit "valid" true -}}
{{- end -}}
{{- range $class, $offer := $classes -}}
{{- if eq (mod $quantity ($offer.max_count | int)) 0 -}}
{{- $_ := set $fit "valid" true -}}
{{- end -}}
{{- end -}}
{{- if hasKey $fit "valid" -}}true{{- else -}}false{{- end -}}
{{- end -}}

{{/* Fail closed on compute records the chart cannot express faithfully. */}}
{{- define "kueue-admission.validateCompute" -}}
{{- if eq .Values.role "cell" -}}
{{- $teamSlugs := dict -}}
{{- $tpuClasses := .Values.compute.tpu_classes -}}
{{- range $team := .Values.teams -}}
{{- if hasKey $teamSlugs $team.slug -}}
{{- fail (printf "teams contains duplicate slug %q" $team.slug) -}}
{{- end -}}
{{- $_ := set $teamSlugs $team.slug true -}}
{{- range $class, $quota := $team.quota.classes -}}
{{- $tpu := include "kueue-admission.quantityInt" (list (default "0" (index $quota "google.com/tpu"))) | int -}}
{{- if ne (include "kueue-admission.tpuQuantityFits" (list $tpu $tpuClasses)) "true" -}}
{{- fail (printf "team %s google.com/tpu %s quota does not fit an offered TPU class" $team.slug $class) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- range $resource := list "cpu" "memory" "nvidia.com/gpu" "google.com/tpu" -}}
{{- $_ := include "kueue-admission.elastic" (list $.Values.compute $resource) -}}
{{- range $class := list "ha" "ma" "wa" "be" -}}
{{- $_ := include "kueue-admission.classShared" (list $.Values.compute $.Values.teams $class $resource) -}}
{{- end -}}
{{- end -}}
{{- $tpu := include "kueue-admission.quantityInt" (list (default "0" (index .Values.compute.ceiling "google.com/tpu"))) | int -}}
{{- if and (gt $tpu 0) (ne .Values.provider "gcp") -}}
{{- fail "google.com/tpu quota is supported only by the GCP provider" -}}
{{- end -}}
{{- if and (gt (len $tpuClasses) 0) (ne .Values.provider "gcp") -}}
{{- fail "TPU classes are supported only by the GCP provider" -}}
{{- end -}}
{{- if ne (gt $tpu 0) (gt (len $tpuClasses) 0) -}}
{{- fail "TPU classes must be offered exactly when google.com/tpu capacity is non-zero" -}}
{{- end -}}
{{- $fixedPools := default dict .Values.compute.fixed_pools -}}
{{- if and (gt $tpu 0) (ne (len $fixedPools) 2) -}}
{{- fail "TPU capacity requires exactly two canonical fixed pools" -}}
{{- end -}}
{{- if and (eq $tpu 0) (gt (len $fixedPools) 0) -}}
{{- fail "fixed TPU pools require a non-zero google.com/tpu ceiling" -}}
{{- end -}}
{{- if ne (include "kueue-admission.tpuQuantityFits" (list $tpu $tpuClasses)) "true" -}}
{{- fail "google.com/tpu ceiling does not fit an offered TPU class" -}}
{{- end -}}
{{- if and (gt $tpu 0) (ne $tpu (include "kueue-admission.fixedTpuTotal" (list .Values.compute) | int)) -}}
{{- fail "google.com/tpu ceiling must equal canonical fixed-pool capacity" -}}
{{- end -}}
{{- range $name, $pool := $fixedPools -}}
{{- if not (hasKey $tpuClasses $pool.class) -}}
{{- fail (printf "fixed pool %s references unoffered TPU class %s" $name $pool.class) -}}
{{- end -}}
{{- $chips := (index $tpuClasses $pool.class).max_count | int -}}
{{- $spot := eq $name "tpu-spot" -}}
{{- $reserved := 0 -}}
{{- range $class := list "ha" "ma" "wa" "be" -}}
{{- $stableFirst := or (eq $class "ha") (eq $class "ma") -}}
{{- if eq $spot (not $stableFirst) -}}
{{- $reserved = add $reserved (include "kueue-admission.quantityInt" (list (include "kueue-admission.floor" (list $.Values.compute $class "google.com/tpu"))) | int) -}}
{{- end -}}
{{- end -}}
{{- if gt $reserved (mul $pool.nodes $chips) -}}
{{- fail (printf "fixed pool %s cannot satisfy its google.com/tpu class floors" $name) -}}
{{- end -}}
{{- end -}}
{{- range $class, $_ := $tpuClasses -}}
{{- if or (ne (index (index $fixedPools "tpu") "class") $class) (ne (index (index $fixedPools "tpu-spot") "class") $class) -}}
{{- fail (printf "TPU class %s requires on-demand and Spot fixed-pool witnesses" $class) -}}
{{- end -}}
{{- end -}}
{{- range $class := list "ha" "ma" "wa" "be" -}}
{{- $floor := include "kueue-admission.quantityInt" (list (include "kueue-admission.floor" (list $.Values.compute $class "google.com/tpu"))) | int -}}
{{- if ne (include "kueue-admission.tpuQuantityFits" (list $floor $tpuClasses)) "true" -}}
{{- fail (printf "google.com/tpu %s floor does not fit an offered TPU class" $class) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
