#!/bin/sh
# Verifies GPU class admission defaults and mutates accelerator tolerations across native and Ray pods to defend hardware portability.

# shellcheck disable=SC2310
set -eu

busybox_image="${BUSYBOX_IMAGE:?busybox image is required}"
aws_accelerator_label_prefix=k8s.amazonaws.com
script_directory="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
repository_root="$(git -C "${script_directory}" rev-parse --show-toplevel)"
run_id=$$
namespace=gpu-class-admission-e2e-${run_id}
policy_suffix=gpu-class-e2e-${run_id}
compute_config=team-compute-capabilities-gpu-e2e-${run_id}
test_root="$(mktemp -d)"
base_job="${test_root}/job.json"
gpu_job="${test_root}/gpu-job.json"

cluster() {
  kubectl "$@"
}

if cluster get --raw /apis/admissionregistration.k8s.io/v1 | jq --exit-status \
  '.resources | any(.name == "mutatingadmissionpolicies")' >/dev/null; then
  mutating_api_version=admissionregistration.k8s.io/v1
else
  mutating_api_version=admissionregistration.k8s.io/v1beta1
fi

team() {
  kubectl --namespace="${namespace}" "$@"
}

cleanup_cluster() {
  cluster delete validatingadmissionpolicybinding/team-governed-workload-"${policy_suffix}" \
    mutatingadmissionpolicybinding/team-scheduling-defaults-"${policy_suffix}" \
    validatingadmissionpolicy/team-governed-workload-"${policy_suffix}" \
    mutatingadmissionpolicy/team-scheduling-defaults-"${policy_suffix}" \
    --ignore-not-found=true --wait=false >/dev/null 2>&1 || true
  cluster delete configmap/"${compute_config}" --namespace=kueue-system \
    --ignore-not-found=true --wait=false >/dev/null 2>&1 || true
  cluster delete namespace/"${namespace}" --ignore-not-found=true \
    --wait=false >/dev/null 2>&1 || true
}

cleanup() {
  cleanup_cluster
  rm -rf -- "${test_root}"
}
trap cleanup EXIT HUP INT TERM
cleanup_cluster

cluster create namespace "${namespace}" >/dev/null
cluster label namespace "${namespace}" \
  gpu-class-admission-test=true --overwrite >/dev/null
team create serviceaccount ray-runner >/dev/null
cluster create configmap "${compute_config}" --namespace=kueue-system \
  --from-literal=cpu.avx2=true \
  --from-literal=gpu.l4=2 \
  --from-literal=tpu.v5e-2x2=4 >/dev/null

base_manifests="${test_root}/base.yaml"
cluster kustomize \
  "${repository_root}/src/infra/argocd/components/kueue/kustomize" \
  >"${base_manifests}"

yq -o=json 'select(.kind == "MutatingAdmissionPolicy")' "${base_manifests}" | jq \
  --arg name "team-scheduling-defaults-${policy_suffix}" \
  --arg api_version "${mutating_api_version}" \
  '.apiVersion = $api_version | .metadata.name = $name' \
  | cluster create --filename=- >/dev/null
yq -o=json 'select(.kind == "ValidatingAdmissionPolicy")' "${base_manifests}" | jq \
  --arg name "team-governed-workload-${policy_suffix}" \
  '.metadata.name = $name' | cluster create --filename=- >/dev/null

policy_name=team-governed-workload-${policy_suffix}
attempt=0
while [ "${attempt}" -lt 30 ]; do
  policy_status="$(cluster get validatingadmissionpolicy "${policy_name}" \
    --output=json)"
  if printf '%s\n' "${policy_status}" | jq --exit-status \
    '.status.observedGeneration == .metadata.generation' >/dev/null; then
    if ! printf '%s\n' "${policy_status}" | jq --exit-status \
      '(.status.typeChecking.expressionWarnings // []) | length == 0' \
      >/dev/null; then
      printf 'Admission policy %s has CEL type-check warnings:\n%s\n' \
        "${policy_name}" "${policy_status}" >&2
      exit 1
    fi
    break
  fi
  attempt=$((attempt + 1))
  sleep 1
done
if [ "${attempt}" -eq 30 ]; then
  printf 'Admission policy %s did not finish type checking.\n' \
    "${policy_name}" >&2
  exit 1
fi

