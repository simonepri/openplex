#!/bin/sh
# Asserts only authorized Coder provisioners and controller chains can mount protected workspace secrets to defend credentials from direct pod injection.

# shellcheck disable=SC2312
set -eu

busybox_image="${BUSYBOX_IMAGE:?busybox image is required}"
script_directory="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
repository_root="$(git -C "${script_directory}" rev-parse --show-toplevel)"
backup_proxy_image="$(
  yq --exit-status --unwrapScalar \
    '.spec.template.spec.containers[].env[] | select(.name == "CODER_TEMPLATE_WORKSPACE_BACKUP_PROXY_IMAGE") | .value' \
    "${repository_root}/src/infra/argocd/components/coder/kustomize/template-reconciler.k8s.yaml"
)"
namespace=workspace-secret-admission-e2e
provisioner=cluster:test:coder-provisioner
team_user=conformance-user
workspace_id=11111111-1111-4111-8111-111111111111
deployment="coder-${workspace_id}"
runtime_secret="${deployment}-runtime"
buildbuddy_config="${deployment}-buildbuddy"
fixture="${FIXTURE:?fixture path is required}"
controller_message='Protected workspace Secrets may appear only in the exact Coder Deployment and its controller-owned ReplicaSet.'
pod_message='Protected workspace Secrets may appear only in a ReplicaSet-controller-created Coder Pod.'
other_controller_message='Only the Coder Deployment and its ReplicaSet may reference protected workspace Secrets.'
cron_message='CronJobs may not reference protected workspace Secrets.'
runtime_message='Only the scoped Coder provisioner may manage an exact per-workspace runtime Secret.'
buildbuddy_message='BuildBuddy workspace credentials require the exact read-only Coder Bazel configuration projection.'
interactive_message='Interactive access to protected Coder workspace Pods is forbidden.'
temp_dir="$(mktemp -d)"
rendered="${temp_dir}/fixture.yaml"
rbac="${temp_dir}/rbac.yaml"
org_secret="${temp_dir}/org-secret.json"
runtime="${temp_dir}/runtime-secret.json"
buildbuddy_credentials="${temp_dir}/buildbuddy-credentials.json"
buildbuddy_config_map="${temp_dir}/buildbuddy-config-map.json"
buildbuddy_external_secret="${temp_dir}/buildbuddy-external-secret.json"
buildbuddy_secret_store="${temp_dir}/buildbuddy-secret-store.json"
deployment_json="${temp_dir}/deployment.json"
live_replicaset="${temp_dir}/live-replicaset.json"
replicaset_json="${temp_dir}/replicaset.json"
pod_json="${temp_dir}/pod.json"
ordinary_pod="${temp_dir}/ordinary-pod.json"
port_forward_pid=

cleanup() {
  status=$?
  set +e
  if [ -n "${port_forward_pid}" ]; then
    kill "${port_forward_pid}" 2>/dev/null
    wait "${port_forward_pid}" 2>/dev/null
  fi
  kubectl delete deployment "${deployment}" --namespace="${namespace}" \
    --as="${provisioner}" --as-group=cluster:coder-provisioners --ignore-not-found=true --wait=true >/dev/null 2>&1
  for historical in historical-protected-env historical-protected-envfrom \
    historical-protected-imagepull historical-protected-volume; do
    kubectl delete deployment "${historical}" --namespace="${namespace}" \
      --ignore-not-found=true --wait=false >/dev/null 2>&1
  done
  kubectl delete pod ordinary-debug-target --namespace="${namespace}" \
    --as="${team_user}" --ignore-not-found=true --wait=true >/dev/null 2>&1
  kubectl delete workloads --all --namespace="${namespace}" \
    --ignore-not-found=true --wait=true --timeout=60s >/dev/null 2>&1
  kubectl delete localqueue be --namespace="${namespace}" \
    --ignore-not-found=true --wait=false >/dev/null 2>&1
  kubectl delete clusterqueue "${namespace}" \
    --ignore-not-found=true --wait=false >/dev/null 2>&1
  kubectl delete secret "${runtime_secret}" --namespace="${namespace}" \
    --as="${provisioner}" --as-group=cluster:coder-provisioners --ignore-not-found=true >/dev/null 2>&1
  kubectl delete secret workspace-backups --namespace="${namespace}" \
    --ignore-not-found=true >/dev/null 2>&1
  kubectl delete secret workspace-buildbuddy-auth --namespace="${namespace}" \
    --ignore-not-found=true >/dev/null 2>&1
  kubectl delete configmap "${buildbuddy_config}" --namespace="${namespace}" \
    --ignore-not-found=true >/dev/null 2>&1
  kubectl delete rolebinding \
    workspace-secret-admission-conformance \
    workspace-secret-admission-controllers \
    workspace-secret-admission-provisioner \
    --namespace="${namespace}" --ignore-not-found=true >/dev/null 2>&1
  kubectl delete role \
    workspace-secret-admission-conformance \
    workspace-secret-admission-controllers \
    workspace-secret-admission-provisioner \
    --namespace="${namespace}" --ignore-not-found=true >/dev/null 2>&1
  rm -r -- "${temp_dir}"
  exit "${status}"
}

expect_allowed() {
  description="$1"
  shift
  set +e
  output="$("$@" 2>&1)"
  status=$?
  set -e
  if [ "${status}" -ne 0 ]; then
    printf '%s unexpectedly failed:\n%s\n' "${description}" "${output}" >&2
    return 1
  fi
}

