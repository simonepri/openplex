#!/usr/bin/env bash
# Tests Kueue admission polling logic, timeout escalations, and container startup verification in wait-for-admission.sh.

set -euo pipefail

subject="${1:-$(cd -- "$(dirname -- "$0")" && pwd)/wait-for-admission.sh}"
test_dir="$(mktemp -d)"
trap 'rm -rf "$test_dir"' EXIT
mkdir -p "${test_dir}/bin"
curl_log="${test_dir}/curl.log"

if grep -Eq '/[{}]/' "${subject}"; then
  printf '%s\n' 'admission probe contains an awk brace regex rejected by BusyBox' >&2
  exit 1
fi

cat >"${test_dir}/bin/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '<%s>\n' "$@" >>"${ADMISSION_PROBE_CURL_LOG:?}"

workspace_id="${TEST_WORKSPACE_ID:-123e4567-e89b-42d3-a456-426614174000}"
build_id="${TEST_BUILD_ID:-223e4567-e89b-42d3-a456-426614174000}"
scenario="${TEST_SCENARIO:-admitted_workload}"

case " $* " in
  *'/api/v1/namespaces/team-examples-workspaces/pods'*)
    container_status='{"name":"workspace","state":{"waiting":{"reason":"PodInitializing"}},"ready":false}'
    case "$scenario" in
      admitted_gate_cleared | admitted_workload | stale_deployment)
        container_status='{"name":"workspace","state":{"running":{"startedAt":"2026-09-18T18:00:00Z"}},"ready":false}'
        ;;
    esac
    if [[ "$scenario" == "unschedulable" ]]; then
      # Pod admitted by Kueue but rejected by the scheduler.
      printf '%s\n' '{"apiVersion":"v1","kind":"PodList","items":[{"metadata":{"name":"coder-pod","annotations":{"kueue.x-k8s.io/workload":"pod-coder-'"$workspace_id"'-xxxx"},"labels":{"com.coder.workspace.id":"'"$workspace_id"'","com.coder.workspace.build.id":"'"$build_id"'"}},"spec":{"schedulingGates":[]},"status":{"phase":"Pending","conditions":[{"type":"PodScheduled","status":"False","reason":"Unschedulable","message":"0/1 nodes are available: 1 Insufficient cpu."}],"containerStatuses":[{"lastState":{},"name":"workspace","ready":false,"state":{"waiting":{"reason":"ContainerCreating"}}}]}}]}'
    elif [[ "$scenario" == "admitted_gate_cleared" ]]; then
      # Pod exists and scheduling gate has already been removed by Kueue.
      printf '%s\n' '{"apiVersion":"v1","kind":"PodList","items":[{"metadata":{"name":"coder-pod","annotations":{"kueue.x-k8s.io/workload":"pod-coder-'"$workspace_id"'-xxxx"},"labels":{"com.coder.workspace.id":"'"$workspace_id"'","com.coder.workspace.build.id":"'"$build_id"'"}},"spec":{"schedulingGates":[]},"status":{"containerStatuses":['"$container_status"']}}]}'
    elif [[ "$scenario" == "not_created_yet" ]]; then
      printf '%s\n' '{"apiVersion":"v1","kind":"PodList","metadata":{"resourceVersion":"1"},"items":[]}'
    else
      # Pod exists and is waiting behind Kueue scheduling gate.
      printf '%s\n' '{"apiVersion":"v1","kind":"PodList","items":[{"metadata":{"name":"coder-pod","annotations":{"kueue.x-k8s.io/workload":"pod-coder-'"$workspace_id"'-xxxx"},"labels":{"com.coder.workspace.id":"'"$workspace_id"'","com.coder.workspace.build.id":"'"$build_id"'"}},"spec":{"schedulingGates":[{"name":"kueue.x-k8s.io/admission"}]},"status":{"containerStatuses":['"$container_status"']}}]}'
    fi
    ;;
  *'/apis/kueue.x-k8s.io/v1beta2/namespaces/team-examples-workspaces/workloads'*)
    case "$scenario" in
      admitted_workload | admitted_not_running | stale_deployment)
        printf '%s\n' '{"apiVersion":"kueue.x-k8s.io/v1beta2","kind":"Workload","metadata":{"name":"pod-coder-'"$workspace_id"'-xxxx"},"spec":{"queueName":"ha","podSets":[{"name":"main","count":1,"template":{"spec":{"containers":[{"name":"workspace","resources":{"limits":{"cpu":"3","memory":"8Gi"},"requests":{"cpu":"1","memory":"2Gi"}}}],"initContainers":[{"name":"prepare-workspace-volume"},{"name":"tailnet","restartPolicy":"Always","resources":{"limits":{"cpu":"250m","memory":"128Mi"},"requests":{"cpu":"10m","memory":"32Mi"}}},{"name":"backup-proxy","restartPolicy":"Always","resources":{"limits":{"cpu":"500m","memory":"256Mi"},"requests":{"cpu":"25m","memory":"64Mi"}}}]}}}]},"status":{"conditions":[{"type":"QuotaReserved","status":"True","reason":"QuotaReserved","message":"Quota reserved"},{"type":"Admitted","status":"True","reason":"Admitted","message":"The workload is admitted"}]}}'
        ;;
      quota_rejected)
        printf '%s\n' '{"apiVersion":"kueue.x-k8s.io/v1beta2","kind":"Workload","metadata":{"name":"pod-coder-'"$workspace_id"'-xxxx"},"spec":{"queueName":"ha","podSets":[{"name":"main","count":1,"template":{"spec":{"containers":[{"name":"workspace","resources":{"limits":{"cpu":"3","memory":"8Gi"},"requests":{"cpu":"1","memory":"2Gi"}}}],"initContainers":[{"name":"prepare-workspace-volume"},{"name":"tailnet","restartPolicy":"Always","resources":{"limits":{"cpu":"250m","memory":"128Mi"},"requests":{"cpu":"10m","memory":"32Mi"}}},{"name":"backup-proxy","restartPolicy":"Always","resources":{"limits":{"cpu":"500m","memory":"256Mi"},"requests":{"cpu":"25m","memory":"64Mi"}}}]}}}]},"status":{"conditions":[{"type":"QuotaReserved","status":"False","reason":"NoReservation","message":"Quota exhausted on queue team-examples-ha"},{"type":"Admitted","status":"False","reason":"NoReservation","message":"The workload has no reservation"}]}}'
        ;;
      misconfigured_queue)
        printf '%s\n' '{"apiVersion":"kueue.x-k8s.io/v1beta2","kind":"Workload","metadata":{"name":"pod-coder-'"$workspace_id"'-xxxx"},"spec":{"queueName":"ha","podSets":[{"name":"main","count":1,"template":{"spec":{"containers":[{"name":"workspace","resources":{"limits":{"cpu":"3","memory":"8Gi"},"requests":{"cpu":"1","memory":"2Gi"}}}],"initContainers":[{"name":"prepare-workspace-volume"},{"name":"tailnet","restartPolicy":"Always","resources":{"limits":{"cpu":"250m","memory":"128Mi"},"requests":{"cpu":"10m","memory":"32Mi"}}},{"name":"backup-proxy","restartPolicy":"Always","resources":{"limits":{"cpu":"500m","memory":"256Mi"},"requests":{"cpu":"25m","memory":"64Mi"}}}]}}}]},"status":{"conditions":[{"type":"QuotaReserved","status":"False","reason":"Misconfigured","message":"ClusterQueue team-examples-ha does not exist"},{"type":"Admitted","status":"False","reason":"NoReservation","message":"The workload has no reservation"}]}}'
        ;;
      pending_timeout)
        printf '%s\n' '{"apiVersion":"kueue.x-k8s.io/v1beta2","kind":"Workload","metadata":{"name":"pod-coder-'"$workspace_id"'-xxxx"},"spec":{"queueName":"ha","podSets":[{"name":"main","count":1,"template":{"spec":{"containers":[{"name":"workspace","resources":{"limits":{"cpu":"3","memory":"8Gi"},"requests":{"cpu":"1","memory":"2Gi"}}}],"initContainers":[{"name":"prepare-workspace-volume"},{"name":"tailnet","restartPolicy":"Always","resources":{"limits":{"cpu":"250m","memory":"128Mi"},"requests":{"cpu":"10m","memory":"32Mi"}}},{"name":"backup-proxy","restartPolicy":"Always","resources":{"limits":{"cpu":"500m","memory":"256Mi"},"requests":{"cpu":"25m","memory":"64Mi"}}}]}}}]},"status":{"conditions":[{"type":"QuotaReserved","status":"False","reason":"WaitingForQuota","message":"insufficient unused quota for cpu in flavor on-demand, 774m more needed"}]}}'
        ;;
      *)
        printf '%s\n' '{"kind":"Status","apiVersion":"v1","status":"Failure","reason":"NotFound","code":404}'
        ;;
    esac
    ;;
  *'/apis/apps/v1/namespaces/team-examples-workspaces/deployments/coder-'*)
    case "$scenario" in
      admitted_gate_cleared | admitted_workload)
        printf '%s\n' '{"apiVersion":"apps/v1","kind":"Deployment","metadata":{"generation":4},"status":{"observedGeneration":4,"replicas":1,"updatedReplicas":1,"readyReplicas":1,"availableReplicas":1}}'
        ;;
      stale_deployment)
        printf '%s\n' '{"apiVersion":"apps/v1","kind":"Deployment","metadata":{"generation":4},"status":{"observedGeneration":3,"replicas":1,"updatedReplicas":1,"readyReplicas":1,"availableReplicas":1}}'
        ;;
      *)
        printf '%s\n' '{"apiVersion":"apps/v1","kind":"Deployment","metadata":{"generation":4},"status":{"observedGeneration":4,"replicas":1,"updatedReplicas":1}}'
        ;;
    esac
    ;;
  *) exit 22 ;;
