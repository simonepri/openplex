{{- /* Provides shared validation, resource-pack composition, and selectors for one team. */ -}}

{{- define "fleet-team.registeredClusters" -}}
{{- if not (hasKey .Values "registeredClusters") -}}
  {{- fail "registeredClusters is required" -}}
{{- end -}}
{{- if not (gt (len .Values.registeredClusters) 0) -}}
  {{- fail "registeredClusters must not be empty" -}}
{{- end -}}
{{- $clusterProviders := dict -}}
{{- $clusterSuffixes := dict -}}
{{- range $rc := .Values.registeredClusters -}}
  {{- $rcLabels := default dict (index $rc "labels") -}}
  {{- $rcProvider := index $rcLabels "provider" -}}
  {{- if $rcProvider -}}
    {{- $_ := set $clusterProviders $rc.name $rcProvider -}}
  {{- end -}}
  {{- $rcAnno := default dict (index $rc "annotations") -}}
  {{- $rcSuffix := index $rcAnno "bucket-suffix" -}}
  {{- if $rcSuffix -}}
    {{- $_ := set $clusterSuffixes $rc.name $rcSuffix -}}
  {{- end -}}
{{- end -}}
{{- $registered := list -}}
{{- range $registration := .Values.registeredClusters -}}
  {{- $name := required "registered cluster requires a name" $registration.name -}}
  {{- $server := required (printf "registered cluster %q requires a server" $name) $registration.server -}}
  {{- $labels := default dict (index $registration "labels") -}}
  {{- $annotations := default dict (index $registration "annotations") -}}
  {{- $role := index $labels "role" -}}
  {{- if not $role -}}
    {{- fail (printf "registered cluster %q requires label role" $name) -}}
  {{- end -}}
  {{- if and (ne $role "cell") (ne $role "ctrl") -}}
    {{- fail (printf "registered cluster %q label role must be 'cell' or 'ctrl', got %q" $name $role) -}}
  {{- end -}}
  {{- $provider := index $labels "provider" -}}
  {{- if not $provider -}}
    {{- fail (printf "registered cluster %q requires label provider" $name) -}}
  {{- end -}}
  {{- if not (has $provider (list "aws" "floci" "gcp")) -}}
    {{- fail (printf "registered cluster %q label provider %q is unsupported" $name $provider) -}}
  {{- end -}}
  {{- $region := "" -}}
  {{- if eq $provider "aws" -}}
    {{- $region = index $annotations "aws-region" -}}
    {{- if not $region -}}
      {{- fail (printf "registered cluster %q on aws requires aws-region" $name) -}}
    {{- end -}}
  {{- /* # floci-divergence: Floci local clusters default to the lh1 region when unspecified. */ -}}
  {{- else if eq $provider "floci" -}}
    {{- $region = or (index $labels "region") "lh1" -}}
  {{- else if eq $provider "gcp" -}}
    {{- $region = index $labels "region" -}}
    {{- if not $region -}}
      {{- fail (printf "registered cluster %q on gcp requires label region" $name) -}}
    {{- end -}}
  {{- end -}}
  {{- /* # floci-divergence: Floci local environments default cloud selector to eaws (emulated AWS). */ -}}
  {{- $cloud := or (index $labels "cloud") (ternary "eaws" $provider (eq $provider "floci")) -}}
  {{- $cluster := dict "cloud" $cloud "labels" $labels "name" $name "provider" $provider "region" $region "role" $role "server" $server -}}
  {{- if eq $role "ctrl" -}}
    {{- /* floci-divergence: Floci local control planes use local origin registry workload root. */ -}}
    {{- if eq $provider "floci" -}}
      {{- $workloadRoot := or (index $annotations "origin-registry-workload-root") "origin-registry:5000/000000000000/us-east-1" -}}
      {{- $_ := set $cluster "localRuntime" (dict "originRegistry" (dict "workloadRoot" $workloadRoot)) -}}
    {{- end -}}
  {{- else -}}
    {{- $accountID := or (index $annotations "aws-account-id") (index $.Values "awsAccountId") -}}
    {{- $ecrRegistry := or (index $annotations "ecr-registry") (index $.Values "ecrRegistry") -}}
    {{- $gcpProjectId := or (index $annotations "gcp-project-id") (index $.Values "gcpProjectId") -}}
    {{- if and (eq $provider "gcp") $gcpProjectId -}}
      {{- $_ := set $cluster "providerConfig" (dict "gcp" (dict "projectID" $gcpProjectId)) -}}
    {{- end -}}
    {{- $deploymentRepos := dict -}}
    {{- $packages := list -}}
    {{- range $img := include "fleet-team.images" $ | fromJsonArray -}}
      {{- $packages = append $packages $img.package -}}
    {{- end -}}
    {{- if and (eq (len $packages) 0) (hasKey $.Values "delivery") -}}
      {{- if hasKey $.Values.delivery "originRepositories" -}}
        {{- $packages = keys $.Values.delivery.originRepositories | sortAlpha -}}
      {{- else if hasKey $.Values.delivery "images" -}}
        {{- range $img := $.Values.delivery.images -}}
          {{- $packages = append $packages $img.package -}}
        {{- end -}}
      {{- end -}}
    {{- end -}}
    {{- /* # floci-divergence: Floci local clusters pull deployment repositories from the local registry endpoint. */ -}}
    {{- if eq $provider "floci" -}}
      {{- range $pkg := $packages -}}
        {{- $_ := set $deploymentRepos $pkg (printf "localhost:15100/000000000000/us-east-1/%s" $pkg) -}}
      {{- end -}}
    {{- else if $ecrRegistry -}}
      {{- range $pkg := $packages -}}
        {{- $_ := set $deploymentRepos $pkg (printf "%s/%s" $ecrRegistry $pkg) -}}
      {{- end -}}
    {{- else -}}
      {{- range $pkg := $packages -}}
        {{- if and (hasKey $.Values "delivery") (hasKey $.Values.delivery "originRepositories") (hasKey $.Values.delivery.originRepositories $pkg) -}}
          {{- $_ := set $deploymentRepos $pkg (index $.Values.delivery.originRepositories $pkg) -}}
        {{- end -}}
      {{- end -}}
    {{- end -}}
    {{- if gt (len $deploymentRepos) 0 -}}
      {{- $_ := set $cluster "delivery" (dict "deploymentRepositories" $deploymentRepos) -}}
    {{- end -}}
    {{- if and (eq $provider "gcp") $accountID -}}
      {{- $pullRegion := or (index $annotations "workload-image-pull-region") "us-west-2" -}}
      {{- $pullRole := printf "arn:aws:iam::%s:role/%s-%s-ecr-pull" $accountID $name $.Values.team.slug -}}
      {{- $_ := set $cluster "workloadImagePull" (dict "region" $pullRegion "roles" (dict $.Values.team.slug $pullRole)) -}}
    {{- end -}}
    {{- $registeredCellsAnno := index $annotations "registered-cells" -}}
    {{- $hasStorage := or $registeredCellsAnno (index $annotations "storage-endpoint") (index $annotations "s3-endpoint") -}}
    {{- if $hasStorage -}}
      {{- $regCells := list -}}
      {{- if $registeredCellsAnno -}}
        {{- range $c := splitList "," $registeredCellsAnno -}}
          {{- with trim $c -}}
            {{- $regCells = append $regCells . -}}
          {{- end -}}
        {{- end -}}
      {{- else -}}
        {{- $regCells = list $name -}}
      {{- end -}}
      {{- $domain := or (index $annotations "cluster-domain") (index $annotations "access-domain") $.Values.accessAliasDomain -}}
      {{- if not $domain -}}
        {{- fail (printf "registered cluster %q requires accessAliasDomain or cluster-domain annotation" $name) -}}
      {{- end -}}
      {{- $gatewayCells := list -}}
      {{- range $c := $regCells -}}
        {{- $cProvider := index $clusterProviders $c -}}
        {{- if not $cProvider -}}
          {{- if eq $c $name -}}
            {{- $cProvider = $provider -}}
          {{- else -}}
            {{- fail (printf "cell %q listed in registered-cells must be present in registeredClusters with label provider" $c) -}}
          {{- end -}}
        {{- end -}}
        {{- $cVirtualName := trimPrefix "cell-" $c -}}
        {{- $cSuffix := index $clusterSuffixes $c -}}
        {{- $cSuffix = required (printf "%s requires the bucket-suffix annotation" $c) $cSuffix -}}
        {{- $homeBucket := printf "%s-home-%s" $c $cSuffix -}}
        {{- $scratchBucket := printf "%s-scratch-%s" $c $cSuffix -}}
        {{- $metaBucket := printf "%s-meta-%s" $c $cSuffix -}}
        {{- $gatewayCells = append $gatewayCells (dict
              "name" $c
              "provider" $cProvider
              "virtualName" $cVirtualName
              "crossRegionServer" (printf "s3-gateway.%s.%s" $c $domain)
              "buckets" (dict
                "home" (dict "name" $homeBucket)
                "scratch" (dict "name" $scratchBucket)
                "meta" (dict "name" $metaBucket))
            ) -}}
      {{- end -}}
      {{- $gatewayConfig := dict
            "version" 1
            "cells" $gatewayCells -}}
      {{- $_ := set $cluster "globalStorage" (dict
            "endpoint" (index $annotations "global-storage-endpoint")
            "bucketPrefix" (index $annotations "global-storage-bucket-prefix")
            "bucketSuffix" (index $annotations "global-storage-bucket-suffix")
            "provider" (index $annotations "global-storage-provider")) -}}
      {{- $team := $.Values.team.slug -}}
      {{- $handoff := dict -}}
      {{- if eq $provider "aws" -}}
        {{- $secretStoreAuth := dict "aws" (dict "region" $region) -}}
        {{- $handoff = dict
              "provider" "aws"
              "recordName" (printf "%s-s3-team-%s" $name $team)
              "secretStore" (dict
                "name" "team-s3"
                "serviceAccount" "team-s3"
                "auth" $secretStoreAuth
              )
              "externalSecret" (dict
                "name" "team-s3"
                "targetSecretName" "team-s3"
                "remoteProperties" (dict
                  "accessKeyId" "access_key_id"
                  "secretAccessKey" "secret_access_key"
                )
              ) -}}
      {{- /* # floci-divergence: Floci emulates team storage handoff credentials via local secret store with AWS auth. */ -}}
      {{- else if eq $provider "floci" -}}
        {{- $secretStoreAuth := dict "aws" (dict "region" (default "us-east-1" (index $annotations "aws-region"))) -}}
        {{- $handoff = dict
              "provider" "floci"
              "recordName" (printf "s3-team-%s" $team)
              "secretStore" (dict
                "name" "team-s3"
                "serviceAccount" "team-s3"
                "auth" $secretStoreAuth
              )
              "externalSecret" (dict
                "name" "team-s3"
                "targetSecretName" "team-s3"
                "remoteProperties" (dict
                  "accessKeyId" "access_key_id"
                  "secretAccessKey" "secret_access_key"
                )
              ) -}}
      {{- else if eq $provider "gcp" -}}
        {{- if not $gcpProjectId -}}
          {{- fail (printf "registered cluster %q on gcp requires gcp-project-id annotation or gcpProjectId value" $name) -}}
        {{- end -}}
        {{- $secretStoreAuth := dict "gcp" (dict
              "clusterLocation" $region
              "clusterName" $name
              "projectId" $gcpProjectId
              "serviceAccountEmail" (printf "%s-record-reader@%s.iam.gserviceaccount.com" $team $gcpProjectId)
            ) -}}
        {{- $handoff = dict
              "provider" "gcp"
              "recordName" (printf "s3-team-%s" $team)
              "secretStore" (dict
                "name" "team-s3"
                "serviceAccount" "team-s3"
                "auth" $secretStoreAuth
              )
              "externalSecret" (dict
                "name" "team-s3"
                "targetSecretName" "team-s3"
                "remoteProperties" (dict
                  "accessKeyId" "access_key_id"
                  "secretAccessKey" "secret_access_key"
                )
              ) -}}
      {{- end -}}
      {{- $virtual := trimPrefix "cell-" $name -}}
      {{- $storageContract := dict
            "version" 1
            "gateway" (dict "endpoint" "http://s3-gateway.s3-system.svc")
            "authorizationPrefixes" (dict
              "global" (printf "global/home/%s/" $team)
              "global-meta" "global/meta/"
              "global-scratch" (printf "global/scratch/%s/" $team)
              "home" (printf "home/%s/" $team)
              "meta" "meta/"
              "scratch" (printf "scratch/%s/" $team)
            )
            "mounts" (dict
              "global" (dict "bucket" "global" "keyPrefix" (printf "home/%s/" $team) "remote" "global")
              "global-meta" (dict "bucket" "global" "keyPrefix" "meta/" "remote" "global-meta")
              "global-scratch" (dict "bucket" "global" "keyPrefix" (printf "scratch/%s/" $team) "remote" "global-scratch")
              "home" (dict "bucket" $virtual "keyPrefix" (printf "home/%s/" $team) "remote" "home")
              "meta" (dict "bucket" $virtual "keyPrefix" "meta/" "remote" "meta")
              "scratch" (dict "bucket" $virtual "keyPrefix" (printf "scratch/%s/" $team) "remote" "scratch")
            ) -}}
      {{- $_ := set $handoff "storageContract" $storageContract -}}
      {{- $gatewayIdentities := dict -}}
      {{- if eq $provider "aws" -}}
        {{- if not $accountID -}}
          {{- fail (printf "registered cluster %q on aws requires aws-account-id annotation or awsAccountId value" $name) -}}
        {{- end -}}
        {{- $_ := set $gatewayIdentities $team (dict
              "serviceAccount" (printf "s3-gateway-%s" $team)
              "roleArn" (printf "arn:aws:iam::%s:role/%s-s3-gateway-%s" $accountID $name $team)
            ) -}}
      {{- else if eq $provider "gcp" -}}
        {{- if not $gcpProjectId -}}
          {{- fail (printf "registered cluster %q on gcp requires gcp-project-id annotation or gcpProjectId value" $name) -}}
        {{- end -}}
        {{- $_ := set $gatewayIdentities $team (dict
              "serviceAccount" (printf "s3-gateway-%s" $team)
              "serviceAccountEmail" (printf "%s-gateway@%s.iam.gserviceaccount.com" $team $gcpProjectId)
            ) -}}
      {{- end -}}
      {{- $_ := set $cluster "storage" (dict
            "gateway" (dict "config" $gatewayConfig)
            "teams" (dict
              "gatewayIdentities" $gatewayIdentities
              "handoffs" (dict $team $handoff)
            )
          ) -}}
    {{- end -}}
  {{- end -}}
  {{- $registered = append $registered $cluster -}}
{{- end -}}
{{- $registered | toJson -}}
{{- end -}}