expect_denied() {
  description="$1"
  expected="$2"
  shift 2
  set +e
  output="$("$@" 2>&1)"
  status=$?
  set -e
  if [ "${status}" -eq 0 ]; then
    printf '%s unexpectedly succeeded\n' "${description}" >&2
    return 1
  fi
  if ! printf '%s\n' "${output}" | tr -s '[:space:]' ' ' | grep -F "${expected}" >/dev/null; then
    printf '%s failed outside the expected policy:\n%s\n' "${description}" "${output}" >&2
    return 1
  fi
}

expect_connect_denied() {
  description="$1"
  shift
  output_file="${temp_dir}/connect-output"
  set +e
  "$@" >"${output_file}" 2>&1 &
  connect_pid=$!
  elapsed=0
  while kill -0 "${connect_pid}" 2>/dev/null; do
    if [ "${elapsed}" -eq 50 ]; then
      kill "${connect_pid}" 2>/dev/null
      wait "${connect_pid}" 2>/dev/null
      set -e
      printf '%s did not fail closed within five seconds\n' "${description}" >&2
      return 1
    fi
    sleep 0.1
    elapsed=$((elapsed + 1))
  done
  wait "${connect_pid}"
  status=$?
  set -e
  if [ "${status}" -eq 0 ]; then
    printf '%s unexpectedly succeeded\n' "${description}" >&2
    return 1
  fi
  if ! tr -s '[:space:]' ' ' <"${output_file}" | grep -F "${interactive_message}" >/dev/null; then
    printf '%s failed outside the expected policy:\n' "${description}" >&2
    sed -n '1,20p' "${output_file}" >&2
    return 1
  fi
}

require_authorized() {
  verb="$1"
  resource="$2"
  identity="$3"
  if [ "$(kubectl auth can-i "${verb}" "${resource}" --namespace="${namespace}" --as="${identity}")" != yes ]; then
    printf '%s cannot %s %s in the fixture namespace\n' "${identity}" "${verb}" "${resource}" >&2
    exit 1
  fi
}

trap cleanup EXIT HUP INT TERM

awk -v image="${busybox_image}" -v backup_proxy_image="${backup_proxy_image}" '{
  gsub(/[(][$]values[.]busyboxImage[)]/, image)
  gsub(/[(][$]values[.]backupProxyImage[)]/, backup_proxy_image)
  print
}' "${fixture}" >"${rendered}"
yq eval-all 'select(.kind == "Role" or .kind == "RoleBinding")' "${rendered}" >"${rbac}"
yq eval-all --output-format=json \
  'select(.kind == "Secret" and .metadata.name == "workspace-backups")' \
  "${rendered}" >"${org_secret}"
yq eval-all --output-format=json \
  'select(.kind == "Secret" and .metadata.name == "coder-11111111-1111-4111-8111-111111111111-runtime")' \
  "${rendered}" >"${runtime}"
yq eval-all --output-format=json \
  'select(.kind == "Secret" and .metadata.name == "workspace-buildbuddy-auth")' \
  "${rendered}" >"${buildbuddy_credentials}"
yq eval-all --output-format=json \
  'select(.kind == "ConfigMap" and .metadata.name == "coder-11111111-1111-4111-8111-111111111111-buildbuddy")' \
  "${rendered}" >"${buildbuddy_config_map}"
yq eval-all --output-format=json \
  'select(.kind == "ExternalSecret" and .metadata.name == "workspace-buildbuddy-auth")' \
  "${rendered}" >"${buildbuddy_external_secret}"
yq eval-all --output-format=json \
  'select(.kind == "SecretStore" and .metadata.name == "workspace-buildbuddy-auth")' \
  "${rendered}" >"${buildbuddy_secret_store}"
yq eval-all --output-format=json 'select(.kind == "Deployment")' \
  "${rendered}" | jq '
    .metadata.labels += {"availability-class":"be","latency-class":"ls"} |
    .spec.template.metadata.labels += {"availability-class":"be","latency-class":"ls"}
  ' >"${deployment_json}"
kubectl label namespace "${namespace}" app.kubernetes.io/part-of- --overwrite >/dev/null 2>&1 || true
jq --arg name historical-protected-env --arg runtime "${runtime_secret}" '
  .metadata.name = $name |
  .metadata.labels = {
    "app.kubernetes.io/instance":$name,
    "app.kubernetes.io/managed-by":"chainsaw",
    "app.kubernetes.io/name":"workspace-secret-history",
    "availability-class":"be",
    "latency-class":"ls"
  } |
  .spec.replicas = 0 |
  .spec.selector.matchLabels = .metadata.labels |
  .spec.template.metadata.labels = .metadata.labels |
  .spec.template.spec.containers = [(.spec.template.spec.containers[0] |
    .name = "probe" |
    .env = [{name:"PROTECTED",valueFrom:{secretKeyRef:{name:$runtime,key:"coder_agent_token"}}}])] |
  del(.spec.template.spec.initContainers,.spec.template.spec.ephemeralContainers,
    .spec.template.spec.imagePullSecrets,.spec.template.spec.volumes,
    .spec.template.spec.containers[0].volumeMounts)
' "${deployment_json}" >"${temp_dir}/historical-protected-env.json"
jq --arg name historical-protected-envfrom '
  .metadata.name = $name |
  .metadata.labels["app.kubernetes.io/instance"] = $name |
  .spec.selector.matchLabels["app.kubernetes.io/instance"] = $name |
  .spec.template.metadata.labels["app.kubernetes.io/instance"] = $name |
  del(.spec.template.spec.containers[0].env) |
  .spec.template.spec.containers[0].envFrom = [{secretRef:{name:"workspace-backups"}}]