esac
EOF
chmod +x "${test_dir}/bin/curl"

kubeconfig="${test_dir}/kubeconfig"
cat >"${kubeconfig}" <<'EOF'
"apiVersion": "v1"
"clusters":
- "cluster":
    "certificate-authority-data": "Y2E="
    "server": "https://172.19.255.11:6443"
  "name": "cell-eaws-lh1"
"contexts":
- "context":
    "cluster": "cell-eaws-lh1"
    "user": "cluster:coder-provisioner:cell-eaws-lh1"
  "name": "cell-eaws-lh1"
"current-context": "cell-eaws-lh1"
"users":
- "name": "cluster:coder-provisioner:cell-eaws-lh1"
  "user":
    "client-certificate-data": "Y2VydA=="
    "client-key-data": "a2V5"
EOF

ws_id="123e4567-e89b-42d3-a456-426614174000"
build_id="223e4567-e89b-42d3-a456-426614174000"

run_probe() {
  local scenario="$1"
  local timeout="${2:-2}"
  env \
    "PATH=${test_dir}/bin:${PATH}" \
    "ADMISSION_PROBE_CURL_LOG=${curl_log}" \
    "TEST_SCENARIO=${scenario}" \
    "TEST_BUILD_ID=${build_id}" \
    "TEST_WORKSPACE_ID=${ws_id}" \
    "KUBERNETES_CONFIG_PATH=${kubeconfig}" \
    "WORKSPACE_CELL=cell-eaws-lh1" \
    "WORKSPACE_NAMESPACE=team-examples-workspaces" \
    "CODER_WORKSPACE_BUILD_ID=${build_id}" \
    "CODER_WORKSPACE_ID=${ws_id}" \
    "TIMEOUT_SECONDS=${timeout}" \
    "${subject}"
}