{{- define "fleet-team.projectName" -}}
{{- printf "apps-%s" (required "team.slug is required" .Values.team.slug) -}}
{{- end -}}

{{- define "fleet-team.namespace" -}}
{{- printf "team-%s" (required "team.slug is required" .Values.team.slug) -}}
{{- end -}}

{{- define "fleet-team.group" -}}
{{- printf "cluster:group:team:%s" (required "team.slug is required" .Values.team.slug) -}}
{{- end -}}

{{- define "fleet-team.groupDev" -}}
{{- printf "cluster:group:team:%s:dev" (required "team.slug is required" .Values.team.slug) -}}
{{- end -}}

{{- define "fleet-team.groupOps" -}}
{{- printf "cluster:group:team:%s:ops" (required "team.slug is required" .Values.team.slug) -}}
{{- end -}}

{{- define "fleet-team.resources" -}}
{{- $resources := .Values.resourcePacks.base -}}
{{- range $pack := .Values.enabledPacks -}}
{{- $contribution := required (printf "resource pack %q has no contribution" $pack) (index $.Values.resourcePacks $pack) -}}
{{- $resources = concat $resources $contribution -}}
{{- end -}}
{{- $resources | toJson -}}
{{- end -}}

{{- define "fleet-team.projects" -}}
{{- $resolved := list -}}
{{- range $claim := .Values.team.projects -}}
{{- $match := dict -}}
{{- range $project := $.Values.projectIndex.projects -}}
{{- if eq $project.name $claim -}}
{{- $match = $project -}}
{{- end -}}
{{- end -}}
{{- if not $match.name -}}
{{- fail (printf "team %q claims project %q missing from project-index.json" $.Values.team.slug $claim) -}}
{{- end -}}
{{- if ne $match.team $.Values.team.slug -}}
{{- fail (printf "project-index.json assigns project %q to team %q, not %q" $claim $match.team $.Values.team.slug) -}}
{{- end -}}
{{- $resolved = append $resolved $match -}}
{{- end -}}
{{- $resolved | toJson -}}
{{- end -}}

