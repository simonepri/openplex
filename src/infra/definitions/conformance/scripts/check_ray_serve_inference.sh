#!/bin/sh
# Checks Ray Serve cluster admission, prediction responses, and rejection of invalid payloads.

# shellcheck disable=SC2310,SC2312
set -eu

cd "$(git rev-parse --show-toplevel)"

namespace=team-examples-workloads
application=cell-eaws-lh1-ray-serve-prod
ray_service=ray-serve-example-prod
service=ray-serve-example-prod-serve-svc
ctrl_kubeconfig=.tmp/kubeconfigs/ctrl-eaws-lh1.yaml
local_port=18085
port_forward_log="$(mktemp)"
valid_response="$(mktemp)"
invalid_response="$(mktemp)"
request_error="$(mktemp)"
port_forward_pid=

cleanup() {
  if test -n "${port_forward_pid}"; then
    kill "${port_forward_pid}" 2>/dev/null || true
    wait "${port_forward_pid}" 2>/dev/null || true
  fi
  rm -f \
    "${port_forward_log}" \
    "${valid_response}" \
    "${invalid_response}" \
    "${request_error}"
}
trap cleanup EXIT

ctrl() {
  kubectl --kubeconfig "${ctrl_kubeconfig}" "$@"
}

wait_for_application() {
  deadline="$(($(date +%s) + 300))"
  while :; do
    application_json=
    if application_json="$(
      ctrl get application "${application}" --namespace=argocd \
        --output=json 2>/dev/null
    )" && printf '%s' "${application_json}" | jq --exit-status \
      --arg namespace "${namespace}" '
        .spec.destination.namespace == $namespace and
        .spec.source.path == "src/examples/ray_serve/deployment" and
        .status.sync.status == "Synced" and
        .status.health.status == "Healthy"
      ' >/dev/null; then
      return
    fi
    test "$(date +%s)" -lt "${deadline}" || {
      printf '%s\n' "${application_json}" >&2
      return 1
    }
    sleep 2
  done
}

wait_for_port_forward() {
  deadline="$(($(date +%s) + 60))"
  while ! grep -Fq "Forwarding from 127.0.0.1:${local_port}" \
    "${port_forward_log}"; do
    if ! kill -0 "${port_forward_pid}" 2>/dev/null; then
      cat "${port_forward_log}" >&2
      return 1
    fi
    test "$(date +%s)" -lt "${deadline}" || {
      cat "${port_forward_log}" >&2
      return 1
    }
    sleep 1
  done
}

wait_for_prediction() {
  deadline="$(($(date +%s) + 180))"
  while :; do
    : >"${valid_response}"
    : >"${request_error}"
    request_status=000
    if request_status="$(
      curl --silent --show-error --noproxy '*' \
        --connect-timeout 5 --max-time 120 \
        --header 'Content-Type: application/json' \
        --data '{"features":[2,4]}' \
        --output "${valid_response}" --write-out '%{http_code}' \
        "http://127.0.0.1:${local_port}/" 2>"${request_error}"
    )" && test "${request_status}" = 200 \
      && jq --exit-status '
        type == "object" and keys == ["score"] and .score == 4
      ' "${valid_response}" >/dev/null; then
      return
    fi
    test "$(date +%s)" -lt "${deadline}" || {
      printf 'Ray Serve returned HTTP %s without the promised score.\n' \
        "${request_status}" >&2
      cat "${request_error}" "${valid_response}" >&2
      return 1
    }
    sleep 2
  done
}

assert_invalid_request_denied() {
  invalid_status="$(
    curl --silent --show-error --noproxy '*' \
      --connect-timeout 5 --max-time 30 \
      --header 'Content-Type: application/json' \
      --data '{"features":[2]}' \
      --output "${invalid_response}" --write-out '%{http_code}' \
      "http://127.0.0.1:${local_port}/"
  )"
  if test "${invalid_status}" != 400; then
    printf 'Invalid Ray Serve request returned HTTP %s, expected 400.\n' \
      "${invalid_status}" >&2
    cat "${invalid_response}" >&2
    return 1
  fi
}

assert_cluster_admitted() {
  cluster="$(kubectl get rayservice "${ray_service}" --namespace="${namespace}" \
    --output=jsonpath='{.status.activeServiceStatus.rayClusterName}')"
  test -n "${cluster}"
  cluster_json="$(kubectl get raycluster "${cluster}" --namespace="${namespace}" --output=json)"
  cluster_uid="$(printf '%s' "${cluster_json}" | jq --raw-output '.metadata.uid')"
  queue="$(printf '%s' "${cluster_json}" | jq --raw-output '.metadata.labels["kueue.x-k8s.io/queue-name"]')"
  expected_image="$(ctrl get application "${application}" --namespace=argocd --output=json \
    | jq --raw-output '.spec.source.kustomize.images | select(length == 1) | .[0] | split("=")[1]')"
  printf '%s' "${cluster_json}" | jq --exit-status \
    --arg service "${ray_service}" --arg image "${expected_image}" '
      .spec.suspend == false and
      any(.metadata.ownerReferences[]; .kind == "RayService" and .name == $service) and
      ([.spec.headGroupSpec.template.spec.containers[],
        .spec.workerGroupSpecs[].template.spec.containers[]] | all(.image == $image))
    ' >/dev/null
  kubectl get workloads --namespace="${namespace}" --output=json \
    | jq --exit-status --arg uid "${cluster_uid}" --arg queue "${queue}" '
      [.items[] | select(any(.metadata.ownerReferences[]?;
        .kind == "RayCluster" and .uid == $uid))] |
      length == 1 and all(.[];
        .spec.queueName == $queue and
        .status.admission != null and
        any(.status.conditions[]?; .type == "Admitted" and .status == "True"))
    ' >/dev/null
}

wait_for_application
kubectl wait rayservice/"${ray_service}" --namespace="${namespace}" \
  --for=jsonpath='{.status.serviceStatus}'=Running --timeout=600s >/dev/null
assert_cluster_admitted

kubectl port-forward service/"${service}" --namespace="${namespace}" \
  --address=127.0.0.1 "${local_port}:8000" >"${port_forward_log}" 2>&1 &
port_forward_pid=$!
wait_for_port_forward

wait_for_prediction
assert_invalid_request_denied
