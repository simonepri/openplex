#!/bin/sh
# Asserts team pod placement remains cloud-portable while allowing in-place pod updates to defend scheduling portability contracts.

set -eu

busybox_image="${BUSYBOX_IMAGE:?busybox image is required}"
expect_avx2="${EXPECT_AVX2:-false}"
aws_accelerator_label_prefix=k8s.amazonaws.com
script_directory="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
repository_root="$(git -C "${script_directory}" rev-parse --show-toplevel)"
namespace=team-examples-workloads
test_root="$(mktemp -d)"
job="${test_root}/job.json"
ray_job="${test_root}/ray-job.json"
scheduled_job=portable-placement-scheduled-$$

cleanup() {
  team delete job "${scheduled_job}" --ignore-not-found=true \
    --wait=false >/dev/null
  rm -rf -- "${test_root}"
}
trap cleanup EXIT HUP INT TERM

team() {
  kubectl --namespace="${namespace}" \
    --as=conformance-user \
    --as-group=cluster:group:team:examples "$@"
}

expect_denied() {
  expected=$1
  manifest=$2
  set +e
  output="$(team create --dry-run=server --output=name --filename="${manifest}" 2>&1)"
  status=$?
  set -e
  if [ "${status}" -eq 0 ]; then
    printf 'Admission unexpectedly accepted infrastructure-owned placement.\n' >&2
    return 1
  fi
  if ! printf '%s\n' "${output}" | grep -F "${expected}" >/dev/null; then
    printf 'Admission failed outside the expected policy:\n%s\n' "${output}" >&2
    return 1
  fi
}

job_fixture="${repository_root}/src/infra/definitions/conformance/fixtures/scheduling/workload-class-agreement.test.k8s.yaml"
sed -e "s|image: (\$values.busyboxImage)|image: ${busybox_image}|" \
  "${job_fixture}" | kubectl create --dry-run=client --filename=- --output=json >"${job}"

jq --arg name "${scheduled_job}" '
  .metadata.name = $name |
  .spec.activeDeadlineSeconds = 180 |
  .spec.template.metadata.labels += .metadata.labels |
  .spec.template.metadata.labels["kueue.x-k8s.io/queue-name"] = .metadata.labels["availability-class"] |
  .spec.template.spec.containers[0].command = ["sleep", "180"]
' "${job}" >"${test_root}/scheduled-job.json"
team create --filename="${test_root}/scheduled-job.json" >/dev/null

jq '
  .metadata.name = "portable-placement-allowed" |
  .spec.template.spec.nodeSelector = {
    "kubernetes.io/arch":"amd64"
  } |
  .spec.template.spec.affinity.nodeAffinity = {
    requiredDuringSchedulingIgnoredDuringExecution:{
      nodeSelectorTerms:[{matchExpressions:[{
        key:"kubernetes.io/arch",
        operator:"In",
        values:["amd64"]
      }]}]
    },
    preferredDuringSchedulingIgnoredDuringExecution:[{
      weight:1,
      preference:{matchExpressions:[{
        key:"kubernetes.io/os",
        operator:"In",
        values:["linux"]
      }]}
    }]
  } |
  .spec.template.spec.topologySpreadConstraints = [
    {
      maxSkew:1,
      topologyKey:"kubernetes.io/hostname",
      whenUnsatisfiable:"ScheduleAnyway",
      labelSelector:{matchLabels:{"app.kubernetes.io/component":"class-agreement"}}
    },
    {
      maxSkew:1,
      topologyKey:"topology.kubernetes.io/region",
      whenUnsatisfiable:"ScheduleAnyway",
      labelSelector:{matchLabels:{"app.kubernetes.io/component":"class-agreement"}}
    },
    {
      maxSkew:1,
      topologyKey:"topology.kubernetes.io/zone",
      whenUnsatisfiable:"ScheduleAnyway",
      labelSelector:{matchLabels:{"app.kubernetes.io/component":"class-agreement"}}
    }
  ]
' "${job}" >"${test_root}/allowed-job.json"
team create --dry-run=server --output=name \
  --filename="${test_root}/allowed-job.json" >/dev/null

while read -r key value; do
  jq --arg key "${key}" --arg value "${value}" '
    .metadata.name = "portable-placement-denied" |
    .spec.template.spec.nodeSelector = {($key):$value}
  ' "${job}" >"${test_root}/denied-job.json"
  expect_denied '[rule:portable-node-placement]' "${test_root}/denied-job.json"