{{- define "fleet-team.images" -}}
{{- $paths := dict -}}
{{- range $project := include "fleet-team.projects" . | fromJsonArray -}}
{{- $_ := set $paths $project.path true -}}
{{- end -}}
{{- $images := list -}}
{{- range $image := .Values.delivery.images -}}
{{- if hasKey $paths $image.package -}}
{{- $images = append $images $image -}}
{{- end -}}
{{- end -}}
{{- $images | toJson -}}
{{- end -}}

{{- define "fleet-team.projectImages" -}}
{{- $root := index . "root" -}}
{{- $path := index . "path" -}}
{{- $images := list -}}
{{- range $image := $root.Values.delivery.images -}}
{{- if eq $image.package $path -}}
{{- $images = append $images $image -}}
{{- end -}}
{{- end -}}
{{- $images | toJson -}}
{{- end -}}

{{- define "fleet-team.deploymentRepositories" -}}
{{- $cell := index . "cell" -}}
{{- $images := index . "images" -}}
{{- $all := required (printf "cell %q requires delivery.deploymentRepositories" $cell.name) $cell.delivery.deploymentRepositories -}}
{{- $resolved := dict -}}
{{- range $image := $images -}}
{{- if not (hasKey $all $image.package) -}}
{{- fail (printf "cell %q deployment repositories require image package %q" $cell.name $image.package) -}}
{{- end -}}
{{- $_ := set $resolved $image.package (index $all $image.package) -}}
{{- end -}}
{{- $resolved | toJson -}}
{{- end -}}

