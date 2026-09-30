{{- /* Provides shared validation, resource-pack composition, and selectors for one team. */ -}}

{{- define "fleet-team.registeredClusters" -}}
{{- range $cluster := .Values.clusterRegistry.clusters -}}
{{- if not $cluster.provider -}}
{{- fail (printf "clusterRegistry cluster %q requires a provider" $cluster.name) -}}
{{- end -}}
{{- if and (eq $cluster.provider "aws") (not $cluster.region) -}}
{{- fail (printf "clusterRegistry cluster %q on aws requires region" $cluster.name) -}}
{{- end -}}
{{- end -}}
{{- if hasKey .Values "registeredClusters" -}}
{{- $configured := dict -}}
{{- range $cluster := .Values.clusterRegistry.clusters -}}
{{- $_ := set $configured $cluster.name $cluster -}}
{{- end -}}
{{- $registered := list -}}
{{- range $registration := .Values.registeredClusters -}}
{{- $labels := default dict (index $registration "labels") -}}
{{- $annotations := default dict (index $registration "annotations") -}}
{{- $cluster := dict -}}
{{- if hasKey $configured $registration.name -}}
{{- $cluster = deepCopy (index $configured $registration.name) -}}
{{- if index $annotations "aws-region" -}}
{{- $_ := set $cluster "region" (index $annotations "aws-region") -}}
{{- end -}}
{{- else if eq (index $labels "role") "ctrl" -}}
{{- $role := index $labels "role" -}}
{{- $provider := index $labels "provider" -}}
{{- if not $provider -}}
{{- fail (printf "registered ctrl cluster %q requires a provider" $registration.name) -}}
{{- end -}}
{{- $region := index $annotations "aws-region" -}}
{{- if and (eq $provider "aws") (not $region) -}}
{{- fail (printf "registered ctrl cluster %q on aws requires aws-region" $registration.name) -}}
{{- end -}}
{{- $cluster = dict "name" $registration.name "server" $registration.server "role" $role "provider" $provider "cloud" $provider "region" $region -}}
{{- else -}}
{{- fail (printf "registered cluster %q requires a clusterRegistry entry" $registration.name) -}}
{{- end -}}
{{- if eq (default "" $cluster.role) "ctrl" -}}
{{- if not $cluster.provider -}}
{{- fail (printf "registered ctrl cluster %q requires a provider" $registration.name) -}}
{{- end -}}
{{- $region := or $cluster.region (index $annotations "aws-region") -}}
{{- if and (eq $cluster.provider "aws") (not $region) -}}
{{- fail (printf "registered ctrl cluster %q on aws requires aws-region" $registration.name) -}}
{{- end -}}
{{- end -}}
{{- $_ := set $cluster "server" $registration.server -}}
{{- $_ := set $cluster "labels" $labels -}}
{{- $accountID := or (index $annotations "aws-account-id") (index $.Values "awsAccountId") -}}
{{- $ecrRegistry := or (index $annotations "ecr-registry") (index $.Values "ecrRegistry") -}}
{{- /* floci-divergence: Local nodes pull from the workspace OCI registry, not the ECR API endpoint supplied in cluster registration. */ -}}
{{- if and $ecrRegistry (ne $cluster.provider "floci") -}}
  {{- if hasKey $cluster "delivery" -}}
    {{- if hasKey $cluster.delivery "deploymentRepositories" -}}
      {{- $deploymentRepos := dict -}}
      {{- range $pkg, $repo := $cluster.delivery.deploymentRepositories -}}
        {{- $_ := set $deploymentRepos $pkg (printf "%s/%s" $ecrRegistry $pkg) -}}
      {{- end -}}
      {{- $_ := set $cluster.delivery "deploymentRepositories" $deploymentRepos -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- if $accountID -}}
  {{- if hasKey $cluster "coderProvisioner" -}}
    {{- if and $cluster.coderProvisioner.identity (hasKey $cluster.coderProvisioner.identity "roleArn") -}}
      {{- $_ := set $cluster.coderProvisioner.identity "roleArn" (regexReplaceAll `arn:aws:iam::[0-9]{12}:` $cluster.coderProvisioner.identity.roleArn (printf "arn:aws:iam::%s:" $accountID)) -}}
    {{- end -}}
  {{- end -}}
  {{- if hasKey $cluster "workloadImagePush" -}}
    {{- if hasKey $cluster.workloadImagePush "roles" -}}
      {{- $pushRoles := dict -}}
      {{- range $k, $v := $cluster.workloadImagePush.roles -}}
        {{- $_ := set $pushRoles $k (regexReplaceAll `arn:aws:iam::[0-9]{12}:` $v (printf "arn:aws:iam::%s:" $accountID)) -}}
      {{- end -}}
      {{- $_ := set $cluster.workloadImagePush "roles" $pushRoles -}}
    {{- end -}}
  {{- end -}}
  {{- if hasKey $cluster "workloadImagePull" -}}
    {{- if hasKey $cluster.workloadImagePull "roles" -}}
      {{- $pullRoles := dict -}}
      {{- range $k, $v := $cluster.workloadImagePull.roles -}}
        {{- $_ := set $pullRoles $k (regexReplaceAll `arn:aws:iam::[0-9]{12}:` $v (printf "arn:aws:iam::%s:" $accountID)) -}}
      {{- end -}}
      {{- $_ := set $cluster.workloadImagePull "roles" $pullRoles -}}
    {{- end -}}
  {{- end -}}
  {{- if hasKey $cluster "storage" -}}
    {{- if and $cluster.storage (hasKey $cluster.storage "teams") -}}
      {{- if hasKey $cluster.storage.teams "gatewayIdentities" -}}
        {{- range $teamName, $ident := $cluster.storage.teams.gatewayIdentities -}}
          {{- if hasKey $ident "roleArn" -}}
            {{- $_ := set $ident "roleArn" (regexReplaceAll `arn:aws:iam::[0-9]{12}:` $ident.roleArn (printf "arn:aws:iam::%s:" $accountID)) -}}
          {{- end -}}
        {{- end -}}
      {{- end -}}
      {{- if hasKey $cluster.storage.teams "handoffs" -}}
        {{- range $teamName, $handoff := $cluster.storage.teams.handoffs -}}
          {{- if and (hasKey $handoff "secretStore") $handoff.secretStore (hasKey $handoff.secretStore "auth") -}}
            {{- if and (hasKey $handoff.secretStore.auth "aws") (hasKey $handoff.secretStore.auth.aws "roleArn") -}}
              {{- $_ := set $handoff.secretStore.auth.aws "roleArn" (regexReplaceAll `arn:aws:iam::[0-9]{12}:` $handoff.secretStore.auth.aws.roleArn (printf "arn:aws:iam::%s:" $accountID)) -}}
            {{- end -}}
          {{- end -}}
        {{- end -}}
      {{- end -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- $gcpProjectNumber := or (index $annotations "gcp-project-number") (index $.Values "gcpProjectNumber") -}}
{{- if $gcpProjectNumber -}}
  {{- if hasKey $cluster "coderProvisioner" -}}
    {{- if and $cluster.coderProvisioner.federation (hasKey $cluster.coderProvisioner.federation "tokenAudience") -}}
      {{- $_ := set $cluster.coderProvisioner.federation "tokenAudience" (regexReplaceAll `projects/[0-9]+/locations/` $cluster.coderProvisioner.federation.tokenAudience (printf "projects/%v/locations/" $gcpProjectNumber)) -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- $gcpProjectId := or (index $annotations "gcp-project-id") (index $.Values "gcpProjectId") -}}
{{- if $gcpProjectId -}}
  {{- if hasKey $cluster "coderProvisioner" -}}
    {{- if and (hasKey $cluster.coderProvisioner "identity") $cluster.coderProvisioner.identity -}}
      {{- if hasKey $cluster.coderProvisioner.identity "serviceAccount" -}}
        {{- $_ := set $cluster.coderProvisioner.identity "serviceAccount" (regexReplaceAll `@(?:configured-by-applicationset|[a-z0-9-]+)\.iam\.gserviceaccount\.com` $cluster.coderProvisioner.identity.serviceAccount (printf "@%s.iam.gserviceaccount.com" $gcpProjectId)) -}}
      {{- end -}}
      {{- if and (hasKey $cluster.coderProvisioner.identity "kubernetesSubject") (hasKey $cluster.coderProvisioner.identity.kubernetesSubject "name") -}}
        {{- $_ := set $cluster.coderProvisioner.identity.kubernetesSubject "name" (regexReplaceAll `@(?:configured-by-applicationset|[a-z0-9-]+)\.iam\.gserviceaccount\.com` $cluster.coderProvisioner.identity.kubernetesSubject.name (printf "@%s.iam.gserviceaccount.com" $gcpProjectId)) -}}
      {{- end -}}
    {{- end -}}
  {{- end -}}
  {{- if hasKey $cluster "providerConfig" -}}
    {{- if and (hasKey $cluster.providerConfig "gcp") $cluster.providerConfig.gcp -}}
      {{- $_ := set $cluster.providerConfig.gcp "projectID" $gcpProjectId -}}
    {{- end -}}
  {{- end -}}
  {{- if hasKey $cluster "storage" -}}
    {{- if and $cluster.storage (hasKey $cluster.storage "teams") -}}
      {{- if hasKey $cluster.storage.teams "gatewayIdentities" -}}
        {{- range $teamName, $ident := $cluster.storage.teams.gatewayIdentities -}}
          {{- if hasKey $ident "serviceAccountEmail" -}}
            {{- $_ := set $ident "serviceAccountEmail" (regexReplaceAll `@(?:configured-by-applicationset|[a-z0-9-]+)\.iam\.gserviceaccount\.com` $ident.serviceAccountEmail (printf "@%s.iam.gserviceaccount.com" $gcpProjectId)) -}}
          {{- end -}}
        {{- end -}}
      {{- end -}}
      {{- if hasKey $cluster.storage.teams "handoffs" -}}
        {{- range $teamName, $handoff := $cluster.storage.teams.handoffs -}}
          {{- if and (hasKey $handoff "secretStore") $handoff.secretStore (hasKey $handoff.secretStore "auth") -}}
            {{- if and (hasKey $handoff.secretStore.auth "gcp") $handoff.secretStore.auth.gcp -}}
              {{- $_ := set $handoff.secretStore.auth.gcp "projectId" $gcpProjectId -}}
              {{- if hasKey $handoff.secretStore.auth.gcp "serviceAccountEmail" -}}
                {{- $_ := set $handoff.secretStore.auth.gcp "serviceAccountEmail" (regexReplaceAll `@(?:configured-by-applicationset|[a-z0-9-]+)\.iam\.gserviceaccount\.com` $handoff.secretStore.auth.gcp.serviceAccountEmail (printf "@%s.iam.gserviceaccount.com" $gcpProjectId)) -}}
              {{- end -}}
            {{- end -}}
          {{- end -}}
        {{- end -}}
      {{- end -}}
    {{- end -}}
  {{- end -}}
  {{- if hasKey $cluster "delivery" -}}
    {{- if hasKey $cluster.delivery "serviceAccount" -}}
      {{- $_ := set $cluster.delivery "serviceAccount" (regexReplaceAll `@(?:configured-by-applicationset|[a-z0-9-]+)\.iam\.gserviceaccount\.com` $cluster.delivery.serviceAccount (printf "@%s.iam.gserviceaccount.com" $gcpProjectId)) -}}
    {{- end -}}
    {{- if hasKey $cluster.delivery "serviceAccountEmail" -}}
      {{- $_ := set $cluster.delivery "serviceAccountEmail" (regexReplaceAll `@(?:configured-by-applicationset|[a-z0-9-]+)\.iam\.gserviceaccount\.com` $cluster.delivery.serviceAccountEmail (printf "@%s.iam.gserviceaccount.com" $gcpProjectId)) -}}
    {{- end -}}
    {{- if and (hasKey $cluster.delivery "identity") $cluster.delivery.identity -}}
      {{- if hasKey $cluster.delivery.identity "serviceAccount" -}}
        {{- $_ := set $cluster.delivery.identity "serviceAccount" (regexReplaceAll `@(?:configured-by-applicationset|[a-z0-9-]+)\.iam\.gserviceaccount\.com` $cluster.delivery.identity.serviceAccount (printf "@%s.iam.gserviceaccount.com" $gcpProjectId)) -}}
      {{- end -}}
      {{- if hasKey $cluster.delivery.identity "serviceAccountEmail" -}}
        {{- $_ := set $cluster.delivery.identity "serviceAccountEmail" (regexReplaceAll `@(?:configured-by-applicationset|[a-z0-9-]+)\.iam\.gserviceaccount\.com` $cluster.delivery.identity.serviceAccountEmail (printf "@%s.iam.gserviceaccount.com" $gcpProjectId)) -}}
      {{- end -}}
      {{- if and (hasKey $cluster.delivery.identity "kubernetesSubject") (hasKey $cluster.delivery.identity.kubernetesSubject "name") -}}
        {{- $_ := set $cluster.delivery.identity.kubernetesSubject "name" (regexReplaceAll `@(?:configured-by-applicationset|[a-z0-9-]+)\.iam\.gserviceaccount\.com` $cluster.delivery.identity.kubernetesSubject.name (printf "@%s.iam.gserviceaccount.com" $gcpProjectId)) -}}
      {{- end -}}
    {{- end -}}
    {{- if hasKey $cluster.delivery "deploymentRepositories" -}}
      {{- $deliveryRepos := dict -}}
      {{- range $k, $v := $cluster.delivery.deploymentRepositories -}}
        {{- if kindIs "string" $v -}}
          {{- $_ := set $deliveryRepos $k (regexReplaceAll `@(?:configured-by-applicationset|[a-z0-9-]+)\.iam\.gserviceaccount\.com` $v (printf "@%s.iam.gserviceaccount.com" $gcpProjectId)) -}}
        {{- else -}}
          {{- $_ := set $deliveryRepos $k $v -}}
        {{- end -}}
      {{- end -}}
      {{- $_ := set $cluster.delivery "deploymentRepositories" $deliveryRepos -}}
    {{- end -}}
    {{- if hasKey $cluster.delivery "repositories" -}}
      {{- $deliveryRepos := dict -}}
      {{- range $k, $v := $cluster.delivery.repositories -}}
        {{- if kindIs "string" $v -}}
          {{- $_ := set $deliveryRepos $k (regexReplaceAll `@(?:configured-by-applicationset|[a-z0-9-]+)\.iam\.gserviceaccount\.com` $v (printf "@%s.iam.gserviceaccount.com" $gcpProjectId)) -}}
        {{- else -}}
          {{- $_ := set $deliveryRepos $k $v -}}
        {{- end -}}
      {{- end -}}
      {{- $_ := set $cluster.delivery "repositories" $deliveryRepos -}}
    {{- end -}}
  {{- end -}}
  {{- if hasKey $cluster "headscaleRegistrar" -}}
    {{- if kindIs "string" $cluster.headscaleRegistrar -}}
      {{- $_ := set $cluster "headscaleRegistrar" (regexReplaceAll `@(?:configured-by-applicationset|[a-z0-9-]+)\.iam\.gserviceaccount\.com` $cluster.headscaleRegistrar (printf "@%s.iam.gserviceaccount.com" $gcpProjectId)) -}}
    {{- else if and $cluster.headscaleRegistrar (kindIs "map" $cluster.headscaleRegistrar) -}}
      {{- range $k, $v := $cluster.headscaleRegistrar -}}
        {{- if kindIs "string" $v -}}
          {{- $_ := set $cluster.headscaleRegistrar $k (regexReplaceAll `@(?:configured-by-applicationset|[a-z0-9-]+)\.iam\.gserviceaccount\.com` $v (printf "@%s.iam.gserviceaccount.com" $gcpProjectId)) -}}
        {{- else if and $v (kindIs "map" $v) -}}
          {{- $subMap := dict -}}
          {{- range $subK, $subV := $v -}}
            {{- if kindIs "string" $subV -}}
              {{- $_ := set $subMap $subK (regexReplaceAll `@(?:configured-by-applicationset|[a-z0-9-]+)\.iam\.gserviceaccount\.com` $subV (printf "@%s.iam.gserviceaccount.com" $gcpProjectId)) -}}
            {{- else -}}
              {{- $_ := set $subMap $subK $subV -}}
            {{- end -}}
          {{- end -}}
          {{- $_ := set $cluster.headscaleRegistrar $k $subMap -}}
        {{- end -}}
      {{- end -}}
    {{- end -}}
  {{- end -}}
  {{- if hasKey $cluster "workloadImagePush" -}}
    {{- if hasKey $cluster.workloadImagePush "roles" -}}
      {{- if kindIs "map" $cluster.workloadImagePush.roles -}}
        {{- $pushRoles := dict -}}
        {{- range $k, $v := $cluster.workloadImagePush.roles -}}
          {{- if kindIs "string" $v -}}
            {{- $_ := set $pushRoles $k (regexReplaceAll `@(?:configured-by-applicationset|[a-z0-9-]+)\.iam\.gserviceaccount\.com` $v (printf "@%s.iam.gserviceaccount.com" $gcpProjectId)) -}}
          {{- else -}}
            {{- $_ := set $pushRoles $k $v -}}
          {{- end -}}
        {{- end -}}
        {{- $_ := set $cluster.workloadImagePush "roles" $pushRoles -}}
      {{- else if kindIs "string" $cluster.workloadImagePush.roles -}}
        {{- $_ := set $cluster.workloadImagePush "roles" (regexReplaceAll `@(?:configured-by-applicationset|[a-z0-9-]+)\.iam\.gserviceaccount\.com` $cluster.workloadImagePush.roles (printf "@%s.iam.gserviceaccount.com" $gcpProjectId)) -}}
      {{- end -}}
    {{- end -}}
    {{- if hasKey $cluster.workloadImagePush "accounts" -}}
      {{- if kindIs "map" $cluster.workloadImagePush.accounts -}}
        {{- $pushAccounts := dict -}}
        {{- range $k, $v := $cluster.workloadImagePush.accounts -}}
          {{- if kindIs "string" $v -}}
            {{- $_ := set $pushAccounts $k (regexReplaceAll `@(?:configured-by-applicationset|[a-z0-9-]+)\.iam\.gserviceaccount\.com` $v (printf "@%s.iam.gserviceaccount.com" $gcpProjectId)) -}}
          {{- else -}}
            {{- $_ := set $pushAccounts $k $v -}}
          {{- end -}}
        {{- end -}}
        {{- $_ := set $cluster.workloadImagePush "accounts" $pushAccounts -}}
      {{- else if kindIs "string" $cluster.workloadImagePush.accounts -}}
        {{- $_ := set $cluster.workloadImagePush "accounts" (regexReplaceAll `@(?:configured-by-applicationset|[a-z0-9-]+)\.iam\.gserviceaccount\.com` $cluster.workloadImagePush.accounts (printf "@%s.iam.gserviceaccount.com" $gcpProjectId)) -}}
      {{- end -}}
    {{- end -}}
    {{- if hasKey $cluster.workloadImagePush "serviceAccount" -}}
      {{- if kindIs "string" $cluster.workloadImagePush.serviceAccount -}}
        {{- $_ := set $cluster.workloadImagePush "serviceAccount" (regexReplaceAll `@(?:configured-by-applicationset|[a-z0-9-]+)\.iam\.gserviceaccount\.com` $cluster.workloadImagePush.serviceAccount (printf "@%s.iam.gserviceaccount.com" $gcpProjectId)) -}}
      {{- end -}}
    {{- end -}}
    {{- if hasKey $cluster.workloadImagePush "serviceAccountEmail" -}}
      {{- if kindIs "string" $cluster.workloadImagePush.serviceAccountEmail -}}
        {{- $_ := set $cluster.workloadImagePush "serviceAccountEmail" (regexReplaceAll `@(?:configured-by-applicationset|[a-z0-9-]+)\.iam\.gserviceaccount\.com` $cluster.workloadImagePush.serviceAccountEmail (printf "@%s.iam.gserviceaccount.com" $gcpProjectId)) -}}
      {{- end -}}
    {{- end -}}
  {{- end -}}
  {{- if hasKey $cluster "workloadImagePull" -}}
    {{- if hasKey $cluster.workloadImagePull "roles" -}}
      {{- if kindIs "map" $cluster.workloadImagePull.roles -}}
        {{- $pullRoles := dict -}}
        {{- range $k, $v := $cluster.workloadImagePull.roles -}}
          {{- if kindIs "string" $v -}}
            {{- $_ := set $pullRoles $k (regexReplaceAll `@(?:configured-by-applicationset|[a-z0-9-]+)\.iam\.gserviceaccount\.com` $v (printf "@%s.iam.gserviceaccount.com" $gcpProjectId)) -}}
          {{- else -}}
            {{- $_ := set $pullRoles $k $v -}}
          {{- end -}}
        {{- end -}}
        {{- $_ := set $cluster.workloadImagePull "roles" $pullRoles -}}
      {{- else if kindIs "string" $cluster.workloadImagePull.roles -}}
        {{- $_ := set $cluster.workloadImagePull "roles" (regexReplaceAll `@(?:configured-by-applicationset|[a-z0-9-]+)\.iam\.gserviceaccount\.com` $cluster.workloadImagePull.roles (printf "@%s.iam.gserviceaccount.com" $gcpProjectId)) -}}
      {{- end -}}
    {{- end -}}
    {{- if hasKey $cluster.workloadImagePull "accounts" -}}
      {{- if kindIs "map" $cluster.workloadImagePull.accounts -}}
        {{- $pullAccounts := dict -}}
        {{- range $k, $v := $cluster.workloadImagePull.accounts -}}
          {{- if kindIs "string" $v -}}
            {{- $_ := set $pullAccounts $k (regexReplaceAll `@(?:configured-by-applicationset|[a-z0-9-]+)\.iam\.gserviceaccount\.com` $v (printf "@%s.iam.gserviceaccount.com" $gcpProjectId)) -}}
          {{- else -}}
            {{- $_ := set $pullAccounts $k $v -}}
          {{- end -}}
        {{- end -}}
        {{- $_ := set $cluster.workloadImagePull "accounts" $pullAccounts -}}
      {{- else if kindIs "string" $cluster.workloadImagePull.accounts -}}
        {{- $_ := set $cluster.workloadImagePull "accounts" (regexReplaceAll `@(?:configured-by-applicationset|[a-z0-9-]+)\.iam\.gserviceaccount\.com` $cluster.workloadImagePull.accounts (printf "@%s.iam.gserviceaccount.com" $gcpProjectId)) -}}
      {{- end -}}
    {{- end -}}
    {{- if hasKey $cluster.workloadImagePull "serviceAccount" -}}
      {{- if kindIs "string" $cluster.workloadImagePull.serviceAccount -}}
        {{- $_ := set $cluster.workloadImagePull "serviceAccount" (regexReplaceAll `@(?:configured-by-applicationset|[a-z0-9-]+)\.iam\.gserviceaccount\.com` $cluster.workloadImagePull.serviceAccount (printf "@%s.iam.gserviceaccount.com" $gcpProjectId)) -}}
      {{- end -}}
    {{- end -}}
    {{- if hasKey $cluster.workloadImagePull "serviceAccountEmail" -}}
      {{- if kindIs "string" $cluster.workloadImagePull.serviceAccountEmail -}}
        {{- $_ := set $cluster.workloadImagePull "serviceAccountEmail" (regexReplaceAll `@(?:configured-by-applicationset|[a-z0-9-]+)\.iam\.gserviceaccount\.com` $cluster.workloadImagePull.serviceAccountEmail (printf "@%s.iam.gserviceaccount.com" $gcpProjectId)) -}}
      {{- end -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- $registered = append $registered $cluster -}}
{{- end -}}
{{- $registered | toJson -}}
{{- else -}}
{{- .Values.clusterRegistry.clusters | toJson -}}
{{- end -}}
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
{{- $suffixes := list "workloads" -}}
{{- if eq (include "fleet-team.devWorkspacesTeamEnabled" .) "true" -}}
{{- $suffixes = append $suffixes "workspaces" -}}
{{- end -}}
{{- $suffixes | toJson -}}
{{- end -}}

