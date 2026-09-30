#!/usr/bin/env bash
# Asserts privileged Rclone CSI daemon exceptions accept only pinned controller Pods and reject privilege escalations to defend node isolation.

set -euo pipefail

readonly namespace=csi-rclone
readonly daemonset=rclone-csi-node
test_root=$(mktemp -d)
readonly test_root
trap 'rm -rf "$test_root"' EXIT

kubectl get daemonset "${daemonset}" --namespace "${namespace}" --output json \
  | jq --arg namespace "${namespace}" '
    {
      apiVersion: "v1",
      kind: "Pod",
      metadata: {
        name: "rclone-csi-security-profile-probe",
        namespace: $namespace,
        labels: .spec.template.metadata.labels,
        ownerReferences: [{
          apiVersion: "apps/v1",
          blockOwnerDeletion: true,
          controller: true,
          kind: "DaemonSet",
          name: .metadata.name,
          uid: .metadata.uid
        }]
      },
      spec: .spec.template.spec
    }
  ' >"${test_root}/valid.json"

kubectl create --dry-run=server --filename "${test_root}/valid.json" >/dev/null

kubectl get serviceaccount rclone-csi-node-sa --namespace "${namespace}" --output json \
  | jq 'del(.metadata.managedFields)' >"${test_root}/valid-service-account.json"
kubectl replace --dry-run=server --filename "${test_root}/valid-service-account.json" >/dev/null

expect_denied() {
  local name=$1
  local mutation=$2
  local policy=$3
  local stderr_file="${test_root}/${name}.stderr"

  jq "${mutation}" "${test_root}/valid.json" >"${test_root}/${name}.json"
  if kubectl create --dry-run=server --filename "${test_root}/${name}.json" \
    >/dev/null 2>"${stderr_file}"; then
    printf '%s\n' "${name} mutation bypassed the Rclone CSI admission boundary" >&2
    return 1
  fi
  grep -F "${policy}" "${stderr_file}" >/dev/null
}

expect_denied label-spoof \
  'del(.metadata.labels["app.kubernetes.io/instance"])' \
  production-pod-security
expect_denied chart-identity-spoof \
  '.metadata.labels["app.kubernetes.io/managed-by"] = "team"' \
  rclone-csi-security-profile
expect_denied host-pid '.spec.hostPID = true' rclone-csi-security-profile
expect_denied node-name-bypass '.spec.nodeName = "control-plane"' rclone-csi-security-profile
expect_denied non-linux-placement \
  'del(.spec.nodeSelector["kubernetes.io/os"])' \
  rclone-csi-security-profile
expect_denied missing-nvidia-toleration \
  '.spec.tolerations |= map(select(.key != "nvidia.com/gpu"))' \
  rclone-csi-security-profile
expect_denied missing-tpu-toleration \
  '.spec.tolerations |= map(select(.key != "google.com/tpu"))' \
  rclone-csi-security-profile
expect_denied seed-placement \
  '.spec.tolerations += [{key: "dragonfly.io/seed", operator: "Equal", value: "true", effect: "NoSchedule"}]' \
  rclone-csi-security-profile
expect_denied system-placement \
  '.spec.tolerations += [{key: "CriticalAddonsOnly", operator: "Exists", effect: "NoSchedule"}]' \
  rclone-csi-security-profile
expect_denied driver-command \
  '(.spec.containers[] | select(.name == "rclone")).command = ["/bin/sh", "-c", "sleep 600"]' \
  rclone-csi-security-profile
expect_denied sidecar-image \
  '(.spec.containers[] | select(.name == "liveness-probe")).image = "docker.io/library/busybox:latest"' \
  rclone-csi-security-profile
expect_denied host-volume \
  '(.spec.volumes[] | select(.name == "pods-mount-dir")).hostPath.path = "/"' \
  rclone-csi-security-profile
expect_denied host-mount \
  '(.spec.containers[] | select(.name == "rclone") | .volumeMounts[] | select(.name == "pods-mount-dir")).mountPath = "/host"' \
  rclone-csi-security-profile
expect_denied service-account-token \
  '.spec.automountServiceAccountToken = true' \
  rclone-csi-security-profile
expect_denied init-container \
  '.spec.initContainers = [.spec.containers[0]]' \
  rclone-csi-security-profile
expect_denied owner-reference \
  '.metadata.ownerReferences = []' \
  rclone-csi-security-profile

jq '.metadata.annotations["eks.amazonaws.com/role-arn"] = "arn:aws:iam::000000000000:role/team"' \
  "${test_root}/valid-service-account.json" >"${test_root}/service-account-cloud-role.json"
if kubectl replace --dry-run=server --filename "${test_root}/service-account-cloud-role.json" \
  >/dev/null 2>"${test_root}/service-account-cloud-role.stderr"; then
  printf '%s\n' "cloud role annotation bypassed the Rclone CSI ServiceAccount boundary" >&2
  exit 1
fi
grep -F rclone-csi-service-account-profile "${test_root}/service-account-cloud-role.stderr" >/dev/null