{{- define "fleet-team.deployments" -}}
{{- $deployments := list -}}
{{- range $project := include "fleet-team.projects" . | fromJsonArray -}}
{{- range $deployment := $project.deployments -}}
{{- $stage := dig "stage" "" $deployment -}}
{{- $promotion := dig "promotion" "" $deployment -}}
{{- $source := dig "source" (dict) $deployment -}}
{{- $suffix := $project.name -}}
{{- if $stage -}}
{{- $suffix = printf "%s_%s" $suffix $stage -}}
{{- end -}}
{{- $deployments = append $deployments (dict
      "applicationSuffix" (printf "%s%s" ($project.name | replace "_" "-") (ternary (printf "-%s" ($stage | replace "_" "-")) "" (ne $stage "")))
      "delivery" $project.delivery
      "namespaceSuffix" $suffix
      "path" $deployment.path
      "promotion" $promotion
      "projectName" $project.name
      "projectPath" $project.path
      "source" $source
      "stage" $stage) -}}
{{- end -}}
{{- end -}}
{{- $deployments | toJson -}}
{{- end -}}

{{- define "fleet-team.namespaceSuffixes" -}}
{{- list "workloads" | toJson -}}
{{- end -}}

{{- define "fleet-team.torchCompileCacheEnabled" -}}
{{- $root := index . "root" -}}
{{- $suffix := index . "suffix" -}}
{{- $hasTorchProject := false -}}
{{- range $deployment := include "fleet-team.deployments" $root | fromJsonArray -}}
{{- if has $deployment.projectName (list "ray_serve" "ray_train") -}}
{{- $hasTorchProject = true -}}
{{- end -}}
{{- end -}}
{{- and (eq $suffix "workloads") $hasTorchProject -}}
{{- end -}}