' "${temp_dir}/historical-protected-env.json" >"${temp_dir}/historical-protected-envfrom.json"
jq --arg name historical-protected-volume --arg runtime "${runtime_secret}" '
  .metadata.name = $name |
  .metadata.labels["app.kubernetes.io/instance"] = $name |
  .spec.selector.matchLabels["app.kubernetes.io/instance"] = $name |
  .spec.template.metadata.labels["app.kubernetes.io/instance"] = $name |
  del(.spec.template.spec.containers[0].env) |
  .spec.template.spec.volumes = [{name:"protected",secret:{secretName:$runtime}}]
' "${temp_dir}/historical-protected-env.json" >"${temp_dir}/historical-protected-volume.json"
jq --arg name historical-protected-imagepull '
  .metadata.name = $name |
  .metadata.labels["app.kubernetes.io/instance"] = $name |
  .spec.selector.matchLabels["app.kubernetes.io/instance"] = $name |
  .spec.template.metadata.labels["app.kubernetes.io/instance"] = $name |
  del(.spec.template.spec.containers[0].env) |
  .spec.template.spec.imagePullSecrets = [{name:"workspace-backups"}]
' "${temp_dir}/historical-protected-env.json" >"${temp_dir}/historical-protected-imagepull.json"
for historical in env envfrom imagepull volume; do
  kubectl create --filename="${temp_dir}/historical-protected-${historical}.json" >/dev/null
done
kubectl label namespace "${namespace}" \
  app.kubernetes.io/part-of=team-lane \
  cell=cell-eaws-lh1 \
  cost-center=examples \
  environment=production \
  kueue.x-k8s.io/managed=true \
  pod-security.kubernetes.io/audit=restricted \
  pod-security.kubernetes.io/audit-version=latest \
  pod-security.kubernetes.io/warn=restricted \
  pod-security.kubernetes.io/warn-version=latest \
  team=examples --overwrite >/dev/null
kubectl create --filename=- >/dev/null <<EOF
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata: {name: ${namespace}}
spec:
  namespaceSelector:
    matchLabels: {kubernetes.io/metadata.name: ${namespace}}
  resourceGroups:
    - coveredResources: [cpu, memory]
      flavors:
        - name: on-demand
          resources:
            - {name: cpu, nominalQuota: 100m}
            - {name: memory, nominalQuota: 64Mi}
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata: {name: be, namespace: ${namespace}}
spec: {clusterQueue: ${namespace}}
EOF
kubectl apply --filename="${rbac}" >/dev/null

if [ "$(kubectl auth can-i get secrets --namespace="${namespace}" --as="${team_user}")" != no ]; then
  printf 'team conformance identity unexpectedly has Secret read access\n' >&2
  exit 1
fi
require_authorized create secrets "${provisioner}"
require_authorized create deployments.apps "${provisioner}"
require_authorized create externalsecrets.external-secrets.io "${team_user}"
require_authorized create secretstores.external-secrets.io "${team_user}"
require_authorized create replicasets.apps system:kube-controller-manager
require_authorized create replicasets.apps system:serviceaccount:kube-system:deployment-controller
require_authorized create pods system:kube-controller-manager
require_authorized create pods system:serviceaccount:kube-system:replicaset-controller

jq --exit-status '(.data | keys | sort) == ["access_key_id", "secret_access_key"]' \
  "${org_secret}" >/dev/null
jq --exit-status '
  .type == "Opaque" and
  (.data | keys | sort) == [
    "backup_proxy_access_key",
    "backup_proxy_auth_key",
    "backup_proxy_secret_key",
    "coder_agent_token"
  ]
' "${runtime}" >/dev/null
expect_allowed 'canonical runtime Secret by the scoped provisioner' \
  kubectl create --dry-run=server --output=name --filename="${runtime}" --as="${provisioner}" --as-group=cluster:coder-provisioners
expect_denied 'runtime Secret by a team identity' "${runtime_message}" \
  kubectl create --dry-run=server --output=name --filename="${runtime}" --as="${team_user}"
jq '.data.workspace_backup_password = "cmV0aXJlZC1rZXk="' "${runtime}" >"${temp_dir}/runtime-extra.json"
expect_denied 'runtime Secret with an extra key' "${runtime_message}" \
  kubectl create --dry-run=server --output=name --filename="${temp_dir}/runtime-extra.json" --as="${provisioner}" --as-group=cluster:coder-provisioners
expect_allowed 'canonical BuildBuddy SecretStore' \
  kubectl create --dry-run=server --output=name \
  --filename="${buildbuddy_secret_store}" --as="${team_user}"
expect_allowed 'canonical cell-scoped BuildBuddy ExternalSecret' \
  kubectl create --dry-run=server --output=name \
  --filename="${buildbuddy_external_secret}" --as="${team_user}"
jq '(.spec.data[].remoteRef.key) = "buildbuddy-auth-cell-gcp-euw4"' \
  "${buildbuddy_external_secret}" >"${temp_dir}/buildbuddy-other-cell.json"
expect_denied 'BuildBuddy ExternalSecret for another cell' \
  "Team BuildBuddy credentials must project only the current cell's purpose-separated keys." \
  kubectl create --dry-run=server --output=name \
  --filename="${temp_dir}/buildbuddy-other-cell.json" --as="${team_user}"

