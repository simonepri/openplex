#!/usr/bin/env bash
# Tests Ray Train acceptance harness contracts, workspace identity validation, Kueue admission checks, and checkpoint verification offline.

set -euo pipefail

subject="${1:?acceptance script is required}"
jq_bin="${2:?jq path is required}"
# shellcheck source=/dev/null
source "${subject}"
subject_repository_root="$(cd "$(dirname "${subject}")/../../../../.." && pwd)"

test_root="$(mktemp -d)"
trap 'rm -rf "$test_root"' EXIT
mkdir -p "${test_root}/bin"
cp "${jq_bin}" "${test_root}/bin/jq"
export PATH="${test_root}/bin:${PATH}"

expected_run_id=r0123456789abcd
expected_run_name=ray-train-researchuser-r0123456789abcd
expected_stream_tag=20260905T120000Z_0123456789ab
ray_job_uid=11111111-2222-4333-8444-555555555555
origin_digest="sha256:$(printf 'a%.0s' {1..64})"
deployment_repository=mirror.example.invalid/src/examples/ray_train

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

setup_case() {
  local name="$1"
  case_root="${test_root}/${name}"
  checkpoint_mount_root="${case_root}/s3"
  test_checkout_path="${subject_repository_root}"
  submitted_state="${case_root}/submitted"
  mkdir -p "${checkpoint_mount_root}/eaws-lh1/home/examples"
  export test_checkout_path

  export USER=Research.User
  export BAZEL_OUTPUT_ROOT="${case_root}/bazel"
  export WORKLOAD_REGISTRY=origin-registry:5000/123456789012/us-west-2
  export WORKLOAD_REGISTRY_INSECURE=true
  unset WORKLOAD_LAUNCHER RAY_TRAIN_ACCEPTANCE_RUN_ID

  mock_identity="system:serviceaccount:team-examples-workspaces:coder-workspace"
  mock_context=cell-eaws-lh1
  mock_pod_hostname=workspace-host
  mock_workspace_namespace=team-examples-workspaces
  mock_user_label=Research.User
  mock_workspace_pod_count=1
  mock_worker_count=2
  mock_workload_count=1
  mock_checkpoint_uri="s3://eaws-lh1/home/examples/ray-train/${expected_run_name}/TorchTrainer_fixture/checkpoint_000000"
}

git() {
  case "$*" in
    "rev-parse --show-toplevel") printf '%s\n' "${test_checkout_path}" ;;
    "rev-parse --verify --quiet HEAD^{commit}") return 0 ;;
    "show --no-patch --format=%cd --date=format-local:%Y%m%dT%H%M%SZ HEAD")
      printf '%s\n' 20260905T120000Z
      ;;
    "rev-parse --verify HEAD^{commit}")
      printf '%s\n' 0123456789abcdef0123456789abcdef01234567
      ;;
    *) fail "unexpected git invocation: $*" ;;
  esac
}
export -f git

hostname() {
  printf '%s\n' workspace-host
}

od() {
  [[ $* == "-An -N7 -tx1 /dev/urandom" ]] \
    || fail "unexpected od invocation: $*"
  printf '%s\n' ' 01 23 45 67 89 ab cd'
}

sleep() {
  :
}

workspace_pods_json() {
  jq --compact-output --null-input \
    --argjson pod_count "${mock_workspace_pod_count}" \
    --arg hostname "${mock_pod_hostname}" \
    --arg namespace "${mock_workspace_namespace}" \
    --arg user "${mock_user_label}" '{
      items: [range(0; $pod_count) | {
        metadata: {
          namespace: $namespace,
          labels: {
            "com.coder.workspace.id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            "com.coder.workspace.name": "research",
            "com.coder.user.username": $user
          }
        },
        spec: {
          hostname: $hostname,
          serviceAccountName: "coder-workspace",
          containers: [{name: "workspace"}]
        },
        status: {conditions: [{type: "Ready", status: "True"}]}
      }]
    }'
}