cluster create --filename=- >/dev/null <<EOF
apiVersion: ${mutating_api_version}
kind: MutatingAdmissionPolicyBinding
metadata:
  name: team-scheduling-defaults-${policy_suffix}
spec:
  policyName: team-scheduling-defaults-${policy_suffix}
  matchResources:
    namespaceSelector:
      matchLabels:
        gpu-class-admission-test: "true"
---
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicyBinding
metadata:
  name: team-governed-workload-${policy_suffix}
spec:
  policyName: team-governed-workload-${policy_suffix}
  paramRef:
    name: ${compute_config}
    namespace: kueue-system
    parameterNotFoundAction: Deny
  validationActions: [Deny]
  matchResources:
    namespaceSelector:
      matchLabels:
        gpu-class-admission-test: "true"
EOF

expect_denied() {
  expected="$1"
  manifest="$2"
  set +e
  output="$(team create --dry-run=server --output=name \
    --filename="${manifest}" 2>&1)"
  result=$?
  set -e
  if [ "${result}" -eq 0 ]; then
    printf 'Admission unexpectedly accepted %s.\n' "${manifest}" >&2
    return 1
  fi
  if ! printf '%s\n' "${output}" | grep -F "${expected}" >/dev/null; then
    printf 'Admission failed outside %s:\n%s\n' "${expected}" "${output}" >&2
    return 1
  fi
}

wait_for_admission_binding() {
  manifest="$1"
  attempt=0
  while [ "${attempt}" -lt 30 ]; do
    set +e
    output="$(team create --dry-run=server --output=name \
      --filename="${manifest}" 2>&1)"
    result=$?
    set -e
    if [ "${result}" -ne 0 ]; then
      if printf '%s\n' "${output}" | grep -F \
        '[rule:cpu-capability-accelerator-exclusive]' >/dev/null; then
        return 0
      fi
      printf 'Admission failed before the expected binding denial:\n%s\n' \
        "${output}" >&2
      return 1
    fi
    attempt=$((attempt + 1))
    sleep 1
  done
  printf 'Admission binding did not enforce the CPU/accelerator exclusion.\n' >&2
  return 1
}

expect_cleanly_denied() {
  manifest="$1"
  set +e
  output="$(team create --dry-run=server --output=name \
    --filename="${manifest}" 2>&1)"
  result=$?
  set -e
  if [ "${result}" -eq 0 ]; then
    printf 'Admission unexpectedly accepted %s.\n' "${manifest}" >&2
    return 1
  fi
  if printf '%s\n' "${output}" | grep -E \
    'failed to evaluate|evaluation error|internal error' >/dev/null; then
    printf 'Admission rejected %s with a CEL evaluation error:\n%s\n' \
      "${manifest}" "${output}" >&2
    return 1
  fi
}