done <<EOF
beta.kubernetes.io/instance-type m7i.2xlarge
cloud.google.com/gke-nodepool workload-pool
cloud.google.com/machine-family n2
eks.amazonaws.com/nodegroup workload-pool
feature.node.kubernetes.io/cpu-cpuid.AVX2 true
failure-domain.beta.kubernetes.io/region example-region
failure-domain.beta.kubernetes.io/zone example-zone
karpenter.k8s.aws/instance-category m
karpenter.k8s.aws/instance-family m7i
karpenter.k8s.aws/instance-generation 7
karpenter.k8s.aws/instance-size 2xlarge
karpenter.k8s.aws/instance-type m7i.2xlarge
karpenter.k8s.gcp/instance-cpu-count 8
karpenter.sh/capacity-type spot
karpenter.sh/nodepool workload
${aws_accelerator_label_prefix}/accelerator nvidia-tesla-t4
node.kubernetes.io/instance-type m7i.2xlarge
nvidia.com/gpu.product NVIDIA-L4
topology.kubernetes.io/region example-region
topology.kubernetes.io/zone example-zone
EOF

jq '
  .metadata.name = "portable-placement-required-denied" |
  .spec.template.spec.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution = {
    nodeSelectorTerms:[{matchExpressions:[{
      key:"karpenter.k8s.aws/instance-family",
      operator:"In",
      values:["m7i"]
    }]}]
  }
' "${job}" >"${test_root}/required-denied-job.json"
expect_denied '[rule:portable-node-placement]' "${test_root}/required-denied-job.json"

jq '
  .metadata.name = "location-affinity-required-denied" |
  .spec.template.spec.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution = {
    nodeSelectorTerms:[{matchExpressions:[{
      key:"topology.kubernetes.io/zone",
      operator:"In",
      values:["example-zone"]
    }]}]
  }
' "${job}" >"${test_root}/location-required-denied-job.json"
expect_denied '[rule:portable-node-placement]' \
  "${test_root}/location-required-denied-job.json"

jq '
  .metadata.name = "raw-node-feature-affinity-denied" |
  .spec.template.spec.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution = {
    nodeSelectorTerms:[{matchExpressions:[{
      key:"feature.node.kubernetes.io/cpu-cpuid.AVX2",
      operator:"In",
      values:["true"]
    }]}]
  }
' "${job}" >"${test_root}/node-feature-affinity-denied-job.json"
expect_denied '[rule:portable-node-placement]' \
  "${test_root}/node-feature-affinity-denied-job.json"

jq '
  .metadata.name = "portable-placement-preferred-denied" |
  .spec.template.spec.affinity.nodeAffinity.preferredDuringSchedulingIgnoredDuringExecution = [{
    weight:1,
    preference:{matchExpressions:[{
      key:"karpenter.k8s.aws/instance-family",
      operator:"In",
      values:["m7i"]
    }]}
  }]
' "${job}" >"${test_root}/preferred-denied-job.json"
expect_denied '[rule:portable-node-placement]' "${test_root}/preferred-denied-job.json"

jq '
  .metadata.name = "location-affinity-preferred-denied" |
  .spec.template.spec.affinity.nodeAffinity.preferredDuringSchedulingIgnoredDuringExecution = [{
    weight:1,
    preference:{matchExpressions:[{
      key:"failure-domain.beta.kubernetes.io/region",
      operator:"In",
      values:["example-region"]
    }]}
  }]
' "${job}" >"${test_root}/location-preferred-denied-job.json"
expect_denied '[rule:portable-node-placement]' \
  "${test_root}/location-preferred-denied-job.json"

jq '
  .metadata.name = "portable-placement-topology-denied" |
  .spec.template.spec.topologySpreadConstraints = [{
    maxSkew:1,
    topologyKey:"node.kubernetes.io/instance-type",
    whenUnsatisfiable:"ScheduleAnyway"
  }]
' "${job}" >"${test_root}/topology-denied-job.json"
expect_denied '[rule:portable-node-placement]' "${test_root}/topology-denied-job.json"

jq '
  .metadata.name = "direct-node-name-denied" |
  .spec.template.spec.nodeName = "worker-1"
' "${job}" >"${test_root}/node-name-denied-job.json"
expect_denied '[rule:portable-node-placement]' "${test_root}/node-name-denied-job.json"

jq '
  .metadata.name = "hostname-node-selector-denied" |
  .spec.template.spec.nodeSelector = {"kubernetes.io/hostname":"worker-1"}