for historical in env envfrom imagepull volume; do
  case "${historical}" in
    env) protected_path=/spec/template/spec/containers/0/env ;;
    envfrom) protected_path=/spec/template/spec/containers/0/envFrom ;;
    imagepull) protected_path=/spec/template/spec/imagePullSecrets ;;
    volume) protected_path=/spec/template/spec/volumes ;;
    *) ;;
  esac
  expect_denied "historical ${historical} protected-reference removal" "${controller_message}" \
    kubectl patch deployment "historical-protected-${historical}" --namespace="${namespace}" \
    --as="${team_user}" --dry-run=server --type=json \
    --patch="[{\"op\":\"remove\",\"path\":\"${protected_path}\"}]"
done

expect_allowed 'canonical Coder Deployment by the scoped provisioner' \
  kubectl create --dry-run=server --output=name --filename="${deployment_json}" --as="${provisioner}" --as-group=cluster:coder-provisioners
invalid_backup_proxy_image="${backup_proxy_image%@sha256:*}@sha256:0000000000000000000000000000000000000000000000000000000000000000"
jq --arg image "${invalid_backup_proxy_image}" '
  .spec.template.spec.initContainers[] |=
    if .name == "backup-proxy" then .image = $image else . end
' "${deployment_json}" >"${temp_dir}/deployment-backup-proxy-mismatch.json"
expect_denied 'Coder Deployment with a mismatched backup proxy digest' "${controller_message}" \
  kubectl create --dry-run=server --output=name \
  --filename="${temp_dir}/deployment-backup-proxy-mismatch.json" --as="${provisioner}" --as-group=cluster:coder-provisioners
expect_denied 'canonical Coder Deployment by a team identity' "${controller_message}" \
  kubectl create --dry-run=server --output=name --filename="${deployment_json}" --as="${team_user}"
jq '.spec.template.spec.shareProcessNamespace = true' \
  "${deployment_json}" >"${temp_dir}/deployment-shared-process-namespace.json"
expect_denied 'Coder Deployment with a shared process namespace' "${controller_message}" \
  kubectl create --dry-run=server --output=name \
  --filename="${temp_dir}/deployment-shared-process-namespace.json" --as="${provisioner}" --as-group=cluster:coder-provisioners
for sidecar in tailnet backup-proxy; do
  jq --arg sidecar "${sidecar}" '
    (.spec.template.spec.initContainers[] | select(.name == $sidecar)) |= del(.restartPolicy)
  ' "${deployment_json}" >"${temp_dir}/deployment-${sidecar}-ordinary-init.json"
  expect_denied "${sidecar} without native sidecar lifecycle" "${controller_message}" \
    kubectl create --dry-run=server --output=name \
    --filename="${temp_dir}/deployment-${sidecar}-ordinary-init.json" --as="${provisioner}" --as-group=cluster:coder-provisioners
done
jq --arg runtime "${runtime_secret}" '
  .spec.template.spec.containers[] |=
    if .name == "workspace" then
      .env += [{
        "name":"RCLONE_AUTH_KEY",
        "valueFrom":{"secretKeyRef":{"name":$runtime,"key":"backup_proxy_auth_key"}}
      }]
    else . end
' "${deployment_json}" >"${temp_dir}/deployment-cross-consumer.json"
expect_denied 'combined proxy credential projected into workspace' "${controller_message}" \
  kubectl create --dry-run=server --output=name \
  --filename="${temp_dir}/deployment-cross-consumer.json" --as="${provisioner}" --as-group=cluster:coder-provisioners
jq '
  .spec.template.spec.containers[] |=
    if .name == "workspace" then
      .env += [{
        "name":"RCLONE_CONFIG_BACKEND_ACCESS_KEY_ID",
        "valueFrom":{"secretKeyRef":{"name":"workspace-backups","key":"access_key_id"}}
      }]
    else . end
' "${deployment_json}" >"${temp_dir}/deployment-org-cross-consumer.json"
expect_denied 'organization credential projected into workspace' "${controller_message}" \
  kubectl create --dry-run=server --output=name \
  --filename="${temp_dir}/deployment-org-cross-consumer.json" --as="${provisioner}" --as-group=cluster:coder-provisioners
jq --arg runtime "${runtime_secret}" '
  .spec.template.spec.initContainers[] |=
    if .name == "backup-proxy" then
      .env += [{
        "name":"KOPIA_REPOSITORY_ACCESS_KEY_ID",
        "valueFrom":{"secretKeyRef":{"name":$runtime,"key":"backup_proxy_access_key"}}
      }]
    else . end
' "${deployment_json}" >"${temp_dir}/deployment-local-cross-consumer.json"
expect_denied 'derived loopback credential projected into backup proxy' "${controller_message}" \
  kubectl create --dry-run=server --output=name \
  --filename="${temp_dir}/deployment-local-cross-consumer.json" --as="${provisioner}" --as-group=cluster:coder-provisioners
jq '
  .spec.template.spec.initContainers += [
    (.spec.template.spec.initContainers[] | select(.name == "tailnet") | .name = "helper")
  ]
' "${deployment_json}" >"${temp_dir}/deployment-agent-cross-consumer.json"
expect_denied 'agent token projected into an unapproved helper' "${controller_message}" \
  kubectl create --dry-run=server --output=name \
  --filename="${temp_dir}/deployment-agent-cross-consumer.json" --as="${provisioner}" --as-group=cluster:coder-provisioners