# 1. Test a cleared pod scheduling gate waits for the current workspace container.
output=$(run_probe "admitted_gate_cleared")
[[ ${output} =~ "Workspace pod admission confirmed" ]]
[[ ${output} =~ "Workspace container started" ]]

# 2. Test Kueue Workload admission waits for the current workspace container.
output=$(run_probe "admitted_workload")
[[ ${output} =~ "Kueue admitted workspace workload onto queue ha" ]]
[[ ${output} =~ "Workspace container started" ]]
grep -Fq 'labelSelector=com.coder.workspace.id%3D123e4567-e89b-42d3-a456-426614174000%2Ccom.coder.workspace.build.id%3D223e4567-e89b-42d3-a456-426614174000' "${curl_log}"

# 3. Test quota rejection fails fast with error on stderr.
set +e
stderr_output=$(run_probe "quota_rejected" 2>&1 >/dev/null)
status=$?
set -e
[[ ${status} -eq 1 ]]
[[ ${stderr_output} =~ "Kueue admission rejected workspace pod" ]]
[[ ${stderr_output} =~ "NoReservation" ]]
[[ ${stderr_output} =~ "Quota exhausted on queue team-examples-ha" ]]

# 4. Test missing queue misconfiguration fails fast with error on stderr.
set +e
stderr_output=$(run_probe "misconfigured_queue" 2>&1 >/dev/null)
status=$?
set -e
[[ ${status} -eq 1 ]]
[[ ${stderr_output} =~ "Misconfigured" ]]
[[ ${stderr_output} =~ "ClusterQueue team-examples-ha does not exist" ]]

