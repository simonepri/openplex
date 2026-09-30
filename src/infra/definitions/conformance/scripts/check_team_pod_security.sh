#!/bin/sh
# Verifies Kyverno restricts team Pod security contexts while admitting verified ParcaGPU profiling exceptions to defend host integrity.

# shellcheck disable=SC2312
set -eu

busybox_image="${BUSYBOX_IMAGE:?busybox image is required}"
namespace=team-examples-workloads
gpu_namespace=team-examples-workspaces
team_user=conformance-user
team_group=cluster:group:team:examples
profile_message='Team namespaces must retain native restricted audit and warning while Kyverno owns enforcement.'
parcagpu_message='CAP_PERFMON may be added only to containers with a positive NVIDIA GPU request.'
pod_security_policy=governed-parcagpu-pod-security
user_namespace_message='Team Pods using a user namespace must retain the non-root runtime contract.'
temp_dir="$(mktemp -d)"
safe_pod="${temp_dir}/safe-pod.json"

cleanup() {
  status=$?
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
  if ! printf '%s\n' "${output}" | grep -F "${expected}" >/dev/null; then
    printf '%s failed outside the expected admission boundary:\n%s\n' \
      "${description}" "${output}" >&2
    return 1
  fi
}

expect_pod_allowed() {
  expect_allowed "$1" kubectl create --dry-run=server --output=name \
    --filename="$2" --as="${team_user}" --as-group="${team_group}"
}

expect_pod_denied() {
  expect_denied "$1" "$2" kubectl create --dry-run=server --output=name \
    --filename="$3" --as="${team_user}" --as-group="${team_group}"
}

admit_pod_json() {
  description="$1"
  pod="$2"
  admitted="$3"
  error="${temp_dir}/admission-error"
  if ! kubectl create --dry-run=server --output=json --filename="${pod}" \
    --as="${team_user}" --as-group="${team_group}" \
    >"${admitted}" 2>"${error}"; then
    printf '%s unexpectedly failed:\n' "${description}" >&2
    cat "${error}" >&2
    return 1
  fi
}

trap cleanup EXIT HUP INT TERM

kubectl get namespace "${namespace}" --output=json \
  | jq --exit-status '
    .metadata.labels.team == "examples" and
    .metadata.labels["kueue.x-k8s.io/managed"] == "true" and
    .metadata.labels["pod-security.kubernetes.io/audit"] == "restricted" and
    .metadata.labels["pod-security.kubernetes.io/audit-version"] == "latest" and
    .metadata.labels["pod-security.kubernetes.io/warn"] == "restricted" and
    .metadata.labels["pod-security.kubernetes.io/warn-version"] == "latest" and
    (.metadata.labels | has("pod-security.kubernetes.io/enforce") | not) and
    (.metadata.labels | has("pod-security.kubernetes.io/enforce-version") | not)
  ' >/dev/null

if [ "$(kubectl auth can-i create pods --namespace="${namespace}" \
  --as="${team_user}" --as-group="${team_group}")" != yes ]; then
  printf 'the examples team identity cannot create Pods in %s\n' "${namespace}" >&2
  exit 1
fi

expect_denied 'team namespace native enforcement takeover' "${profile_message}" \
  kubectl label namespace "${namespace}" \
  pod-security.kubernetes.io/enforce=restricted \
  --dry-run=server --overwrite --output=name

expect_denied 'team namespace Kyverno selector removal' "${profile_message}" \
  kubectl label namespace "${namespace}" \
  kueue.x-k8s.io/managed- --dry-run=server --output=name

jq --null-input --arg image "${busybox_image}" --arg namespace "${namespace}" '
  {
    apiVersion:"v1",
    kind:"Pod",
    metadata:{
      name:"team-pod-security-probe",
      namespace:$namespace,
      labels:{"app.kubernetes.io/managed-by":"chainsaw"}
    },
    spec:{
      automountServiceAccountToken:false,
      restartPolicy:"Never",
      securityContext:{
        runAsGroup:1000,
        runAsNonRoot:true,
        runAsUser:1000,
        seccompProfile:{type:"RuntimeDefault"}
      },
      containers:[{
        name:"probe",
        image:$image,
        command:["sh","-c","true"],
        resources:{
          requests:{cpu:"1m",memory:"4Mi"},
          limits:{cpu:"50m",memory:"16Mi"}
        },
        securityContext:{
          allowPrivilegeEscalation:false,
          capabilities:{drop:["ALL"]},
          readOnlyRootFilesystem:true,
          runAsGroup:1000,
          runAsNonRoot:true,
          runAsUser:1000
        }
      }]
    }
  }
' >"${safe_pod}"

expect_pod_allowed 'restricted team Pod' "${safe_pod}"
jq '.metadata.name = "team-user-namespace-safe" | .spec.hostUsers = false' \
  "${safe_pod}" >"${temp_dir}/user-namespace-safe.json"
expect_pod_allowed 'non-root team Pod using a user namespace' \
  "${temp_dir}/user-namespace-safe.json"

jq '
  .metadata.name = "team-hostpath-denied" |
  .spec.volumes = [{name:"host",hostPath:{path:"/"}}]
' "${safe_pod}" >"${temp_dir}/hostpath.json"
expect_pod_denied 'team hostPath Pod' "${pod_security_policy}" \
  "${temp_dir}/hostpath.json"

