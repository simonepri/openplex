#!/bin/sh
# Deletes Chainsaw-managed Ray runs and awaits graceful KubeRay resource termination to defend shared cluster capacity against leaked worker pods.

# shellcheck disable=SC2312
set -eu

run_prefix="${1:?Ray run prefix is required}"
deadline="$(($(date +%s) + 120))"
force_deadline=0
empty_observations=0

while test "${empty_observations}" -lt 3; do
  objects="$(
    kubectl get rayjobs.ray.io,rayclusters.ray.io,jobs.batch,pods \
      --namespace=team-examples-workloads --output=json \
      | jq --raw-output --arg prefix "${run_prefix}" '
        .items[] |
        select(.metadata.name | startswith($prefix)) |
        [.kind, .metadata.name, (.metadata.deletionTimestamp // "")] |
        @tsv
      '
  )"

  if test -z "${objects}"; then
    empty_observations="$((empty_observations + 1))"
    test "${empty_observations}" -eq 3 && break
  else
    empty_observations=0
    printf '%s\n' "${objects}" \
      | while IFS="$(printf '\t')" read -r kind name deletion_timestamp; do
        object="${kind}/${name}"
        if test -z "${deletion_timestamp}"; then
          kubectl delete "${object}" --namespace=team-examples-workloads \
            --ignore-not-found=true --wait=false
        elif test "${force_deadline}" -ne 0; then
          kubectl patch "${object}" --namespace=team-examples-workloads --type=merge \
            --patch='{"metadata":{"finalizers":[]}}' || true
        fi
      done
  fi

  if test "$(date +%s)" -ge "${deadline}"; then
    if test "${force_deadline}" -eq 0; then
      force_deadline="$(($(date +%s) + 30))"
    elif test "$(date +%s)" -ge "${force_deadline}"; then
      printf 'Timed out cleaning Ray resources with prefix %s.\n' \
        "${run_prefix}" >&2
      exit 1
    fi
  fi
  sleep 2
done