# 5. Test pending admission reports quota progress and fails after the timeout.
set +e
output=$(run_probe "pending_timeout" 1 2>"${test_dir}/pending.err")
status=$?
set -e
stderr_output=$(cat "${test_dir}/pending.err")
[[ ${status} -eq 1 ]]
[[ ${stderr_output} =~ "Workspace pod was not admitted within 1s" ]]
[[ ${output} =~ "Waiting for Kueue quota on queue ha" ]]
[[ ${output} =~ "Workspace pod requests cpu=1035m memory=2144Mi" ]]
[[ ${output} =~ "Kueue reports WaitingForQuota: insufficient unused quota for cpu in flavor on-demand, 774m more needed" ]]

# 5b. Test a pod without a Kueue workload still reports why it is waiting.
set +e
output=$(run_probe "not_created_yet" 1 2>/dev/null)
set -e
[[ ${output} =~ "Waiting for Kueue to create the workspace workload" ]]

# 6. Test an admitted but unstarted workspace container reports why it waits,
#    then fails after the timeout.
set +e
output=$(run_probe "admitted_not_running" 1 2>"${test_dir}/unstarted.err")
status=$?
set -e
stderr_output=$(cat "${test_dir}/unstarted.err")
[[ ${status} -eq 1 ]]
[[ ${stderr_output} =~ "Workspace container did not start within 1s" ]]
[[ ${stderr_output} =~ "updated=1 container-running=false pod-ready=0 available=0" ]]
[[ ${output} =~ "Waiting for the workspace container" ]]
[[ ${output} =~ "Container workspace is waiting, PodInitializing" ]]

# 6b. Test an unschedulable pod reports the scheduler's verdict.
set +e
output=$(run_probe "unschedulable" 1 2>/dev/null)
set -e
[[ ${output} =~ "Not scheduled, Unschedulable: 0/1 nodes are available" ]]

# 7. Test stale ready status from the previous generation is not accepted.
set +e
stderr_output=$(run_probe "stale_deployment" 1 2>&1 >/dev/null)
status=$?
set -e
[[ ${status} -eq 1 ]]
[[ ${stderr_output} =~ "generation=4 observed=3" ]]

# 8. Test parameter validation errors.
if env -u KUBERNETES_CONFIG_PATH "${subject}" 2>/dev/null; then
  exit 1
fi

if env "KUBERNETES_CONFIG_PATH=/nonexistent" \
  "WORKSPACE_CELL=cell-eaws-lh1" \
  "WORKSPACE_NAMESPACE=team-examples-workspaces" \
  "CODER_WORKSPACE_BUILD_ID=${build_id}" \
  "CODER_WORKSPACE_ID=${ws_id}" \
  "${subject}" 2>/dev/null; then
  exit 1
fi

if env "KUBERNETES_CONFIG_PATH=${kubeconfig}" \
  "WORKSPACE_CELL=invalid-cell!" \
  "WORKSPACE_NAMESPACE=team-examples-workspaces" \
  "CODER_WORKSPACE_BUILD_ID=${build_id}" \
  "CODER_WORKSPACE_ID=${ws_id}" \
  "${subject}" 2>/dev/null; then
  exit 1
fi

if env "KUBERNETES_CONFIG_PATH=${kubeconfig}" \
  "WORKSPACE_CELL=cell-eaws-lh1" \
  "WORKSPACE_NAMESPACE=team-examples-workspaces" \
  "CODER_WORKSPACE_BUILD_ID=not-a-uuid" \
  "CODER_WORKSPACE_ID=${ws_id}" \
  "${subject}" 2>/dev/null; then
  exit 1
fi

echo "All wait-for-admission tests passed successfully."