jq '
  .metadata.name = "team-root-denied" |
  .spec.securityContext.runAsNonRoot = false |
  .spec.securityContext.runAsUser = 0
' "${safe_pod}" >"${temp_dir}/root.json"
expect_pod_denied 'team root Pod' "${pod_security_policy}" \
  "${temp_dir}/root.json"

jq '
  .metadata.name = "team-capability-denied" |
  .spec.containers[0].securityContext.capabilities.add = ["SYS_ADMIN"]
' "${safe_pod}" >"${temp_dir}/capability.json"
expect_pod_denied 'team capability-add Pod' "${pod_security_policy}" \
  "${temp_dir}/capability.json"

jq '
  .metadata.name = "team-user-namespace-root-denied" |
  .spec.hostUsers = false |
  .spec.securityContext.runAsNonRoot = false |
  .spec.securityContext.runAsUser = 0 |
  .spec.containers[0].securityContext.runAsNonRoot = false |
  .spec.containers[0].securityContext.runAsUser = 0
' "${safe_pod}" >"${temp_dir}/user-namespace-root.json"
expect_pod_denied 'team user-namespace root Pod' "${user_namespace_message}" \
  "${temp_dir}/user-namespace-root.json"

jq '
  .metadata.name = "team-procmount-denied" |
  .spec.hostUsers = false |
  .spec.containers[0].securityContext.procMount = "Unmasked"
' "${safe_pod}" >"${temp_dir}/procmount.json"
expect_pod_denied 'team non-default procMount Pod' "${pod_security_policy}" \
  "${temp_dir}/procmount.json"

if [ "$(kubectl auth can-i create pods --namespace="${gpu_namespace}" \
  --as="${team_user}" --as-group="${team_group}")" != yes ]; then
  printf 'the examples team identity cannot create Pods in %s\n' \
    "${gpu_namespace}" >&2
  exit 1
fi

jq --arg namespace "${gpu_namespace}" '
  .metadata.name = "team-coder-gpu-admitted" |
  .metadata.namespace = $namespace |
  .spec.serviceAccountName = "coder-workspace" |
  .spec.tolerations = [{
    key:"nvidia.com/gpu",
    operator:"Equal",
    value:"present",
    effect:"NoSchedule"
  }] |
  .spec.containers += [(.spec.containers[0] | .name = "cpu-sidecar")] |
  .spec.containers[0].name = "gpu-workspace" |
  .spec.containers[0].resources.requests["nvidia.com/gpu"] = "1" |
  .spec.containers[0].resources.limits["nvidia.com/gpu"] = "1"
' "${safe_pod}" >"${temp_dir}/gpu.json"

admit_pod_json 'Coder Pod with a positive GPU request' \
  "${temp_dir}/gpu.json" "${temp_dir}/gpu-admitted.json"

jq --exit-status '
  .spec.serviceAccountName == "coder-workspace" and
  ([.spec.initContainers[]? | select(
    .name == "parcagpu-library" and
    .image == "ghcr.io/parca-dev/parcagpu:0.3.2@sha256:23ac8c02dcf974b290b48daa274a033db5e9e710b655605e50e90fbd89f37561"
  )] | length) == 1 and
  ([.spec.volumes[]? | select(
    .name == "parcagpu-library" and .emptyDir.sizeLimit == "8Mi"
  )] | length) == 1 and
  (.spec.containers[] | select(.name == "gpu-workspace") |
    .resources.requests["nvidia.com/gpu"] == "1" and
    .resources.limits["nvidia.com/gpu"] == "1" and
    .securityContext.capabilities.drop == ["ALL"] and
    .securityContext.capabilities.add == ["PERFMON"] and
    ([.env[]? | select(
      .name == "CUDA_INJECTION64_PATH" and
      .value == "/opt/parcagpu/libparcagpucupti.so"
    )] | length) == 1 and
    ([.env[]? | select(
      .name == "PARCAGPU_PC_SAMPLING_RATE" and .value == "100"
    )] | length) == 1 and
    ([.volumeMounts[]? | select(
      .name == "parcagpu-library" and
      .mountPath == "/opt/parcagpu" and .readOnly
    )] | length) == 1) and
  (.spec.containers[] | select(.name == "cpu-sidecar") |
    ([.securityContext.capabilities.add[]? | select(. == "PERFMON")] | length) == 0 and
    ([.env[]? | select(
      .name == "CUDA_INJECTION64_PATH" or
      .name == "PARCAGPU_PC_SAMPLING_RATE"
    )] | length) == 0)
' "${temp_dir}/gpu-admitted.json" >/dev/null

jq '
  .metadata.name = "team-coder-perfmon-without-gpu-denied" |
  del(
    .spec.containers[0].resources.requests["nvidia.com/gpu"],
    .spec.containers[0].resources.limits["nvidia.com/gpu"]
  ) |
  .spec.containers[0].securityContext.capabilities.add = ["PERFMON"]
' "${temp_dir}/gpu.json" >"${temp_dir}/perfmon-without-gpu.json"
expect_pod_denied 'Coder Pod with PERFMON but no positive GPU request' \
  "${parcagpu_message}" "${temp_dir}/perfmon-without-gpu.json"

trap - EXIT HUP INT TERM
cleanup