{{- define "fleet-team.devWorkspacesTeamEnabled" -}}
{{- if hasKey .Values.team "workspaces" -}}
{{- ternary "true" "false" .Values.team.workspaces -}}
{{- else -}}
{{- "true" -}}
{{- end -}}
{{- end -}}

{{- define "fleet-team.devWorkspacesEnabled" -}}
{{- $root := index . "root" -}}
{{- $suffix := index . "suffix" -}}
{{- and (eq (include "fleet-team.devWorkspacesTeamEnabled" $root) "true") (eq $suffix "workspaces") -}}
{{- end -}}

{{- define "fleet-team.workspaceSubmissionsEnabled" -}}
{{- $root := index . "root" -}}
{{- $suffix := index . "suffix" -}}
{{- and (eq (include "fleet-team.devWorkspacesTeamEnabled" $root) "true") (eq $suffix "workloads") -}}
{{- end -}}

{{- define "fleet-team.localWorkspaceOrigin" -}}
{{- $matches := list -}}
{{- range $cluster := include "fleet-team.registeredClusters" . | fromJsonArray -}}
{{- /* floci-divergence: Floci local control planes provide local development registry runtime. */ -}}
{{- if and (eq $cluster.role "ctrl") (eq $cluster.provider "floci") -}}
{{- $matches = append $matches $cluster -}}
{{- end -}}
{{- end -}}
{{- if ne (len $matches) 1 -}}
{{- fail "local dev workspaces require exactly one active Floci control registration" -}}
{{- end -}}
{{- $control := first $matches -}}
{{- $origin := required "local dev workspaces require control localRuntime.originRegistry" $control.localRuntime.originRegistry -}}
{{- $ipv4 := required "local dev workspaces require localRuntime.originRegistry.ipv4" $origin.ipv4 -}}
{{- if not (regexMatch `^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$` $ipv4) -}}
{{- fail "localRuntime.originRegistry.ipv4 must be an IPv4 address" -}}
{{- end -}}
{{- $registry := required "local dev workspaces require localRuntime.originRegistry.workloadRoot" $origin.workloadRoot -}}
{{- if not (regexMatch `^origin-registry:5000/[0-9]{12}/[a-z]{2}(?:-gov)?-[a-z]+-[0-9]+$` $registry) -}}
{{- fail "localRuntime.originRegistry.workloadRoot must use the eAWS split-DNS host, account, and region" -}}
{{- end -}}
{{- dict "cidr" (printf "%s/32" $ipv4) "registry" $registry "region" (last (splitList "/" $registry)) | toJson -}}
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