ray_job_json() {
  jq --compact-output --null-input \
    --arg digest "${origin_digest}" \
    --arg repository "${deployment_repository}" \
    --arg run_id "${expected_run_id}" \
    --arg run_name "${expected_run_name}" \
    --arg uid "${ray_job_uid}" '{
      metadata: {
        name: $run_name,
        namespace: "team-examples-workloads",
        uid: $uid,
        labels: {
          "pipeline": "ray-train",
          "run-id": $run_id,
          "user": "researchuser"
        }
      },
      spec: {
        suspend: false,
        runtimeEnvYAML: ("env_vars:\n  RAY_TRAIN_RUN_ID: " + $run_name),
        rayClusterSpec: {
          headGroupSpec: {template: {spec: {containers: [{image: ($repository + "@" + $digest)}]}}},
          workerGroupSpecs: [{template: {spec: {containers: [{image: ($repository + "@" + $digest)}]}}}]
        },
        submitterPodTemplate: {spec: {containers: [{image: ($repository + "@" + $digest)}]}}
      },
      status: {jobStatus: "SUCCEEDED"}
    }'
}

workloads_json() {
  jq --compact-output --null-input \
    --argjson workload_count "${mock_workload_count}" \
    --argjson worker_count "${mock_worker_count}" \
    --arg run_name "${expected_run_name}" \
    --arg uid "${ray_job_uid}" '{
      items: [range(0; $workload_count) | {
        metadata: {
          labels: {"kueue.x-k8s.io/job-uid": $uid},
          ownerReferences: [{
            apiVersion: "ray.io/v1",
            kind: "RayJob",
            name: $run_name,
            uid: $uid,
            controller: true
          }]
        },
        spec: {
          queueName: "wa",
          podSets: [
            {name: "head", count: 1},
            {name: "trainers", count: $worker_count},
            {name: "submitter", count: 1}
          ]
        },
        status: {
          admission: {
            clusterQueue: "team-examples-wa",
            podSetAssignments: [
              {name: "head", count: 1},
              {name: "trainers", count: $worker_count},
              {name: "submitter", count: 1}
            ]
          },
          conditions: [
            {type: "QuotaReserved", status: "True"},
            {type: "Admitted", status: "True"}
          ]
        }
      }]
    }'
}