kubectl create --filename="${org_secret}" >/dev/null
kubectl create --filename="${runtime}" --as="${provisioner}" --as-group=cluster:coder-provisioners >/dev/null
kubectl create --filename="${buildbuddy_credentials}" >/dev/null
kubectl create --filename="${buildbuddy_config_map}" >/dev/null
kubectl create --filename="${deployment_json}" --as="${provisioner}" --as-group=cluster:coder-provisioners >/dev/null
kubectl wait replicasets --namespace="${namespace}" \
  --selector="com.coder.workspace.id=${workspace_id}" --for=create --timeout=60s >/dev/null
kubectl get replicasets --namespace="${namespace}" \
  --selector="com.coder.workspace.id=${workspace_id}" --output=json \
  | jq --exit-status '.items | if length == 1 then .[0] else error("expected one workspace ReplicaSet") end' \
    >"${live_replicaset}"

jq --arg name "${deployment}-abcde" '
  {
    apiVersion:"apps/v1",
    kind:"ReplicaSet",
    metadata:{
      name:$name,
      namespace:.metadata.namespace,
      labels:.metadata.labels,
      ownerReferences:.metadata.ownerReferences
    },
    spec:{replicas:1,selector:.spec.selector,template:.spec.template}
  }
' "${live_replicaset}" >"${replicaset_json}"
jq '
  {
    apiVersion:"v1",
    kind:"Pod",
    metadata:{
      name:(.metadata.name + "-probe"),
      namespace:.metadata.namespace,
      labels:.spec.template.metadata.labels,
      ownerReferences:[{
        apiVersion:"apps/v1",
        kind:"ReplicaSet",
        name:.metadata.name,
        uid:.metadata.uid,
        controller:true,
        blockOwnerDeletion:true
      }]
    },
    spec:.spec.template.spec
  }
' "${live_replicaset}" >"${pod_json}"
for controller in \
  system:kube-controller-manager \
  system:serviceaccount:kube-system:deployment-controller; do
  expect_allowed "canonical ReplicaSet by ${controller}" \
    kubectl create --dry-run=server --output=name --filename="${replicaset_json}" --as="${controller}"
done
jq '.spec.template.spec.shareProcessNamespace = true' \
  "${replicaset_json}" >"${temp_dir}/replicaset-shared-process-namespace.json"
expect_denied 'Coder ReplicaSet with a shared process namespace' "${controller_message}" \
  kubectl create --dry-run=server --output=name \
  --filename="${temp_dir}/replicaset-shared-process-namespace.json" \
  --as=system:kube-controller-manager
for controller in \
  system:kube-controller-manager \
  system:serviceaccount:kube-system:replicaset-controller; do
  expect_allowed "canonical Pod by ${controller}" \
    kubectl create --dry-run=server --output=name --filename="${pod_json}" --as="${controller}"
done
jq '.spec.shareProcessNamespace = true' \
  "${pod_json}" >"${temp_dir}/pod-shared-process-namespace.json"
expect_denied 'Coder Pod with a shared process namespace' "${pod_message}" \
  kubectl create --dry-run=server --output=name \
  --filename="${temp_dir}/pod-shared-process-namespace.json" \
  --as=system:kube-controller-manager
expect_denied 'forged ReplicaSet by a team identity' "${controller_message}" \
  kubectl create --dry-run=server --output=name --filename="${replicaset_json}" --as="${team_user}"
expect_denied 'forged Pod by a team identity' "${pod_message}" \
  kubectl create --dry-run=server --output=name --filename="${pod_json}" --as="${team_user}"

jq --null-input --arg image "${busybox_image}" --arg namespace "${namespace}" --arg runtime "${runtime_secret}" '
  {
    apiVersion:"v1",
    kind:"Pod",
    metadata:{name:"raw-protected-secret",namespace:$namespace,
      labels:{"availability-class":"be","latency-class":"ls"}},
    spec:{
      restartPolicy:"Never",
      automountServiceAccountToken:false,
      securityContext:{runAsNonRoot:true,runAsUser:1000,runAsGroup:1000,seccompProfile:{type:"RuntimeDefault"}},
      containers:[{
        name:"probe",
        image:$image,
        command:["sh","-c","true"],
        resources:{requests:{cpu:"1m",memory:"4Mi"},limits:{cpu:"50m",memory:"16Mi"}},
        securityContext:{allowPrivilegeEscalation:false,readOnlyRootFilesystem:true,capabilities:{drop:["ALL"]}},
        env:[{name:"STOLEN",valueFrom:{secretKeyRef:{name:$runtime,key:"coder_agent_token"}}}]
      }]
    }
  }
' >"${temp_dir}/raw-env-pod.json"
expect_denied 'raw Pod agent-token reference' "${pod_message}" \
  kubectl create --dry-run=server --output=name --filename="${temp_dir}/raw-env-pod.json" --as="${team_user}"
jq --arg runtime "${runtime_secret}" '
  del(.spec.containers[0].env) |
  .metadata.name = "raw-secret-volume" |
  .spec.volumes = [{name:"stolen",secret:{secretName:$runtime}}]
' "${temp_dir}/raw-env-pod.json" >"${temp_dir}/raw-volume-pod.json"
expect_denied 'raw Pod Secret volume reference' "${pod_message}" \
  kubectl create --dry-run=server --output=name --filename="${temp_dir}/raw-volume-pod.json" --as="${team_user}"