{{- define "fleet-team.workspaceBackupStoreName" -}}
{{- $namespace := index . "namespace" -}}
{{- $prefix := "org-workspace-backups-" -}}
{{- $candidate := printf "%s%s" $prefix $namespace -}}
{{- if le (len $candidate) 63 -}}
{{- $candidate -}}
{{- else -}}
{{- printf "%s%s-%s" $prefix (trunc 28 $namespace) (sha256sum $namespace | trunc 12) -}}
{{- end -}}
{{- end -}}

{{- define "fleet-team.workspaceBackupReleaseName" -}}
{{- $identity := printf "%s-%s" (required "team.slug is required" (index . "team")) (required "workspace backup suffix is required" (index . "suffix") | replace "_" "-") -}}
{{- $prefix := "workspace-backup-store-" -}}
{{- $candidate := printf "%s%s" $prefix $identity -}}
{{- if le (len $candidate) 53 -}}
{{- $candidate -}}
{{- else -}}
{{- printf "%s%s-%s" $prefix (trunc 19 $identity | trimSuffix "-") (sha256sum $identity | trunc 10) -}}
{{- end -}}
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
{{- $auth := required "storage projection requires secretStore.auth" .secretStore.auth -}}
{{- $keys := keys $auth | sortAlpha -}}
{{- if ne (len $keys) 1 -}}
{{- fail "storage projection auth must select exactly one adapter" -}}
{{- end -}}
{{- $adapter := first $keys -}}
{{- if not (has $adapter (list "aws" "gcp" "kubernetes")) -}}
{{- fail (printf "storage projection auth adapter %q is unsupported" $adapter) -}}
{{- end -}}
{{- $adapter -}}
{{- end -}}

