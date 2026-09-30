#!/bin/sh
# Drives pre-authenticated Tailscale key lifecycle tests in workspaces and cleans up test node registrations to defend network enrollment safety.

# shellcheck disable=SC2310
set -eu

script_directory="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
repository_root="$(git rev-parse --show-toplevel)"
ctrl_kubeconfig="${TEST_CTRL_KUBECONFIG:-${repository_root}/.tmp/kubeconfigs/ctrl-eaws-lh1.yaml}"
probe_script="${script_directory}/workspace_key_lifecycle_probe.sh"
probe_prefix="key-probe-$(date +%s)-$$"
probe_names="${probe_prefix}-used ${probe_prefix}-reuse ${probe_prefix}-expired ${probe_prefix}-fresh"

workspaces="$(
  kubectl get deployments --all-namespaces \
    --selector=app.kubernetes.io/name=coder-workspace --output=json
)"
workspace_count="$(printf '%s' "${workspaces}" | jq '.items | length')"
if [ "${workspace_count}" -ne 1 ]; then
  printf 'Expected one running Coder workspace, found %s.\n' \
    "${workspace_count}" >&2
  exit 1
fi

namespace="$(printf '%s' "${workspaces}" | jq --raw-output '.items[0].metadata.namespace')"
instance="$(
  printf '%s' "${workspaces}" \
    | jq --exit-status --raw-output \
      '.items[0].metadata.labels["app.kubernetes.io/instance"] | select(length > 0)'
)"
pod="$(
  kubectl get pods --namespace="${namespace}" \
    --selector=app.kubernetes.io/name=coder-workspace \
    --field-selector=status.phase=Running --output=json \
    | jq --exit-status --raw-output \
      --arg instance "${instance}" \
      '.items[] | select(.metadata.labels["app.kubernetes.io/instance"] == $instance) | .metadata.name' \
    | head -n 1
)"
test -n "${pod}"

headscale_pod="$(
  kubectl --kubeconfig="${ctrl_kubeconfig}" get pods --namespace=headscale \
    --selector=app.kubernetes.io/name=headscale \
    --field-selector=status.phase=Running --output=json \
    | jq --raw-output \
      'if (.items | length) == 1 then .items[0].metadata.name else empty end'
)"
if [ -z "${headscale_pod}" ]; then
  printf 'Expected one running Headscale pod in ctrl-eaws-lh1.\n' >&2
  exit 1
fi

delete_probe_nodes() {
  nodes="$(
    kubectl --kubeconfig="${ctrl_kubeconfig}" exec \
      --namespace=headscale "pod/${headscale_pod}" \
      --container=workspace-enrollment -- \
      /ko-app/headscale --config /etc/headscale/config.yaml \
      nodes list --output json
  )"
  for name in ${probe_names}; do
    printf '%s' "${nodes}" \
      | jq --raw-output --arg name "${name}" \
        '.[] | select(.given_name == $name) | .id' \
      | while IFS= read -r identifier; do
        kubectl --kubeconfig="${ctrl_kubeconfig}" exec \
          --namespace=headscale "pod/${headscale_pod}" \
          --container=workspace-enrollment -- \
          /ko-app/headscale --config /etc/headscale/config.yaml \
          nodes delete --identifier "${identifier}" --force >/dev/null
      done
  done
}

cleanup() {
  status="$?"
  trap - EXIT HUP INT TERM
  if ! delete_probe_nodes; then
    printf 'Failed to remove the exact Headscale lifecycle probe nodes.\n' >&2
    test "${status}" -ne 0 || status=1
  fi
  exit "${status}"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

kubectl exec --namespace="${namespace}" "pod/${pod}" --container=tailnet \
  --stdin -- sh -s -- "${probe_prefix}" <"${probe_script}"