' "${job}" >"${test_root}/hostname-selector-denied-job.json"
expect_denied '[rule:portable-node-placement]' "${test_root}/hostname-selector-denied-job.json"

jq '
  .metadata.name = "hostname-affinity-denied" |
  .spec.template.spec.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution = {
    nodeSelectorTerms:[{matchExpressions:[{
      key:"kubernetes.io/hostname",
      operator:"In",
      values:["worker-1"]
    }]}]
  }
' "${job}" >"${test_root}/hostname-affinity-denied-job.json"
expect_denied '[rule:portable-node-placement]' "${test_root}/hostname-affinity-denied-job.json"

jq '
  .metadata.name = "node-name-match-field-required-denied" |
  .spec.template.spec.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution = {
    nodeSelectorTerms:[{matchFields:[{
      key:"metadata.name",
      operator:"In",
      values:["worker-1"]
    }]}]
  }
' "${job}" >"${test_root}/required-match-field-denied-job.json"
expect_denied '[rule:portable-node-placement]' \
  "${test_root}/required-match-field-denied-job.json"

jq '
  .metadata.name = "node-name-match-field-preferred-denied" |
  .spec.template.spec.affinity.nodeAffinity.preferredDuringSchedulingIgnoredDuringExecution = [{
    weight:1,
    preference:{matchFields:[{
      key:"metadata.name",
      operator:"In",
      values:["worker-1"]
    }]}
  }]
' "${job}" >"${test_root}/preferred-match-field-denied-job.json"
expect_denied '[rule:portable-node-placement]' \
  "${test_root}/preferred-match-field-denied-job.json"

jq '
  .metadata.name = "unknown-cpu-capability-denied" |
  .spec.template.spec.nodeSelector = {"cpu-capability.unknown":"true"}
' "${job}" >"${test_root}/unknown-capability-denied-job.json"
expect_denied '[rule:cpu-capability-offered]' "${test_root}/unknown-capability-denied-job.json"

jq '
  .metadata.name = "false-cpu-capability-denied" |
  .spec.template.spec.nodeSelector = {"cpu-capability.avx2":"false"}
' "${job}" >"${test_root}/false-capability-denied-job.json"
expect_denied '[rule:cpu-capability-offered]' "${test_root}/false-capability-denied-job.json"

jq '
  .metadata.name = "offered-cpu-capability" |
  .spec.template.spec.nodeSelector = {"cpu-capability.avx2":"true"}
' "${job}" >"${test_root}/offered-capability-job.json"
if [ "${expect_avx2}" = true ]; then
  team create --dry-run=server --output=name \
    --filename="${test_root}/offered-capability-job.json" >/dev/null
else
  expect_denied '[rule:cpu-capability-offered]' \
    "${test_root}/offered-capability-job.json"
fi

jq '
  .metadata.name = "affinity-cpu-capability-denied" |
  .spec.template.spec.affinity.nodeAffinity.preferredDuringSchedulingIgnoredDuringExecution = [{
    weight:1,
    preference:{matchExpressions:[{
      key:"cpu-capability.avx2",
      operator:"In",
      values:["true"]
    }]}
  }]
' "${job}" >"${test_root}/affinity-capability-denied-job.json"
expect_denied '[rule:cpu-capability-node-selector-only]' "${test_root}/affinity-capability-denied-job.json"

jq '
  .metadata.name = "topology-cpu-capability-denied" |
  .spec.template.spec.topologySpreadConstraints = [{
    maxSkew:1,
    topologyKey:"cpu-capability.avx2",
    whenUnsatisfiable:"ScheduleAnyway"
  }]
' "${job}" >"${test_root}/topology-capability-denied-job.json"
expect_denied '[rule:cpu-capability-node-selector-only]' \
  "${test_root}/topology-capability-denied-job.json"

ray_fixture="${repository_root}/src/examples/ray_data/deployment/ray-data.k8s.yaml"
yq -o=json 'select(.kind == "RayJob")' "${ray_fixture}" | jq \
  --arg image "${busybox_image}" \
  --arg namespace "${namespace}" '
    .metadata.name = "portable-placement-ray" |
    .metadata.namespace = $namespace |
    .spec.shutdownAfterJobFinishes = true |
    .spec.rayClusterSpec.headGroupSpec.template.spec.containers[].image = $image |
    .spec.rayClusterSpec.workerGroupSpecs[].template.spec.containers[].image = $image |
    .spec.submitterPodTemplate.spec.containers[].image = $image
  ' >"${ray_job}"