{{- define "fleet-team.buildbuddyAuth" -}}
{{- $cell := index . "cell" -}}
{{- $adapter := $cell.provider -}}
{{- $auth := dict -}}
{{- /* floci-divergence: Floci clusters adapt BuildBuddy auth to AWS emulation. */ -}}
{{- if eq $adapter "floci" -}}
{{- $adapter = "aws" -}}
{{- $auth = dict "aws" (dict "region" (default "us-east-1" $cell.region)) -}}
{{- else if eq $adapter "aws" -}}
{{- $auth = dict "aws" (dict "region" (required (printf "cell %q requires region for BuildBuddy auth" $cell.name) $cell.region)) -}}
{{- else if eq $adapter "gcp" -}}
{{- $gcp := required (printf "cell %q requires providerConfig.gcp for BuildBuddy auth" $cell.name) $cell.providerConfig.gcp -}}
{{- $auth = dict "gcp" (dict "projectId" (required (printf "cell %q requires a GCP project for BuildBuddy auth" $cell.name) $gcp.projectID)) -}}
{{- else -}}
{{- fail (printf "cell %q uses unsupported BuildBuddy auth provider %q" $cell.name $adapter) -}}
{{- end -}}
{{- dict "adapter" $adapter "auth" $auth "recordName" (printf "buildbuddy-auth-%s" $cell.name) | toJson -}}
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

