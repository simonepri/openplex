#!/bin/sh
# Verifies RBAC authorization allows team members to create jobs and rayjobs in team workloads while denying non-members to defend tenant isolation.

set -eu

namespace="team-examples-workloads"
workspaces_namespace="workspaces"
team_slug="examples"

team_group="cluster:group:team:${team_slug}"
team_dev_group="cluster:group:team:${team_slug}:dev"
other_group="cluster:group:team:other"
fixture_group="cluster:group:team:fixtureteam"

team_user="cluster:user:dev@local.internal"
non_member_user="cluster:user:other@local.internal"

check_can_i() {
  expected="$1"
  verb="$2"
  resource="$3"
  ns="$4"
  user="$5"
  group="${6:-}"

  if [ -n "${group}" ]; then
    actual="$(kubectl auth can-i "${verb}" "${resource}" --namespace="${ns}" --as="${user}" --as-group="${group}")"
    cmd="kubectl auth can-i ${verb} ${resource} --namespace=${ns} --as=${user} --as-group=${group}"
  else
    actual="$(kubectl auth can-i "${verb}" "${resource}" --namespace="${ns}" --as="${user}")"
    cmd="kubectl auth can-i ${verb} ${resource} --namespace=${ns} --as=${user}"
  fi

  if [ "${actual}" != "${expected}" ]; then
    printf 'FAIL: expected %s for: %s, got: %s\n' "${expected}" "${cmd}" "${actual}" >&2
    return 1
  fi
  printf 'OK: %s -> %s\n' "${cmd}" "${actual}"
}

check_can_i "yes" "create" "jobs" "${namespace}" "${team_user}" "${team_group}"
check_can_i "yes" "create" "rayjobs" "${namespace}" "${team_user}" "${team_group}"
check_can_i "yes" "create" "rayclusters" "${namespace}" "${team_user}" "${team_group}"

check_can_i "yes" "create" "jobs" "${namespace}" "${team_user}" "${team_dev_group}"
check_can_i "yes" "create" "rayjobs" "${namespace}" "${team_user}" "${team_dev_group}"
check_can_i "yes" "create" "rayclusters" "${namespace}" "${team_user}" "${team_dev_group}"

check_can_i "no" "create" "jobs" "${namespace}" "${non_member_user}" "${other_group}"
check_can_i "no" "create" "rayjobs" "${namespace}" "${non_member_user}" "${other_group}"
check_can_i "no" "create" "rayclusters" "${namespace}" "${non_member_user}" "${other_group}"

check_can_i "no" "create" "jobs" "${namespace}" "${non_member_user}" "${fixture_group}"
check_can_i "no" "create" "rayjobs" "${namespace}" "${non_member_user}" "${fixture_group}"
check_can_i "no" "create" "rayclusters" "${namespace}" "${non_member_user}" "${fixture_group}"

check_can_i "no" "create" "jobs" "${namespace}" "${team_user}" ""
check_can_i "no" "create" "rayjobs" "${namespace}" "${team_user}" ""
check_can_i "no" "create" "rayclusters" "${namespace}" "${team_user}" ""

check_can_i "no" "create" "jobs" "${workspaces_namespace}" "${team_user}" "${team_group}"
check_can_i "no" "create" "rayjobs" "${workspaces_namespace}" "${team_user}" "${team_group}"
check_can_i "no" "create" "rayclusters" "${workspaces_namespace}" "${team_user}" "${team_group}"

# Assert Kyverno admission enforcement if the test environment supports admission checks.
probe_json='{"apiVersion":"batch/v1","kind":"Job","metadata":{"name":"probe-admission","namespace":"'"${namespace}"'"},"spec":{"template":{"spec":{"restartPolicy":"Never","containers":[{"name":"probe","image":"busybox:latest","command":["true"]}]}}}}'

if printf '%s' "${probe_json}" | kubectl create --dry-run=server --filename=- >/dev/null 2>&1; then
  # Canonical naming (<pipeline>-<user>-<run-id>): valid user submission must be admitted.
  valid_job='{"apiVersion":"batch/v1","kind":"Job","metadata":{"name":"batch-dev-run1","namespace":"'"${namespace}"'","labels":{"pipeline":"batch","run-id":"run1"}},"spec":{"template":{"spec":{"restartPolicy":"Never","containers":[{"name":"worker","image":"busybox:latest","command":["true"]}]}}}}'
  if ! printf '%s' "${valid_job}" | kubectl create --dry-run=server --filename=- --as="${team_user}" --as-group="${team_group}" >/dev/null 2>&1; then
    printf 'FAIL: Kyverno unexpectedly rejected canonical user Job\n' >&2
    exit 1
  fi

  # Non-canonical naming: user submission must be rejected by coder-workload-run-identity.
  invalid_job='{"apiVersion":"batch/v1","kind":"Job","metadata":{"name":"invalid-job-name","namespace":"'"${namespace}"'","labels":{"pipeline":"batch","run-id":"run1"}},"spec":{"template":{"spec":{"restartPolicy":"Never","containers":[{"name":"worker","image":"busybox:latest","command":["true"]}]}}}}'
  if printf '%s' "${invalid_job}" | kubectl create --dry-run=server --filename=- --as="${team_user}" --as-group="${team_group}" >/dev/null 2>&1; then
    printf 'FAIL: Kyverno unexpectedly admitted non-canonical Job name\n' >&2
    exit 1
  fi
  printf 'OK: Kyverno coder-workload-run-identity admission enforcement verified\n'
fi