{{- define "fleet-team.selectedCells" -}}
{{- $root := index . "root" -}}
{{- $selector := $root.Values.team.cells -}}
{{- $matchLabels := default (dict) $selector.matchLabels -}}
{{- $expressions := concat (default (list) $selector.matchExpressions) (default (list) (index . "extraExpressions")) -}}
{{- $selected := list -}}
{{- range $cell := include "fleet-team.registeredClusters" $root | fromJsonArray -}}
{{- $matches := eq $cell.role "cell" -}}
{{- $selectorValues := dict "cloud" $cell.cloud "name" $cell.name "provider" $cell.provider "region" $cell.region "role" $cell.role -}}
{{- range $key, $value := $matchLabels -}}
{{- if or (not (hasKey $selectorValues $key)) (ne (index $selectorValues $key) $value) -}}
{{- $matches = false -}}
{{- end -}}
{{- end -}}
{{- range $expression := $expressions -}}
{{- $hasLabel := hasKey $selectorValues $expression.key -}}
{{- if eq $expression.operator "In" -}}
{{- if not $hasLabel -}}
{{- $matches = false -}}
{{- else if not (has (index $selectorValues $expression.key) $expression.values) -}}
{{- $matches = false -}}
{{- end -}}
{{- else if eq $expression.operator "NotIn" -}}
{{- if $hasLabel -}}
{{- if has (index $selectorValues $expression.key) $expression.values -}}
{{- $matches = false -}}
{{- end -}}
{{- end -}}
{{- else if eq $expression.operator "Exists" -}}
{{- if not $hasLabel -}}
{{- $matches = false -}}
{{- end -}}
{{- else if eq $expression.operator "DoesNotExist" -}}
{{- if $hasLabel -}}
{{- $matches = false -}}
{{- end -}}
{{- else -}}
{{- fail (printf "team.cells uses unsupported selector operator %q" $expression.operator) -}}
{{- end -}}
{{- end -}}
{{- if $matches -}}
{{- $selected = append $selected $cell -}}
{{- end -}}
{{- end -}}
{{- $selected | toJson -}}
{{- end -}}