jq '
  .spec.submitterPodTemplate.spec.nodeSelector = {
    "kubernetes.io/arch":"amd64"
  } |
  .spec.rayClusterSpec.headGroupSpec.template.spec.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution = {
    nodeSelectorTerms:[{matchExpressions:[{
      key:"kubernetes.io/arch",
      operator:"In",
      values:["amd64"]
    }]}]
  } |
  .spec.rayClusterSpec.workerGroupSpecs[0].template.spec.affinity.nodeAffinity.preferredDuringSchedulingIgnoredDuringExecution = [{
    weight:1,
    preference:{matchExpressions:[{
      key:"kubernetes.io/os",
      operator:"In",
      values:["linux"]
    }]}
  }]
' "${ray_job}" >"${test_root}/allowed-ray-job.json"
team create --dry-run=server --output=name \
  --filename="${test_root}/allowed-ray-job.json" >/dev/null

jq '
  .spec.rayClusterSpec.workerGroupSpecs[0].template.spec.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution = {
    nodeSelectorTerms:[{matchFields:[{
      key:"metadata.name",
      operator:"In",
      values:["worker-1"]
    }]}]
  }
' "${ray_job}" >"${test_root}/match-field-denied-ray-job.json"
expect_denied '[rule:portable-node-placement]' \
  "${test_root}/match-field-denied-ray-job.json"

jq '
  .spec.rayClusterSpec.workerGroupSpecs[0].template.spec.affinity.nodeAffinity.preferredDuringSchedulingIgnoredDuringExecution = [{
    weight:1,
    preference:{matchExpressions:[{
      key:"cloud.google.com/gke-nodepool",
      operator:"In",
      values:["tpu"]
    }]}
  }]
' "${ray_job}" >"${test_root}/denied-ray-job.json"
expect_denied '[rule:portable-node-placement]' "${test_root}/denied-ray-job.json"

team wait pod --selector="job-name=${scheduled_job}" \
  --for=create --timeout=60s >/dev/null
team wait pod --selector="job-name=${scheduled_job}" \
  --for=condition=PodScheduled --timeout=60s >/dev/null
team get pod --selector="job-name=${scheduled_job}" --output=json \
  | jq --exit-status '
    .items | select(length == 1) | .[0] |
    select(.spec.nodeName | length > 0) |
    select(.spec.nodeSelector["kubernetes.io/hostname"] | length > 0)
  ' >"${test_root}/scheduled-pod.json"
scheduled_pod="$(jq --raw-output '.metadata.name' "${test_root}/scheduled-pod.json")"
team annotate pod "${scheduled_pod}" placement-admission=checked \
  --dry-run=server --output=json \
  | jq --exit-status --slurpfile scheduled "${test_root}/scheduled-pod.json" '
    .metadata.uid == $scheduled[0].metadata.uid and
    .spec.nodeName == $scheduled[0].spec.nodeName and
    .spec.nodeSelector["kubernetes.io/hostname"] ==
      $scheduled[0].spec.nodeSelector["kubernetes.io/hostname"] and
    .metadata.annotations["placement-admission"] == "checked"
  ' >/dev/null

jq --slurpfile scheduled "${test_root}/scheduled-pod.json" '
  {
    apiVersion:"v1",
    kind:"Pod",
    metadata:{
      name:"direct-pod-node-name-denied",
      namespace:.metadata.namespace,
      labels:.metadata.labels,
      ownerReferences:$scheduled[0].metadata.ownerReferences
    },
    spec:.spec.template.spec
  } |
  .spec.nodeName = $scheduled[0].spec.nodeName
' "${job}" >"${test_root}/node-name-denied-pod.json"
expect_denied '[rule:portable-node-placement]' "${test_root}/node-name-denied-pod.json"

jq --slurpfile scheduled "${test_root}/scheduled-pod.json" '
  .metadata.name = "direct-pod-hostname-selector-denied" |
  del(.spec.nodeName) |
  .spec.nodeSelector = {
    "kubernetes.io/hostname":$scheduled[0].spec.nodeSelector["kubernetes.io/hostname"]
  }
' "${test_root}/node-name-denied-pod.json" >"${test_root}/hostname-denied-pod.json"
expect_denied '[rule:portable-node-placement]' "${test_root}/hostname-denied-pod.json"