jq --arg runtime "${runtime_secret}" '
  del(.spec.containers[0].env) |
  .metadata.name = "raw-image-pull-secret" |
  .spec.imagePullSecrets = [{name:$runtime}]
' "${temp_dir}/raw-env-pod.json" >"${temp_dir}/raw-image-pull-pod.json"
expect_denied 'raw Pod imagePullSecret reference' "${pod_message}" \
  kubectl create --dry-run=server --output=name --filename="${temp_dir}/raw-image-pull-pod.json" --as="${team_user}"
jq --arg runtime "${runtime_secret}" '
  del(.spec.containers[0].env) |
  .metadata.name = "raw-legacy-secret-volume" |
  .spec.volumes = [{name:"stolen",cephfs:{monitors:["10.0.0.1:6789"],secretRef:{name:$runtime}}}]
' "${temp_dir}/raw-env-pod.json" >"${temp_dir}/raw-legacy-pod.json"
expect_denied 'raw Pod legacy volume Secret reference' "${pod_message}" \
  kubectl create --dry-run=server --output=name --filename="${temp_dir}/raw-legacy-pod.json" --as="${team_user}"
jq --arg runtime "${runtime_secret}" '
  del(.spec.containers[0].env) |
  .metadata.name = "raw-init-secret" |
  .spec.initContainers = [(.spec.containers[0] | .name = "init" | .env = [{
    name:"STOLEN",valueFrom:{secretKeyRef:{name:$runtime,key:"coder_agent_token"}}
  }])]
' "${temp_dir}/raw-env-pod.json" >"${temp_dir}/raw-init-pod.json"
expect_denied 'raw Pod init-container agent-token reference' "${pod_message}" \
  kubectl create --dry-run=server --output=name --filename="${temp_dir}/raw-init-pod.json" --as="${team_user}"
jq --arg config "${buildbuddy_config}" '
  del(.spec.containers[0].env) |
  .metadata.name = "raw-buildbuddy-volume" |
  .metadata.labels["com.coder.workspace.id"] = "11111111-1111-4111-8111-111111111111" |
  .spec.containers[0].name = "workspace" |
  .spec.containers[0].volumeMounts = [{
    name:"buildbuddy",mountPath:"/var/run/workspace/buildbuddy",readOnly:true
  }] |
  .spec.volumes = [{name:"buildbuddy",projected:{defaultMode:256,sources:[
    {configMap:{name:$config,items:[{key:"buildbuddy.bazelrc",path:"buildbuddy.bazelrc"}]}},
    {secret:{name:"workspace-buildbuddy-auth",items:[{key:"credentials.bazelrc",path:"credentials.bazelrc"}]}}
  ]}}]
' "${temp_dir}/raw-env-pod.json" >"${temp_dir}/raw-buildbuddy-pod.json"
expect_denied 'raw Pod BuildBuddy credential projection' "${buildbuddy_message}" \
  kubectl create --dry-run=server --output=name \
  --filename="${temp_dir}/raw-buildbuddy-pod.json" --as="${team_user}"
jq '
  (.spec.containers[] | select(.name == "workspace") |
    .volumeMounts[] | select(.name == "buildbuddy")).mountPath = "/tmp/buildbuddy"
' "${pod_json}" >"${temp_dir}/pod-buildbuddy-mount-path.json"
expect_denied 'Coder Pod with a noncanonical BuildBuddy mount' "${buildbuddy_message}" \
  kubectl create --dry-run=server --output=name \
  --filename="${temp_dir}/pod-buildbuddy-mount-path.json" \
  --as=system:serviceaccount:kube-system:replicaset-controller

jq '
  {
    apiVersion:"batch/v1",
    kind:"Job",
    metadata:{name:"raw-protected-job",namespace:.metadata.namespace,labels:.metadata.labels},
    spec:{template:{metadata:{labels:.metadata.labels},spec:(.spec | .restartPolicy = "Never" |
      .containers[0].env = [] |
      .containers[0].envFrom = [{secretRef:{name:"workspace-backups"}}])}}
  }
' "${temp_dir}/raw-env-pod.json" >"${temp_dir}/raw-job.json"
expect_denied 'Job envFrom organization credential' "${other_controller_message}" \
  kubectl create --dry-run=server --output=name --filename="${temp_dir}/raw-job.json" --as="${team_user}"
jq '
  {
    apiVersion:"batch/v1",
    kind:"CronJob",
    metadata:{name:"raw-protected-cron",namespace:.metadata.namespace,labels:.metadata.labels},
    spec:{
      schedule:"0 0 1 1 *",
      jobTemplate:{metadata:{labels:.metadata.labels},spec:{template:{metadata:{labels:.metadata.labels},spec:(.spec | .restartPolicy = "Never" |
        .containers[0].env = [] |
        .volumes = [{name:"stolen",projected:{sources:[{secret:{name:"workspace-backups"}}]}}])}}}
    }
  }
' "${temp_dir}/raw-env-pod.json" >"${temp_dir}/raw-cron.json"
expect_denied 'CronJob projected organization credential' "${cron_message}" \
  kubectl create --dry-run=server --output=name --filename="${temp_dir}/raw-cron.json" --as="${team_user}"