{{- define "fleet-team.storageAuthAdapter" -}}
{{- if and (hasKey . "secretStore") .secretStore (hasKey .secretStore "auth") .secretStore.auth -}}
{{- $auth := .secretStore.auth -}}
{{- $keys := keys $auth | sortAlpha -}}
{{- if ne (len $keys) 1 -}}
{{- fail "storage projection auth must select exactly one adapter" -}}
{{- end -}}
{{- $adapter := first $keys -}}
{{- if not (has $adapter (list "aws" "gcp" "kubernetes")) -}}
{{- fail (printf "storage projection auth adapter %q is unsupported" $adapter) -}}
{{- end -}}
{{- $adapter -}}
{{- else if eq .provider "aws" -}}
aws
{{- else -}}
{{- fail "storage projection requires secretStore.auth" -}}
{{- end -}}
{{- end -}}

{{- define "fleet-team.storageHandoff" -}}
{{- $root := index . "root" -}}
{{- $cell := index . "cell" -}}
{{- $storage := required (printf "cell %q requires storage" $cell.name) $cell.storage -}}
{{- $teams := required (printf "cell %q requires storage.teams" $cell.name) $storage.teams -}}
{{- $handoffs := required (printf "cell %q requires storage.teams.handoffs" $cell.name) $teams.handoffs -}}
{{- if not (hasKey $handoffs $root.Values.team.slug) -}}
{{- fail (printf "cell %q lacks storage handoff for team %q" $cell.name $root.Values.team.slug) -}}
{{- end -}}
{{- $handoff := index $handoffs $root.Values.team.slug -}}
{{- $adapter := include "fleet-team.storageAuthAdapter" $handoff -}}
{{- /* floci-divergence: Floci emulates team storage handoff credentials via AWS auth. */ -}}
{{- if eq $handoff.provider "floci" -}}
{{- if ne $adapter "aws" -}}
{{- fail (printf "team storage handoff provider %q requires AWS auth" $handoff.provider) -}}
{{- end -}}
{{- else if ne $adapter $handoff.provider -}}
{{- fail (printf "team storage handoff provider %q requires matching auth, found %q" $handoff.provider $adapter) -}}
{{- end -}}
{{- $handoff | toJson -}}
{{- end -}}