expect_gpu_accepted() {
  manifest="$1"
  pod_spec_filter="$2"
  response="${test_root}/accepted.json"
  attempt=0
  while [ "${attempt}" -lt 30 ]; do
    if team create --dry-run=server --output=json \
      --filename="${manifest}" >"${response}" 2>"${response}.error" \
      && jq --exit-status \
        "${pod_spec_filter} as \$podSpec |
       \$podSpec.tolerations | any(.[];
         .key == \"nvidia.com/gpu\" and
         .operator == \"Equal\" and
         .value == \"present\" and
         .effect == \"NoSchedule\")" \
        "${response}" >/dev/null 2>&1; then
      return 0
    fi
    attempt=$((attempt + 1))
    sleep 1
  done
  cat "${response}" >&2
  cat "${response}.error" >&2
  return 1
}

expect_gpu_pod_accepted() {
  manifest="$1"
  response="${test_root}/accepted-pod.json"
  team create --dry-run=server --output=json \
    --filename="${manifest}" >"${response}"
  jq --exit-status '
    .spec.priorityClassName == "wa-lt" and
    .spec.priority == 21 and
    (.spec.tolerations | any(.[];
      .key == "nvidia.com/gpu" and
      .operator == "Equal" and
      .value == "present" and
      .effect == "NoSchedule"))
  ' "${response}" >/dev/null
}

expect_tpu_accepted() {
  manifest="$1"
  pod_spec_filter="$2"
  response="${test_root}/accepted-tpu.json"
  team create --dry-run=server --output=json \
    --filename="${manifest}" >"${response}"
  if ! jq --exit-status \
    "${pod_spec_filter} as \$podSpec |
     \$podSpec.tolerations | any(.[];
       .key == \"google.com/tpu\" and
       .operator == \"Equal\" and
       .value == \"present\" and
       .effect == \"NoSchedule\")" \
    "${response}" >/dev/null; then
    cat "${response}" >&2
    return 1
  fi
}

expect_tpu_pod_accepted() {
  manifest="$1"
  response="${test_root}/accepted-tpu-pod.json"
  team create --dry-run=server --output=json \
    --filename="${manifest}" >"${response}"
  jq --exit-status '
    .spec.priorityClassName == "wa-lt" and
    .spec.priority == 21 and
    (.spec.tolerations | any(.[];
      .key == "google.com/tpu" and
      .operator == "Equal" and
      .value == "present" and
      .effect == "NoSchedule"))
  ' "${response}" >/dev/null
}

job_fixture="${repository_root}/src/infra/definitions/conformance/fixtures/scheduling/workload-class-agreement.test.k8s.yaml"
sed -e "s|image: (\$values.busyboxImage)|image: ${busybox_image}|" \
  "${job_fixture}" | kubectl create --dry-run=client --filename=- \
  --output=json | jq --arg namespace "${namespace}" '
      .metadata.name = "gpu-class-job" |
      .metadata.namespace = $namespace
    ' >"${base_job}"

jq '
  .spec.template.spec.nodeSelector = {"gpu-class":"l4"} |
  .spec.template.spec.containers[0].resources.limits["nvidia.com/gpu"] = "1"
' "${base_job}" >"${gpu_job}"
expect_gpu_accepted "${gpu_job}" '.spec.template.spec'

for accelerator in nvidia.com/gpu google.com/tpu; do
  amount=1
  if [ "${accelerator}" = google.com/tpu ]; then
    amount=4
  fi
  jq --arg accelerator "${accelerator}" --arg amount "${amount}" '
    .metadata.name = "cpu-capability-accelerator-denied" |
    .spec.template.spec.nodeSelector = {"cpu-capability.avx2":"true"} |
    .spec.template.spec.containers[0].resources.requests[$accelerator] = $amount |
    .spec.template.spec.containers[0].resources.limits[$accelerator] = $amount |
    .spec.template.spec.tolerations = [{
      key:$accelerator,
      operator:"Equal",
      value:"present",
      effect:"NoSchedule"
    }]
  ' "${base_job}" >"${test_root}/capability-accelerator-denied-job.json"
  if [ "${accelerator}" = nvidia.com/gpu ]; then
    wait_for_admission_binding "${test_root}/capability-accelerator-denied-job.json"
  fi
  expect_denied '[rule:cpu-capability-accelerator-exclusive]' \
    "${test_root}/capability-accelerator-denied-job.json"
done

jq '
  .metadata.name = "cpu-capability-limit-only-accelerator-denied" |
  .spec.template.spec.nodeSelector = {"cpu-capability.avx2":"true"} |
  .spec.template.spec.containers[0].resources.limits["nvidia.com/gpu"] = "1" |
  .spec.template.spec.tolerations = [{
    key:"nvidia.com/gpu",
    operator:"Equal",
    value:"present",
    effect:"NoSchedule"
  }]
' "${base_job}" >"${test_root}/capability-limit-only-accelerator-denied-job.json"
expect_denied '[rule:cpu-capability-accelerator-exclusive]' \
  "${test_root}/capability-limit-only-accelerator-denied-job.json"

jq '
  .metadata.name = "application-host-port" |
  .spec.template.spec.containers[0].ports = [{containerPort:8080, hostPort:18080}]
' "${base_job}" >"${test_root}/application-host-port.json"
expect_denied '[rule:host-port-forbidden]' \
  "${test_root}/application-host-port.json"

jq '
  .metadata.name = "init-host-port" |
  .spec.template.spec.initContainers = [{
    name:"host-port-init",
    image:.spec.template.spec.containers[0].image,
    command:["sh","-c","true"],
    ports:[{containerPort:8081, hostPort:18081}],
    resources:.spec.template.spec.containers[0].resources,
    securityContext:.spec.template.spec.containers[0].securityContext
  }]
' "${base_job}" >"${test_root}/init-host-port.json"
expect_denied '[rule:host-port-forbidden]' "${test_root}/init-host-port.json"

jq '
  .metadata.name = "gpu-canonical-toleration" |
  .spec.template.spec.tolerations = [{
    key:"nvidia.com/gpu", operator:"Equal", value:"present", effect:"NoSchedule"
  }]
' "${gpu_job}" >"${test_root}/gpu-canonical-toleration.json"
expect_gpu_accepted "${test_root}/gpu-canonical-toleration.json" \
  '.spec.template.spec'

jq '
  .metadata.name = "gpu-noncanonical-toleration" |
  .spec.template.spec.tolerations = [{
    key:"nvidia.com/gpu", operator:"Exists", effect:"NoSchedule"
  }]
' "${gpu_job}" >"${test_root}/gpu-noncanonical-toleration.json"
expect_denied '[rule:accelerator-toleration-canonical]' \
  "${test_root}/gpu-noncanonical-toleration.json"

jq '
  .metadata.name = "gpu-unpaired-toleration" |
  .spec.template.spec.tolerations = [{
    key:"nvidia.com/gpu", operator:"Equal", value:"present", effect:"NoSchedule"
  }]
' "${base_job}" >"${test_root}/gpu-unpaired-toleration.json"
expect_denied '[rule:accelerator-toleration-paired]' \
  "${test_root}/gpu-unpaired-toleration.json"

jq '
  .metadata.name = "wildcard-accelerator-toleration" |
  .spec.template.spec.tolerations = [{operator:"Exists", effect:"NoSchedule"}]
' "${base_job}" >"${test_root}/wildcard-accelerator-toleration.json"
expect_denied '[rule:accelerator-toleration-canonical]' \
  "${test_root}/wildcard-accelerator-toleration.json"

jq '
  .metadata.name = "gpu-request-without-limit" |
  .spec.template.spec.containers[0].resources.requests["nvidia.com/gpu"] = "1" |
  del(.spec.template.spec.containers[0].resources.limits["nvidia.com/gpu"])
' "${gpu_job}" >"${test_root}/request-without-limit.json"
expect_cleanly_denied "${test_root}/request-without-limit.json"

jq '
  .metadata.name = "gpu-request-limit-mismatch" |
  .spec.template.spec.containers[0].resources.requests["nvidia.com/gpu"] = "1" |
  .spec.template.spec.containers[0].resources.limits["nvidia.com/gpu"] = "2"
' "${gpu_job}" >"${test_root}/request-limit-mismatch.json"
expect_cleanly_denied "${test_root}/request-limit-mismatch.json"

for quantity in 0 500m; do
  jq --arg quantity "${quantity}" '
    .metadata.name = "gpu-invalid-count" |
    .spec.template.spec.containers[0].resources.limits["nvidia.com/gpu"] = $quantity
  ' "${gpu_job}" >"${test_root}/invalid-count.json"
  expect_cleanly_denied "${test_root}/invalid-count.json"
done

jq '
  .metadata.name = "gpu-class-app-plus-init" |
  .spec.template.spec.containers[0].resources.limits["nvidia.com/gpu"] = "2" |
  .spec.template.spec.initContainers = [{
    name:"gpu-init",
    image:.spec.template.spec.containers[0].image,
    command:["sh","-c","true"],
    resources:{limits:{"nvidia.com/gpu":"1"}}
  }]
' "${gpu_job}" >"${test_root}/app-plus-init.json"
expect_gpu_accepted "${test_root}/app-plus-init.json" '.spec.template.spec'

jq '
  .metadata.name = "gpu-class-app-sum-too-large" |
  .spec.template.spec.containers += [{
    name:"gpu-sidecar",
    image:.spec.template.spec.containers[0].image,
    command:["sh","-c","true"],
    resources:{limits:{"nvidia.com/gpu":"2"}}
  }]
' "${gpu_job}" >"${test_root}/app-sum-too-large.json"
expect_denied '[rule:gpu-class-max-count]' \
  "${test_root}/app-sum-too-large.json"

jq '
  .metadata.name = "gpu-class-init-too-large" |
  .spec.template.spec.initContainers = [{
    name:"gpu-init",
    image:.spec.template.spec.containers[0].image,
    command:["sh","-c","true"],
    resources:{limits:{"nvidia.com/gpu":"3"}}
  }]
' "${gpu_job}" >"${test_root}/init-too-large.json"
expect_denied '[rule:gpu-class-max-count]' "${test_root}/init-too-large.json"

jq '
  .metadata.name = "gpu-class-restartable-init" |
  .spec.template.spec.initContainers = [{
    name:"gpu-init-sidecar",
    image:.spec.template.spec.containers[0].image,
    command:["sh","-c","true"],
    restartPolicy:"Always",
    resources:{limits:{"nvidia.com/gpu":"1"}}
  }]
' "${gpu_job}" >"${test_root}/restartable-init.json"
expect_denied '[rule:gpu-restartable-init-unsupported]' \
  "${test_root}/restartable-init.json"

jq '
  .metadata.name = "gpu-class-unknown" |
  .spec.template.spec.nodeSelector["gpu-class"] = "unknown"
' "${gpu_job}" >"${test_root}/unknown.json"
expect_denied '[rule:gpu-class-offered]' "${test_root}/unknown.json"

jq '
  .metadata.name = "gpu-class-without-count" |
  del(.spec.template.spec.containers[0].resources.limits["nvidia.com/gpu"])
' "${gpu_job}" >"${test_root}/class-without-count.json"
expect_denied '[rule:gpu-class-request-paired]' \
  "${test_root}/class-without-count.json"

jq '
  .metadata.name = "gpu-count-without-class" |
  del(.spec.template.spec.nodeSelector)
' "${gpu_job}" >"${test_root}/count-without-class.json"
expect_denied '[rule:gpu-request-class-paired]' \
  "${test_root}/count-without-class.json"

jq '
  .metadata.name = "gpu-class-affinity" |
  del(.spec.template.spec.nodeSelector) |
  .spec.template.spec.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution = {
    nodeSelectorTerms:[{matchExpressions:[{
      key:"gpu-class", operator:"In", values:["l4"]
    }]}]
  }
' "${gpu_job}" >"${test_root}/class-affinity.json"
expect_denied '[rule:gpu-class-node-selector-only]' \
  "${test_root}/class-affinity.json"

jq '
  .metadata.name = "gpu-class-topology" |
  del(.spec.template.spec.nodeSelector) |
  .spec.template.spec.topologySpreadConstraints = [{
    maxSkew:1,
    topologyKey:"gpu-class",
    whenUnsatisfiable:"ScheduleAnyway"
  }]
' "${gpu_job}" >"${test_root}/class-topology.json"
expect_denied '[rule:gpu-class-node-selector-only]' \
  "${test_root}/class-topology.json"

while read -r key value; do
  jq --arg key "${key}" --arg value "${value}" '
    .metadata.name = "provider-placement-denied" |
    .spec.template.spec.nodeSelector = {($key):$value}
  ' "${base_job}" >"${test_root}/provider-placement.json"
  expect_denied '[rule:portable-node-placement]' \
    "${test_root}/provider-placement.json"
done <<EOF
agentpool gpu
cloud.google.com/gke-accelerator nvidia-l4
cloud.google.com/gke-spot true
eks.amazonaws.com/capacityType SPOT
eks.amazonaws.com/nodegroup gpu
${aws_accelerator_label_prefix}/accelerator nvidia-tesla-t4
karpenter.k8s.aws/instance-gpu-name l4
karpenter.k8s.gcp/instance-gpu-count 1
nvidia.com/gpu.product NVIDIA-L4
EOF

jq '
  .metadata.name = "tpu-model-selector-denied" |
  .spec.template.spec.nodeSelector = {
    "cloud.google.com/gke-tpu-accelerator":"tpu-v5-lite-podslice",
    "cloud.google.com/gke-tpu-topology":"2x2"
  }
' "${base_job}" >"${test_root}/tpu-selector.json"
expect_denied '[rule:portable-node-placement]' "${test_root}/tpu-selector.json"

jq '
  {
    apiVersion:"v1",
    kind:"Pod",
    metadata:{name:"gpu-class-pod", namespace:.metadata.namespace, labels:.metadata.labels},
    spec:.spec.template.spec
  }
' "${gpu_job}" >"${test_root}/pod.json"
expect_gpu_pod_accepted "${test_root}/pod.json"
team create --output=json --filename="${test_root}/pod.json" \
  >"${test_root}/accepted-live-pod.json"
jq --exit-status '
  .spec.priorityClassName == "wa-lt" and
  .spec.priority == 21 and
  (.spec.tolerations | any(.[];
    .key == "nvidia.com/gpu" and
    .operator == "Equal" and
    .value == "present" and
    .effect == "NoSchedule"))
' "${test_root}/accepted-live-pod.json" >/dev/null
team delete pod/gpu-class-pod --wait=false >/dev/null

jq '
  .metadata.name = "gpu-class-pod-wrong-priority-class" |
  .spec.priorityClassName = "ha-ls"
' "${test_root}/pod.json" >"${test_root}/pod-wrong-priority-class.json"
expect_cleanly_denied "${test_root}/pod-wrong-priority-class.json"

jq '
  .metadata.name = "gpu-class-pod-wrong-priority" |
  .spec.priority = 999
' "${test_root}/pod.json" >"${test_root}/pod-wrong-priority.json"
expect_cleanly_denied "${test_root}/pod-wrong-priority.json"

jq '
  {
    apiVersion:"apps/v1",
    kind:"Deployment",
    metadata:{name:"gpu-class-deployment", namespace:.metadata.namespace, labels:.metadata.labels},
    spec:{
      replicas:1,
      selector:{matchLabels:{app:"gpu-class-deployment"}},
      template:{metadata:{labels:{app:"gpu-class-deployment"}}, spec:.spec.template.spec}
    }
  } |
  .spec.template.spec.restartPolicy = "Always"
' "${gpu_job}" >"${test_root}/deployment.json"
expect_gpu_accepted "${test_root}/deployment.json" '.spec.template.spec'

jq '
  {
    apiVersion:"apps/v1",
    kind:"StatefulSet",
    metadata:{name:"gpu-class-statefulset", namespace:.metadata.namespace, labels:.metadata.labels},
    spec:{
      serviceName:"gpu-class-statefulset",
      replicas:1,
      selector:{matchLabels:{app:"gpu-class-statefulset"}},
      template:{metadata:{labels:{app:"gpu-class-statefulset"}}, spec:.spec.template.spec}
    }
  } |
  .spec.template.spec.restartPolicy = "Always"
' "${gpu_job}" >"${test_root}/statefulset.json"
expect_gpu_accepted "${test_root}/statefulset.json" '.spec.template.spec'

ray_job="${test_root}/ray-job.json"
yq -o=json 'select(.kind == "RayJob")' "${repository_root}/src/examples/ray_data/deployment/ray-data.k8s.yaml" | jq \
  --arg image "${busybox_image}" \
  --arg namespace "${namespace}" '
    .metadata.name = "gpu-class-ray-job" |
    .metadata.namespace = $namespace |
    del(.spec.submitterPodTemplate) |
    .spec.rayClusterSpec.headGroupSpec.template.spec.containers[].image = $image |
    .spec.rayClusterSpec.workerGroupSpecs[].template.spec.containers[].image = $image |
    .spec.rayClusterSpec.workerGroupSpecs[0].template.spec.nodeSelector = {
      "gpu-class":"l4"
    } |
    .spec.rayClusterSpec.workerGroupSpecs[0].template.spec.containers[0].resources.limits["nvidia.com/gpu"] = "1"
  ' >"${ray_job}"
expect_gpu_accepted "${ray_job}" \
  '.spec.rayClusterSpec.workerGroupSpecs[0].template.spec'

jq '
  .metadata.name = "gpu-class-ray-request-only" |
  .spec.rayClusterSpec.workerGroupSpecs[0].template.spec.containers[0].resources.requests["nvidia.com/gpu"] = "1" |
  del(.spec.rayClusterSpec.workerGroupSpecs[0].template.spec.containers[0].resources.limits["nvidia.com/gpu"])
' "${ray_job}" >"${test_root}/ray-request-only.json"
expect_denied '[rule:gpu-count-valid]' "${test_root}/ray-request-only.json"

jq '
  .metadata.name = "gpu-class-ray-request-limit-mismatch" |
  .spec.rayClusterSpec.workerGroupSpecs[0].template.spec.containers[0].resources.requests["nvidia.com/gpu"] = "1" |
  .spec.rayClusterSpec.workerGroupSpecs[0].template.spec.containers[0].resources.limits["nvidia.com/gpu"] = "2"
' "${ray_job}" >"${test_root}/ray-request-limit-mismatch.json"
expect_denied '[rule:gpu-count-valid]' \
  "${test_root}/ray-request-limit-mismatch.json"

for quantity in 0 500m; do
  jq --arg quantity "${quantity}" '
    .metadata.name = "gpu-class-ray-invalid-count" |
    .spec.rayClusterSpec.workerGroupSpecs[0].template.spec.containers[0].resources.limits["nvidia.com/gpu"] = $quantity
  ' "${ray_job}" >"${test_root}/ray-invalid-count.json"
  expect_denied '[rule:gpu-count-valid]' "${test_root}/ray-invalid-count.json"
done

jq '
  {
    apiVersion:"ray.io/v1",
    kind:"RayCluster",
    metadata:{name:"gpu-class-ray-cluster", namespace:.metadata.namespace, labels:.metadata.labels},
    spec:.spec.rayClusterSpec
  }
' "${ray_job}" >"${test_root}/ray-cluster.json"
expect_gpu_accepted "${test_root}/ray-cluster.json" \
  '.spec.workerGroupSpecs[0].template.spec'

yq -o=json 'select(.kind == "RayService")' "${repository_root}/src/examples/ray_serve/deployment/ray-serve.k8s.yaml" | jq \
  --arg image "${busybox_image}" \
  --arg namespace "${namespace}" '
    .metadata.name = "gpu-class-ray-service" |
    .metadata.namespace = $namespace |
    .spec.rayClusterConfig.headGroupSpec.template.spec.containers[].image = $image |
    .spec.rayClusterConfig.workerGroupSpecs[].template.spec.containers[].image = $image |
    .spec.rayClusterConfig.workerGroupSpecs[0].template.spec.nodeSelector = {
      "gpu-class":"l4"
    } |
    .spec.rayClusterConfig.workerGroupSpecs[0].template.spec.containers[0].resources.limits["nvidia.com/gpu"] = "1"
  ' >"${test_root}/ray-service.json"
expect_gpu_accepted "${test_root}/ray-service.json" \
  '.spec.rayClusterConfig.workerGroupSpecs[0].template.spec'

tpu_job="${test_root}/tpu-job.json"
jq '
  .metadata.name = "tpu-class-job" |
  .spec.template.spec.nodeSelector = {"tpu-class":"v5e-2x2"} |
  .spec.template.spec.tolerations = [{key:"workload", operator:"Exists"}] |
  .spec.template.spec.containers[0].resources.limits["google.com/tpu"] = "4"
' "${base_job}" >"${tpu_job}"
expect_tpu_accepted "${tpu_job}" '.spec.template.spec'

jq '
  .metadata.name = "tpu-canonical-toleration" |
  .spec.template.spec.tolerations = [{
    key:"google.com/tpu", operator:"Equal", value:"present", effect:"NoSchedule"
  }]
' "${tpu_job}" >"${test_root}/tpu-canonical-toleration.json"
expect_tpu_accepted "${test_root}/tpu-canonical-toleration.json" \
  '.spec.template.spec'

jq '
  .metadata.name = "tpu-noncanonical-toleration" |
  .spec.template.spec.tolerations = [{
    key:"google.com/tpu", operator:"Equal", value:"true", effect:"NoSchedule"
  }]
' "${tpu_job}" >"${test_root}/tpu-noncanonical-toleration.json"
expect_denied '[rule:accelerator-toleration-canonical]' \
  "${test_root}/tpu-noncanonical-toleration.json"

jq '
  .metadata.name = "tpu-unpaired-toleration" |
  .spec.template.spec.tolerations = [{
    key:"google.com/tpu", operator:"Equal", value:"present", effect:"NoSchedule"
  }]
' "${base_job}" >"${test_root}/tpu-unpaired-toleration.json"
expect_denied '[rule:accelerator-toleration-paired]' \
  "${test_root}/tpu-unpaired-toleration.json"

jq '
  .metadata.name = "tpu-request-without-limit" |
  .spec.template.spec.containers[0].resources.requests["google.com/tpu"] = "4" |
  del(.spec.template.spec.containers[0].resources.limits["google.com/tpu"])
' "${tpu_job}" >"${test_root}/tpu-request-without-limit.json"
expect_cleanly_denied "${test_root}/tpu-request-without-limit.json"

jq '
  .metadata.name = "tpu-wrong-chip-count" |
  .spec.template.spec.containers[0].resources.limits["google.com/tpu"] = "2"
' "${tpu_job}" >"${test_root}/tpu-wrong-chip-count.json"
expect_denied '[rule:tpu-class-count]' "${test_root}/tpu-wrong-chip-count.json"

jq '
  .metadata.name = "tpu-unknown-class" |
  .spec.template.spec.nodeSelector["tpu-class"] = "unknown"
' "${tpu_job}" >"${test_root}/tpu-unknown-class.json"
expect_denied '[rule:tpu-class-offered]' "${test_root}/tpu-unknown-class.json"

jq '
  .metadata.name = "tpu-class-without-count" |
  del(.spec.template.spec.containers[0].resources.limits["google.com/tpu"])
' "${tpu_job}" >"${test_root}/tpu-class-without-count.json"
expect_denied '[rule:tpu-class-request-paired]' \
  "${test_root}/tpu-class-without-count.json"

jq '
  .metadata.name = "tpu-count-without-class" |
  del(.spec.template.spec.nodeSelector)
' "${tpu_job}" >"${test_root}/tpu-count-without-class.json"
expect_denied '[rule:tpu-request-class-paired]' \
  "${test_root}/tpu-count-without-class.json"

jq '
  .metadata.name = "tpu-with-gpu" |
  .spec.template.spec.nodeSelector["gpu-class"] = "l4" |
  .spec.template.spec.containers[0].resources.limits["nvidia.com/gpu"] = "1"
' "${tpu_job}" >"${test_root}/tpu-with-gpu.json"
expect_denied '[rule:tpu-gpu-exclusive]' "${test_root}/tpu-with-gpu.json"

jq '
  {
    apiVersion:"v1",
    kind:"Pod",
    metadata:{name:"tpu-class-pod", namespace:.metadata.namespace, labels:.metadata.labels},
    spec:.spec.template.spec
  }
' "${tpu_job}" >"${test_root}/tpu-pod.json"
expect_tpu_pod_accepted "${test_root}/tpu-pod.json"

jq '
  .metadata.name = "tpu-class-ray-job" |
  .spec.rayClusterSpec.workerGroupSpecs[0].template.spec.nodeSelector = {
    "tpu-class":"v5e-2x2"
  } |
  del(.spec.rayClusterSpec.workerGroupSpecs[0].template.spec.containers[0].resources.limits["nvidia.com/gpu"]) |
  .spec.rayClusterSpec.workerGroupSpecs[0].template.spec.containers[0].resources.limits["google.com/tpu"] = "4"
' "${ray_job}" >"${test_root}/tpu-ray-job.json"
expect_tpu_accepted "${test_root}/tpu-ray-job.json" \
  '.spec.rayClusterSpec.workerGroupSpecs[0].template.spec'

jq '
  {
    apiVersion:"ray.io/v1",
    kind:"RayCluster",
    metadata:{name:"tpu-class-ray-cluster", namespace:.metadata.namespace, labels:.metadata.labels},
    spec:.spec.rayClusterSpec
  }
' "${test_root}/tpu-ray-job.json" >"${test_root}/tpu-ray-cluster.json"
expect_tpu_accepted "${test_root}/tpu-ray-cluster.json" \
  '.spec.workerGroupSpecs[0].template.spec'

jq '
  .metadata.name = "tpu-class-ray-service" |
  .spec.rayClusterConfig.workerGroupSpecs[0].template.spec.nodeSelector = {
    "tpu-class":"v5e-2x2"
  } |
  del(.spec.rayClusterConfig.workerGroupSpecs[0].template.spec.containers[0].resources.limits["nvidia.com/gpu"]) |
  .spec.rayClusterConfig.workerGroupSpecs[0].template.spec.containers[0].resources.limits["google.com/tpu"] = "4"
' "${test_root}/ray-service.json" >"${test_root}/tpu-ray-service.json"
expect_tpu_accepted "${test_root}/tpu-ray-service.json" \
  '.spec.rayClusterConfig.workerGroupSpecs[0].template.spec'