jq --arg runtime "${runtime_secret}" '
  {
    apiVersion:"apps/v1",
    kind:"StatefulSet",
    metadata:{name:"raw-protected-statefulset",namespace:.metadata.namespace,labels:.metadata.labels},
    spec:{
      serviceName:"raw-protected-statefulset",
      replicas:1,
      selector:{matchLabels:{app:"raw-protected-statefulset"}},
      template:{\
        metadata:{labels:(.metadata.labels + {app:"raw-protected-statefulset"})},
        spec:(.spec | .restartPolicy = "Always" | .containers[0].env = [] | .volumes = [{
          name:"stolen",
          csi:{driver:"fixture.csi.invalid",nodePublishSecretRef:{name:$runtime}}
        }])
      }
    }
  }
' "${temp_dir}/raw-env-pod.json" >"${temp_dir}/raw-statefulset.json"
expect_denied 'StatefulSet CSI agent-token reference' "${other_controller_message}" \
  kubectl create --dry-run=server --output=name --filename="${temp_dir}/raw-statefulset.json" --as="${team_user}"

jq '.metadata.annotations.fixture = "true"' "${runtime}" >"${temp_dir}/runtime-update.json"
expect_denied 'team runtime Secret update' "${runtime_message}" \
  kubectl replace --as="${team_user}" \
  --raw="/api/v1/namespaces/${namespace}/secrets/${runtime_secret}?dryRun=All" \
  --filename="${temp_dir}/runtime-update.json"
jq 'del(.metadata.labels["com.coder.resource"])' "${runtime}" >"${temp_dir}/runtime-label-removal.json"
expect_denied 'provisioner runtime Secret label removal' "${runtime_message}" \
  kubectl replace --as="${provisioner}" --as-group=cluster:coder-provisioners \
  --raw="/api/v1/namespaces/${namespace}/secrets/${runtime_secret}?dryRun=All" \
  --filename="${temp_dir}/runtime-label-removal.json"
kubectl rollout status deployment/"${deployment}" --namespace="${namespace}" --timeout=180s >/dev/null
protected_pod="$(kubectl get pods --namespace="${namespace}" \
  --selector="com.coder.workspace.id=${workspace_id}" --output=json \
  | jq -r 'if (.items | length) == 1 then .items[0].metadata.name else empty end')"
if [ -z "${protected_pod}" ]; then
  printf 'the canonical Deployment did not materialize exactly one Pod\n' >&2
  exit 1
fi
kubectl get pod "${protected_pod}" --namespace="${namespace}" --output=json \
  | jq --exit-status --arg runtime "${runtime_secret}" '
		(.spec.containers | map(.name)) == ["workspace"] and
		(.spec.initContainers | map(select(.name == "backup-proxy" or .name == "tailnet") | .restartPolicy) | unique) == ["Always"] and
		(.spec.initContainers | map(.name) | sort) == ["backup-proxy","tailnet"] and
		([.spec.containers[].env[]?, .spec.initContainers[].env[]?] | map(select(.valueFrom.secretKeyRef.name == $runtime)) | length) == 6 and
		([.spec.containers[].env[]?, .spec.initContainers[].env[]?] | map(select(.valueFrom.secretKeyRef.name == "workspace-backups")) | length) == 2 and
		([.spec.initContainers[] | select(.name == "backup-proxy") | .env[] |
		  select(.valueFrom.secretKeyRef.name == $runtime or
		    .valueFrom.secretKeyRef.name == "workspace-backups")] | length) == 4 and
		([.spec.initContainers[] | select(.name == "backup-proxy") | .env[] |
		  select(.name == "CODER_AGENT_TOKEN" and .valueFrom.secretKeyRef.key == "coder_agent_token")] | length) == 1 and
		([.spec.initContainers[] | select(.name == "backup-proxy") | .env[] |
		  select(.name == "RCLONE_AUTH_KEY" and .valueFrom.secretKeyRef.key == "backup_proxy_auth_key")] | length) == 1 and
		([.spec.initContainers[] | select(.name == "backup-proxy") | .env[] |
		  select(.name == "RCLONE_CONFIG_BACKEND_ACCESS_KEY_ID" and
		    .valueFrom.secretKeyRef.name == "workspace-backups" and
		    .valueFrom.secretKeyRef.key == "access_key_id")] | length) == 1 and
		([.spec.initContainers[] | select(.name == "backup-proxy") | .env[] |
		  select(.name == "RCLONE_CONFIG_BACKEND_SECRET_ACCESS_KEY" and
		    .valueFrom.secretKeyRef.name == "workspace-backups" and
		    .valueFrom.secretKeyRef.key == "secret_access_key")] | length) == 1 and
		([.spec.volumes[] | select(.name == "buildbuddy") | .projected.sources[] |
		  select(.secret.name == "workspace-buildbuddy-auth" and
		    .secret.items == [{"key":"credentials.bazelrc","path":"credentials.bazelrc"}])] | length) == 1
  ' >/dev/null

for patch in label owner finalizer; do
  case "${patch}" in
    label)
      payload='[{"op":"remove","path":"/spec/template/metadata/labels/com.coder.resource"}]'
      ;;
    owner)
      payload='[{"op":"add","path":"/metadata/ownerReferences","value":[{"apiVersion":"v1","kind":"Pod","name":"forged","uid":"cccccccc-cccc-4ccc-8ccc-cccccccccccc"}]}]'
      ;;
    finalizer)
      payload='[{"op":"add","path":"/metadata/finalizers","value":["chainsaw.kyverno.io/hold"]}]'
      ;;
    *) ;;
  esac
  expect_denied "team Deployment ${patch} patch" "${controller_message}" \
    kubectl patch deployment "${deployment}" --namespace="${namespace}" --as="${team_user}" \
    --dry-run=server --type=json --patch="${payload}"