{{- define "fleet-team.syncPolicyCommon" -}}
retry:
  limit: 5
  backoff:
    duration: 10s
    factor: 2
    maxDuration: 3m
syncOptions:
  # keep-sorted start
  - ApplyOutOfSyncOnly=true
  - FailOnSharedResource=true
  - PruneLast=true
  - RespectIgnoreDifferences=true
  - ServerSideApply=true
  # keep-sorted end
{{- end -}}

{{- define "fleet-team.applicationSyncPolicy" -}}
automated:
  enabled: true
  allowEmpty: false
  prune: true
  selfHeal: true
{{ include "fleet-team.syncPolicyCommon" . -}}
{{- end -}}

{{- define "fleet-team.promotedApplicationSyncPolicy" -}}
automated:
  enabled: false
{{ include "fleet-team.syncPolicyCommon" . -}}
{{- end -}}

{{- define "fleet-team.validate" -}}
{{- $slug := required "team.slug is required" .Values.team.slug -}}
{{- if ne (int .Values.projectIndex.version) 3 -}}
{{- fail "projectIndex.version must be 3" -}}
{{- end -}}
{{- $_ := include "fleet-team.projects" . | fromJsonArray -}}
{{- $_ := include "fleet-team.images" . | fromJsonArray -}}
{{- $_ := include "fleet-team.deployments" . | fromJsonArray -}}
{{- if ne .Values.team.promotion "automatic" -}}
{{- fail (printf "team %q uses promotion mode %q; only the automatic team policy is implemented" $slug .Values.team.promotion) -}}
{{- end -}}
{{- $resources := include "fleet-team.resources" . | fromJsonArray -}}
{{- $projectKinds := dict -}}
{{- $roleResources := dict -}}
{{- range $resource := $resources -}}
{{- $projectKind := printf "%s/%s" $resource.apiGroup $resource.kind -}}
{{- $roleResource := printf "%s/%s" $resource.apiGroup $resource.resource -}}
{{- if hasKey $roleResources $roleResource -}}
{{- fail (printf "enabled resource packs duplicate Role resource %q" $roleResource) -}}
{{- end -}}
{{- $_ := set $roleResources $roleResource true -}}
{{- if $resource.project -}}
{{- if hasKey $projectKinds $projectKind -}}
{{- fail (printf "enabled resource packs duplicate AppProject kind %q" $projectKind) -}}
{{- end -}}
{{- $_ := set $projectKinds $projectKind true -}}
{{- end -}}
{{- if and (eq $resource.apiGroup "gateway.networking.k8s.io") (eq $resource.kind "HTTPRoute") -}}
{{- fail "HTTPRoute is managed and cannot enter the team resource contract" -}}
{{- end -}}
{{- end -}}
{{- end -}}