kubectl() {
  case "${1:-}:${2:-}" in
    config:current-context)
      printf '%s\n' "${mock_context}"
      ;;
    config:view)
      printf '%s' team-examples-workspaces
      ;;
    auth:whoami)
      jq --compact-output --null-input --arg identity "${mock_identity}" \
        '{status: {userInfo: {username: $identity}}}'
      ;;
    get:pods)
      workspace_pods_json
      ;;
    get:rayjob/*)
      if [[ " $* " == *" --ignore-not-found "* ]]; then
        [[ ! -e ${submitted_state} ]] || printf 'rayjob.ray.io/%s\n' "${expected_run_name}"
      else
        [[ -e ${submitted_state} ]] || fail 'RayJob read preceded submission'
        ray_job_json
      fi
      ;;
    get:configmap/workload-repositories)
      jq --compact-output --null-input --arg repository "${deployment_repository}" '{
        data: {
          "repositories.json": ({
            version: 1,
            repositories: {"src/examples/ray_train": $repository}
          } | tojson)
        }
      }'
      ;;
    get:localqueue/wa)
      printf '%s\n' '{"spec":{"clusterQueue":"team-examples-wa"}}'
      ;;
    get:workloads)
      workloads_json
      ;;
    logs:job/*)
      printf 'RAY_TRAIN_RESULT={"checkpoint_path":"%s","loss":0.25}\n' \
        "${mock_checkpoint_uri}"
      ;;
    *) fail "unexpected kubectl invocation: $*" ;;
  esac
}

curl() {
  [[ $* == *"http://origin-registry:5000/v2/123456789012/us-west-2/src/examples/ray_train/manifests/${expected_stream_tag}"* ]] \
    || fail "unexpected curl invocation: $*"
  printf 'Docker-Content-Digest: %s\r\n' "${origin_digest}"
}

bazel() {
  [[ $* == "--output_user_root=${BAZEL_OUTPUT_ROOT} run //src/examples/ray_train:submit" ]] \
    || fail "unexpected bazel invocation: $*"
  [[ ${WORKLOAD_RUN_ID:-} == "${expected_run_id}" ]] \
    || fail "unexpected workload run ID: ${WORKLOAD_RUN_ID:-}"
  [[ ${WORKLOAD_STREAM_TAG:-} == "${expected_stream_tag}" ]] \
    || fail "unexpected workload stream tag: ${WORKLOAD_STREAM_TAG:-}"
  : >"${submitted_state}"
  mkdir -p "${checkpoint_mount_root}/${mock_checkpoint_uri#s3://}"
  printf '%s' checkpoint >"${checkpoint_mount_root}/${mock_checkpoint_uri#s3://}/model.pt"
}

setup_case success
output="$(local_ray_train_acceptance "${checkpoint_mount_root}" 1)"
[[ ${output} == *"${expected_run_name} succeeded with loss 0.25"* ]] \
  || fail 'successful acceptance did not report the canonical run and finite loss'
[[ ${output} == *"${mock_checkpoint_uri}/model.pt"* ]] \
  || fail 'successful acceptance did not report the actual checkpoint object'

setup_case wrong_identity
mock_identity=system:serviceaccount:team-examples-workspaces:another-service-account
if (local_ray_train_acceptance "${checkpoint_mount_root}" 0) >/dev/null 2>&1; then
  fail 'acceptance admitted a different Kubernetes service account'
fi
[[ ! -e ${submitted_state} ]] || fail 'identity denial ran the publisher'

setup_case wrong_workspace_namespace
mock_workspace_namespace=team-other-workspaces
if (local_ray_train_acceptance "${checkpoint_mount_root}" 0) >/dev/null 2>&1; then
  fail 'acceptance admitted a workspace pod from another team dev namespace'
fi
[[ ! -e ${submitted_state} ]] || fail 'workspace namespace denial ran the publisher'

setup_case wrong_cell
mock_context=cell-eaws-other
if (local_ray_train_acceptance "${checkpoint_mount_root}" 0) >/dev/null 2>&1; then
  fail 'acceptance admitted a cell other than the configured storage writer'
fi
[[ ! -e ${submitted_state} ]] || fail 'cell denial ran the publisher'

setup_case wrong_user_label
mock_user_label=Another.User
if (local_ray_train_acceptance "${checkpoint_mount_root}" 0) >/dev/null 2>&1; then
  fail 'acceptance admitted a workspace owned by another user'
fi
[[ ! -e ${submitted_state} ]] || fail 'workspace user denial ran the publisher'

setup_case wrong_hostname
mock_pod_hostname=another-workspace
if (local_ray_train_acceptance "${checkpoint_mount_root}" 0) >/dev/null 2>&1; then
  fail 'acceptance admitted a different running workspace pod'
fi
[[ ! -e ${submitted_state} ]] || fail 'workspace hostname denial ran the publisher'

setup_case duplicate_workspace_pods
mock_workspace_pod_count=2
if (local_ray_train_acceptance "${checkpoint_mount_root}" 0) >/dev/null 2>&1; then
  fail 'acceptance admitted an ambiguous workspace pod identity'
fi
[[ ! -e ${submitted_state} ]] || fail 'workspace pod cardinality denial ran the publisher'

setup_case reused_checkpoint
mkdir -p "${checkpoint_mount_root}/eaws-lh1/home/examples/ray-train/${expected_run_name}"
if (local_ray_train_acceptance "${checkpoint_mount_root}" 0) >/dev/null 2>&1; then
  fail 'acceptance reused an existing checkpoint directory'
fi
[[ ! -e ${submitted_state} ]] || fail 'checkpoint collision ran the publisher'

setup_case wrong_kueue_shape
mock_worker_count=1
if (local_ray_train_acceptance "${checkpoint_mount_root}" 0) >/dev/null 2>&1; then
  fail 'acceptance admitted the wrong Kueue pod-set shape'
fi

setup_case duplicate_kueue_workloads
mock_workload_count=2
if (local_ray_train_acceptance "${checkpoint_mount_root}" 0) >/dev/null 2>&1; then
  fail 'acceptance admitted multiple Kueue workloads for one RayJob'
fi
[[ -e ${submitted_state} ]] || fail 'Kueue workload cardinality case did not reach the publisher'

setup_case escaped_checkpoint
mock_checkpoint_uri=s3://eaws-lh1/home/examples/ray-train/another-run/checkpoint_000000
if (local_ray_train_acceptance "${checkpoint_mount_root}" 0) >/dev/null 2>&1; then
  fail 'acceptance trusted a checkpoint outside the canonical run directory'
fi

printf '%s\n' 'Ray Train live acceptance contract passed.'
