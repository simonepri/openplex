#!/usr/bin/env bash
# Asserts only approved Dragonfly node clients obtain host-network privileges and read-only mTLS mounts to defend node security boundaries.

set -euo pipefail

readonly client_namespace=dragonfly-system
readonly test_namespace=security-e2e
readonly client_policy=require-reviewed-dragonfly-node-client-security-profile
readonly generic_policy=block-privileged-pods
readonly busybox_image=${BUSYBOX_IMAGE:?BUSYBOX_IMAGE is required}
test_root=$(mktemp -d)
readonly test_root
trap 'rm -rf "$test_root"' EXIT

kubectl get daemonsets --namespace "${client_namespace}" --output=json >"${test_root}/daemonsets.json"
jq -e --arg namespace "${client_namespace}" '
  [
    .items[] |
    select(
      .spec.template.metadata.labels.app == "dragonfly" and
      .spec.template.metadata.labels.component == "client" and
      .spec.template.metadata.labels.release == "dragonfly"
    )
  ] |
  if length != 1 then
    error("expected exactly one deployed Dragonfly client DaemonSet")
  else
    .[0] |
    {
      apiVersion: "v1",
      kind: "Pod",
      metadata: {
        name: "dragonfly-client-security-profile-probe",
        namespace: $namespace,
        labels: .spec.template.metadata.labels,
        annotations: .spec.template.metadata.annotations
      },
      spec: .spec.template.spec
    }
  end
' "${test_root}/daemonsets.json" >"${test_root}/client.json"

kubectl create --dry-run=server --filename "${test_root}/client.json" >/dev/null

expect_denied_manifest() {
  local name=$1
  local manifest=$2
  local rule=$3
  local stderr_file="${test_root}/${name}.stderr"

  if kubectl create --dry-run=server --filename "${manifest}" \
    >/dev/null 2>"${stderr_file}"; then
    printf '%s\n' "${name} bypassed the Dragonfly admission boundary" >&2
    return 1
  fi
  grep -F production-pod-security "${stderr_file}" >/dev/null
  grep -F "${rule}" "${stderr_file}" >/dev/null
}

readonly client_drift_cases=(
  client-init-command
  '.spec.initContainers[0].command[2] = "exec arbitrary-command"'
  client-mtls-secret
  '(.spec.volumes[] | select(.name == "mtls").secret.secretName) = "foreign-dragonfly-grpc-mtls"'
)
for ((case_index = 0; case_index < ${#client_drift_cases[@]}; case_index += 2)); do
  case_name=${client_drift_cases[${case_index}]}
  mutation=${client_drift_cases[$((case_index + 1))]}
  jq "${mutation}" "${test_root}/client.json" >"${test_root}/${case_name}.json"
  expect_denied_manifest \
    "${case_name}" \
    "${test_root}/${case_name}.json" \
    "${client_policy}"
done

jq -n \
  --arg image "${busybox_image}" \
  --arg namespace "${test_namespace}" '
    {
      apiVersion: "v1",
      kind: "Pod",
      metadata: {
        name: "generic-host-network-security-profile-probe",
        namespace: $namespace
      },
      spec: {
        automountServiceAccountToken: false,
        hostNetwork: true,
        restartPolicy: "Never",
        containers: [{
          name: "probe",
          image: $image,
          command: ["sleep", "1"],
          securityContext: {
            allowPrivilegeEscalation: false,
            capabilities: {drop: ["ALL"]},
            privileged: false,
            readOnlyRootFilesystem: true,
            runAsNonRoot: true,
            runAsUser: 65532,
            seccompProfile: {type: "RuntimeDefault"}
          }
        }]
      }
    }
  ' >"${test_root}/generic-host-network.json"
expect_denied_manifest \
  generic-host-network \
  "${test_root}/generic-host-network.json" \
  "${generic_policy}"