{{- define "fleet-team.applicationSyncPolicy" -}}
automated:
  enabled: true
  allowEmpty: false
  prune: true
  selfHeal: true
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

{{- define "fleet-team.promotedApplicationSyncPolicy" -}}
automated:
  enabled: false
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

{{- define "fleet-team.validate" -}}
{{- $slug := required "team.slug is required" .Values.team.slug -}}
{{- if ne (int .Values.clusterRegistry.version) 1 -}}
{{- fail "clusterRegistry.version must be 1" -}}
{{- end -}}
{{- if ne (int .Values.projectIndex.version) 3 -}}
{{- fail "projectIndex.version must be 3" -}}
{{- end -}}
{{- $_ := include "fleet-team.projects" . | fromJsonArray -}}
{{- $_ := include "fleet-team.images" . | fromJsonArray -}}
{{- $deployments := include "fleet-team.deployments" . | fromJsonArray -}}
{{- if eq (include "fleet-team.devWorkspacesTeamEnabled" .) "true" -}}
{{- range $deployment := $deployments -}}
{{- if eq $deployment.namespaceSuffix "dev" -}}
{{- fail "the reserved dev namespace suffix cannot identify a workload deployment" -}}
{{- end -}}
{{- end -}}
{{- end -}}
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

{{- /* Projects a cell's buildbuddy.io/* cluster labels into the team_namespace devWorkspaces.buildbuddy values; an unset label keeps the chart's default. */ -}}
{{- define "fleet-team.workspaceBuildbuddy" -}}
{{- $values := dict "accessAliasDomain" .accessAliasDomain -}}
{{- $mode := dig "labels" "buildbuddy.io/mode" "" .cell -}}
{{- if $mode -}}
{{- $_ := set $values "mode" $mode -}}
{{- end -}}
{{- $proxy := dig "labels" "buildbuddy.io/enterprise-proxy" "" .cell -}}
{{- if $proxy -}}
{{- $_ := set $values "enterpriseProxy" (eq $proxy "enabled") -}}
{{- end -}}
{{- $executors := dig "labels" "buildbuddy.io/executors" "" .cell -}}
{{- if $executors -}}
{{- $_ := set $values "executors" $executors -}}
{{- end -}}
{{- $values | toJson -}}
{{- end -}}