done
expect_denied 'provisioner Deployment protected-reference removal' "${controller_message}" \
  kubectl patch deployment "${deployment}" --namespace="${namespace}" --as="${provisioner}" --as-group=cluster:coder-provisioners \
  --dry-run=server --type=json \
  --patch='[{"op":"remove","path":"/spec/template/spec/containers/0/env/2"}]'
expect_denied 'provisioner Deployment shared-process-namespace update' "${controller_message}" \
  kubectl patch deployment "${deployment}" --namespace="${namespace}" --as="${provisioner}" --as-group=cluster:coder-provisioners \
  --dry-run=server --type=merge --patch='{"spec":{"template":{"spec":{"shareProcessNamespace":true}}}}'

for patch in label owner finalizer; do
  case "${patch}" in
    label)
      payload='[{"op":"remove","path":"/metadata/labels/com.coder.resource"}]'
      ;;
    owner)
      payload='[{"op":"remove","path":"/metadata/ownerReferences"}]'
      ;;
    finalizer)
      payload='[{"op":"add","path":"/metadata/finalizers","value":["chainsaw.kyverno.io/hold"]}]'
      ;;
    *) ;;
  esac
  expect_denied "protected Pod ${patch} patch" "${pod_message}" \
    kubectl patch pod "${protected_pod}" --namespace="${namespace}" --as="${team_user}" \
    --dry-run=server --type=json --patch="${payload}"
done
jq --null-input --arg image "${busybox_image}" --arg namespace "${namespace}" '
  {
    apiVersion:"v1",
    kind:"Pod",
    metadata:{name:"ordinary-debug-target",namespace:$namespace,
      labels:{"availability-class":"be","latency-class":"ls"}},
    spec:{
      restartPolicy:"Never",
      automountServiceAccountToken:false,
      securityContext:{runAsNonRoot:true,runAsUser:1000,runAsGroup:1000,seccompProfile:{type:"RuntimeDefault"}},
      containers:[{
        name:"app",
        image:$image,
        command:["sh","-ceu","while :; do sleep 30; done"],
        resources:{requests:{cpu:"1m",memory:"4Mi"},limits:{cpu:"50m",memory:"16Mi"}},
        securityContext:{allowPrivilegeEscalation:false,readOnlyRootFilesystem:true,capabilities:{drop:["ALL"]}}
      }]
    }
  }
' >"${ordinary_pod}"
kubectl create --filename="${ordinary_pod}" --as="${team_user}" >/dev/null
kubectl wait pod/ordinary-debug-target --namespace="${namespace}" \
  --for=condition=Ready --timeout=120s >/dev/null
expect_allowed 'ordinary application Pod exec' \
  kubectl exec pod/ordinary-debug-target --namespace="${namespace}" --as="${team_user}" -- true

expect_connect_denied 'protected Pod exec' \
  kubectl exec pod/"${protected_pod}" --namespace="${namespace}" --container=workspace \
  --as="${team_user}" -- true
expect_connect_denied 'protected Pod attach' \
  kubectl attach pod/"${protected_pod}" --namespace="${namespace}" --container=workspace \
  --as="${team_user}"
local_port=$((20000 + $$ % 10000))
expect_connect_denied 'protected Pod port-forward' \
  kubectl port-forward pod/"${protected_pod}" --namespace="${namespace}" --as="${team_user}" \
  "${local_port}:19000"

jq --null-input --arg image "${busybox_image}" '
  {spec:{ephemeralContainers:[{
    name:"debugger",
    image:$image,
    command:["sh","-c","true"],
    securityContext:{allowPrivilegeEscalation:false,readOnlyRootFilesystem:true,capabilities:{drop:["ALL"]}}
  }]}}
' >"${temp_dir}/ordinary-ephemeral.json"
expect_denied 'ordinary application Pod ephemeral-container injection' 'Team pods must not embed ephemeral containers.' \
  kubectl patch pod/ordinary-debug-target --namespace="${namespace}" \
  --subresource=ephemeralcontainers --type=merge \
  --patch-file="${temp_dir}/ordinary-ephemeral.json" --as="${team_user}" --dry-run=server
jq --null-input --arg image "${busybox_image}" '
  {spec:{ephemeralContainers:[{
    name:"debugger",
    image:$image,
    command:["sh","-c","true"],
    env:[{name:"STOLEN",valueFrom:{secretKeyRef:{name:"workspace-backups",key:"access_key_id"}}}],
    securityContext:{allowPrivilegeEscalation:false,readOnlyRootFilesystem:true,capabilities:{drop:["ALL"]}}
  }]}}
' >"${temp_dir}/stolen-ephemeral.json"
expect_denied 'ordinary Pod ephemeral-container credential theft' "${pod_message}" \
  kubectl patch pod/ordinary-debug-target --namespace="${namespace}" \
  --subresource=ephemeralcontainers --type=merge \
  --patch-file="${temp_dir}/stolen-ephemeral.json" --as="${team_user}" --dry-run=server

kubectl get pod "${protected_pod}" --namespace="${namespace}" --output=json \
  >"${temp_dir}/protected-ephemeral.json"
expect_denied 'protected Pod ephemeral-container session' "${interactive_message}" \
  kubectl replace --subresource=ephemeralcontainers --dry-run=server \
  --filename="${temp_dir}/protected-ephemeral.json" --as="${team_user}"

trap - EXIT HUP INT TERM
cleanup
