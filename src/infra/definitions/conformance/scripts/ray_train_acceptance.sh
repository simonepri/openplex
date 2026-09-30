#!/usr/bin/env bash
# Submits and verifies a canonical Ray Train run from a Coder workspace to defend Kueue queue admission, distributed execution, and checkpoint persistence.

# shellcheck disable=SC2312
set -euo pipefail

ray_train_acceptance_error() {
  printf '%s\n' "$1" >&2
  exit 1
}

local_ray_train_acceptance() {
  local checkpoint_mount_root="${1:?checkpoint mount root is required}"
  local script_directory
  script_directory="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  local workspace_namespace=team-examples-workspaces
  local workload_namespace=team-examples-workloads
  local pipeline=ray-train
  local queue=wa
  local cluster_queue=team-examples-wa
  local result_prefix=RAY_TRAIN_RESULT=
  local timeout_seconds="${2:-1200}"

  [[ ${timeout_seconds} =~ ^[0-9]+$ ]] \
    || ray_train_acceptance_error 'Acceptance timeout must be a non-negative integer.'

  : "${WORKLOAD_REGISTRY:?workspace workload registry is required}"
  : "${WORKLOAD_REGISTRY_INSECURE:?workspace registry transport is required}"
  : "${USER:?workspace user is required}"
  : "${BAZEL_OUTPUT_ROOT:?workspace Bazel output root is required}"

  [[ ${WORKLOAD_REGISTRY_INSECURE} == true ]] \
    || ray_train_acceptance_error \
      'Ray Train local acceptance requires the registered HTTP Floci origin.'
  [[ -z ${WORKLOAD_LAUNCHER+x} ]] \
    || ray_train_acceptance_error \
      'Ray Train acceptance must derive its launcher from the workspace user.'
  [[ -z ${RAY_TRAIN_ACCEPTANCE_RUN_ID+x} ]] \
    || ray_train_acceptance_error \
      'Ray Train acceptance generates a unique run ID and does not accept an override.'

  local repository_root
  repository_root="$(git rev-parse --show-toplevel)"
  local script_repository_root
  script_repository_root="$(cd "${script_directory}/../../../../.." && pwd)"
  [[ ${repository_root} == "${script_repository_root}" ]] \
    || ray_train_acceptance_error \
      "Run from the workspace checkout ${script_repository_root}, found ${repository_root}."
  local storage_writer_cell="cell-eaws-lh1"

  local current_context
  current_context="$(kubectl config current-context)"
  [[ ${current_context} == "${storage_writer_cell}" ]] \
    || ray_train_acceptance_error \
      "Ray Train local acceptance requires configured storage-writer cell ${storage_writer_cell}, found ${current_context}."
  local current_namespace
  current_namespace="$(
    kubectl config view --minify \
      --output=jsonpath='{.contexts[0].context.namespace}'
  )"
  [[ ${current_namespace} == "${workspace_namespace}" ]] \
    || ray_train_acceptance_error \
      "Workspace Kubernetes namespace ${current_namespace:-unset} does not match ${workspace_namespace}."

  local expected_subject="system:serviceaccount:${workspace_namespace}:coder-workspace"
  local current_identity
  current_identity="$(kubectl auth whoami --output=json)"
  printf '%s' "${current_identity}" \
    | jq --exit-status --arg subject "${expected_subject}" \
      '.status.userInfo.username == $subject' >/dev/null \
    || ray_train_acceptance_error \
      "Workspace Kubernetes identity is not ${expected_subject}."

  local workspace_pods
  workspace_pods="$(
    kubectl get pods --namespace="${workspace_namespace}" \
      --selector="app.kubernetes.io/name=coder-workspace" \
      --field-selector=status.phase=Running --output=json
  )"
  printf '%s' "${workspace_pods}" \
    | jq --exit-status \
      --arg hostname "$(hostname)" \
      --arg namespace "${workspace_namespace}" \
      --arg service_account coder-workspace \
      --arg user "${USER}" '
        [.items[] | select(.spec.hostname == $hostname)] as $matches |
        $matches[0] as $pod |
        ($matches | length) == 1 and
        $pod.metadata.namespace == $namespace and
        $pod.metadata.labels["com.coder.user.username"] == $user and
        ($pod.metadata.labels["com.coder.workspace.id"] | type == "string" and length > 0) and
        ($pod.metadata.labels["com.coder.workspace.name"] | type == "string" and length > 0) and
        $pod.spec.serviceAccountName == $service_account and
        $pod.spec.hostname == $hostname and
        any($pod.spec.containers[]; .name == "workspace") and
        any($pod.status.conditions[]?; .type == "Ready" and .status == "True")
      ' >/dev/null \
    || ray_train_acceptance_error \
      'The current process is not the attested running team dev workspace pod.'

  local run_id
  run_id="r$(od -An -N7 -tx1 /dev/urandom | tr -d '[:space:]')"
  [[ ${run_id} =~ ^r[0-9a-f]{14}$ ]] \
    || ray_train_acceptance_error \
      "Could not generate a unique Ray Train acceptance run ID: ${run_id}."
  local launcher
  launcher="$(
    printf '%s' "${USER}" \
      | tr '[:upper:]' '[:lower:]' \
      | tr -cd 'a-z0-9' \
      | cut -c1-15
  )"
  [[ -n ${launcher} ]] \
    || ray_train_acceptance_error \
      "Workspace user normalizes to an empty launcher: ${USER}."
  local run_name="${pipeline}-${launcher}-${run_id}"
  local virtual_cell="${current_context#cell-}"
  # nosemgrep: repository.storage.canonical-virtual-s3-uri - Acceptance test harness cell parameter
  local checkpoint_run_uri="s3://${virtual_cell}/home/examples/ray-train/${run_name}"
  local checkpoint_run_directory="${checkpoint_mount_root}/${checkpoint_run_uri#s3://}"
  [[ -d "${checkpoint_mount_root}/${virtual_cell}/home/examples" ]] \
    || ray_train_acceptance_error \
      "Workspace S3 home mount is unavailable below ${checkpoint_mount_root}."
  [[ ! -e ${checkpoint_run_directory} ]] \
    || ray_train_acceptance_error \
      "Unique checkpoint directory already exists at ${checkpoint_run_uri}."
  local existing_ray_job
  existing_ray_job="$(
    kubectl get "rayjob/${run_name}" --namespace="${workload_namespace}" \
      --ignore-not-found --output=name
  )"
  [[ -z ${existing_ray_job} ]] \
    || ray_train_acceptance_error \
      "Unique RayJob already exists as ${existing_ray_job}."

  local stream_tag
  stream_tag="$(bash "${repository_root}/src/bazel/rules/oci/stream_tag.sh")"
  WORKLOAD_RUN_ID="${run_id}" WORKLOAD_STREAM_TAG="${stream_tag}" \
    bazel --output_user_root="${BAZEL_OUTPUT_ROOT}" run //src/examples/ray_train:submit

  local ray_job
  ray_job="$(kubectl get "rayjob/${run_name}" --namespace="${workload_namespace}" --output=json)"
  local ray_job_uid
  ray_job_uid="$(printf '%s' "${ray_job}" | jq --exit-status --raw-output '.metadata.uid')"

  local repository_contract
  repository_contract="$(
    kubectl get configmap/workload-repositories \
      --namespace="${workload_namespace}" --output=json
  )"
  local deployment_repository
  deployment_repository="$(
    printf '%s' "${repository_contract}" \
      | jq --exit-status --raw-output \
        --arg repository_path src/examples/ray_train '
          .data["repositories.json"]
          | fromjson
          | select(.version == 1)
          | .repositories[$repository_path]
          | select(type == "string" and length > 0)
        '
  )"
  local origin_repository="${WORKLOAD_REGISTRY}/src/examples/ray_train"
  local origin_registry="${origin_repository%%/*}"
  local origin_repository_path="${origin_repository#*/}"
  local origin_headers
  origin_headers="$(
    curl --fail --silent --show-error --head \
      --header 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json' \
      "http://${origin_registry}/v2/${origin_repository_path}/manifests/${stream_tag}"
  )"
  local origin_digest
  origin_digest="$(
    printf '%s\n' "${origin_headers}" \
      | tr -d '\r' \
      | awk 'tolower($1) == "docker-content-digest:" { print $2 }'
  )"
  [[ ${origin_digest} =~ ^sha256:[0-9a-f]{64}$ ]] \
    || ray_train_acceptance_error \
      "Floci origin returned an invalid digest for ${origin_repository}:${stream_tag}."
  local deployment_image="${deployment_repository}@${origin_digest}"
  printf '%s' "${ray_job}" \
    | jq --exit-status \
      --arg deployment_image "${deployment_image}" \
      --arg run_id "${run_id}" \
      --arg run_name "${run_name}" \
      --arg user "${launcher}" '
        .metadata.name == $run_name and
        .metadata.namespace == "team-examples-workloads" and
        .metadata.labels.pipeline == "ray-train" and
        .metadata.labels["run-id"] == $run_id and
        .metadata.labels.user == $user and
        (.spec.runtimeEnvYAML | contains("RAY_TRAIN_RUN_ID: " + $run_name)) and
        ([
          .spec.rayClusterSpec.headGroupSpec.template.spec.containers[].image,
          .spec.rayClusterSpec.workerGroupSpecs[].template.spec.containers[].image,
          .spec.submitterPodTemplate.spec.containers[].image
        ] | length == 3 and all(.[]; . == $deployment_image))
      ' >/dev/null \
    || ray_train_acceptance_error \
      "RayJob ${run_name} does not preserve its canonical identity and image digest."

  local local_queue
  local_queue="$(
    kubectl get "localqueue/${queue}" --namespace="${workload_namespace}" --output=json
  )"
  printf '%s' "${local_queue}" \
    | jq --exit-status --arg cluster_queue "${cluster_queue}" \
      '.spec.clusterQueue == $cluster_queue' >/dev/null \
    || ray_train_acceptance_error \
      "LocalQueue ${queue} does not target ClusterQueue ${cluster_queue}."

  local deadline="$(($(date +%s) + timeout_seconds))"
  local workloads
  while :; do
    workloads="$(
      kubectl get workloads --namespace="${workload_namespace}" \
        --selector="kueue.x-k8s.io/job-uid=${ray_job_uid}" --output=json
    )"
    if printf '%s' "${workloads}" \
      | jq --exit-status \
        --arg cluster_queue "${cluster_queue}" \
        --arg queue "${queue}" \
        --arg ray_job_uid "${ray_job_uid}" \
        --arg run_name "${run_name}" '
          .items[0] as $workload |
          (reduce $workload.status.admission.podSetAssignments[] as $assignment
            ({}; .[$assignment.name] = ($assignment.count // null))) as $assignments |
          (.items | length) == 1 and
          $workload.metadata.labels["kueue.x-k8s.io/job-uid"] == $ray_job_uid and
          any($workload.metadata.ownerReferences[]?;
            .apiVersion == "ray.io/v1" and
            .kind == "RayJob" and
            .name == $run_name and
            .uid == $ray_job_uid and
            .controller == true) and
          $workload.spec.queueName == $queue and
          $workload.status.admission.clusterQueue == $cluster_queue and
          any($workload.status.conditions[]?;
            .type == "QuotaReserved" and .status == "True") and
          any($workload.status.conditions[]?;
            .type == "Admitted" and .status == "True") and
          (reduce $workload.spec.podSets[] as $pod_set
            ({}; .[$pod_set.name] = $pod_set.count)) ==
              {"head": 1, "trainers": 2, "submitter": 1} and
          ($assignments | keys | sort) == ["head", "submitter", "trainers"] and
          ($assignments.head == null or $assignments.head == 1) and
          ($assignments.trainers == null or $assignments.trainers == 2) and
          ($assignments.submitter == null or $assignments.submitter == 1)
        ' >/dev/null; then
      break
    fi
    if (($(date +%s) >= deadline)); then
      printf 'RayJob %s did not receive the exact Kueue admission within %s seconds.\n' \
        "${run_name}" "${timeout_seconds}" >&2
      printf '%s\n' "${workloads}" >&2
      return 1
    fi
    sleep 5
  done

  deadline="$(($(date +%s) + timeout_seconds))"
  while :; do
    ray_job="$(kubectl get "rayjob/${run_name}" --namespace="${workload_namespace}" --output=json)"
    local status
    status="$(printf '%s' "${ray_job}" | jq --raw-output '.status.jobStatus // ""')"
    case "${status}" in
      SUCCEEDED) break ;;
      FAILED | STOPPED)
        printf 'RayJob %s reached terminal status %s.\n' "${run_name}" "${status}" >&2
        printf '%s\n' "${ray_job}" >&2
        kubectl logs "job/${run_name}" --namespace="${workload_namespace}" \
          --container=ray-job-submitter >&2 || true
        return 1
        ;;
      *) ;;
    esac
    if (($(date +%s) >= deadline)); then
      printf 'RayJob %s did not succeed within %s seconds.\n' \
        "${run_name}" "${timeout_seconds}" >&2
      printf '%s\n' "${ray_job}" >&2
      kubectl logs "job/${run_name}" --namespace="${workload_namespace}" \
        --container=ray-job-submitter >&2 || true
      return 1
    fi
    sleep 5
  done

  printf '%s' "${ray_job}" \
    | jq --exit-status '
      .spec.suspend == false and
      .status.jobStatus == "SUCCEEDED"
    ' >/dev/null

  local submitter_log
  submitter_log="$(
    kubectl logs "job/${run_name}" --namespace="${workload_namespace}" \
      --container=ray-job-submitter
  )"
  local result_records
  result_records="$(
    printf '%s\n' "${submitter_log}" \
      | sed -n "s/^.*${result_prefix}//p"
  )"
  [[ "$(printf '%s\n' "${result_records}" | sed '/^$/d' | wc -l | tr -d '[:space:]')" == 1 ]] || {
    printf 'RayJob %s did not emit exactly one completion record.\n' "${run_name}" >&2
    printf '%s\n' "${submitter_log}" >&2
    return 1
  }

  local checkpoint_uri
  if ! checkpoint_uri="$(
    printf '%s' "${result_records}" \
      | jq --exit-status --raw-output \
        --arg checkpoint_run_uri "${checkpoint_run_uri}" '
            .checkpoint_path
            | select(type == "string")
            | select(startswith($checkpoint_run_uri + "/"))
          '
  )"; then
    ray_train_acceptance_error \
      "RayJob ${run_name} reported a checkpoint outside its canonical run directory."
  fi
  local loss
  if ! loss="$(
    printf '%s' "${result_records}" \
      | jq --exit-status --raw-output '.loss | select(type == "number")'
  )"; then
    ray_train_acceptance_error \
      "RayJob ${run_name} did not report a numeric loss."
  fi
  python3 - "${loss}" <<'PY' || ray_train_acceptance_error \
    "RayJob ${run_name} did not report a finite loss."
import math
import sys

if not math.isfinite(float(sys.argv[1])):
    raise SystemExit(1)
PY
  local checkpoint_file="${checkpoint_mount_root}/${checkpoint_uri#s3://}/model.pt"
  deadline="$(($(date +%s) + 120))"
  while [[ ! -s ${checkpoint_file} ]]; do
    if (($(date +%s) >= deadline)); then
      printf 'Ray Train checkpoint is absent at %s/model.pt.\n' "${checkpoint_uri}" >&2
      return 1
    fi
    sleep 2
  done

  printf 'Ray Train run %s succeeded with loss %s and retained checkpoint %s/model.pt\n' \
    "${run_name}" "${loss}" "${checkpoint_uri}"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  local_ray_train_acceptance /s3
fi
