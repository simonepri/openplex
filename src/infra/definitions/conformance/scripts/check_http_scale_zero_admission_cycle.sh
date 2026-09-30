#!/bin/sh
# Validates HTTP scale-to-zero activation and Kueue queue admission cycles to defend responsive cold-starts and quota enforcement for web workloads.

# shellcheck disable=SC2310,SC2312
set -eu

cd "$(git rev-parse --show-toplevel)"

namespace=team-examples-workloads
deployment=svelte-web
application=cell-eaws-lh1-svelte-web-prod
ctrl_kubeconfig=.tmp/kubeconfigs/ctrl-eaws-lh1.yaml
request_status="$(mktemp)"
request_headers="$(mktemp)"
request_error="$(mktemp)"
ca_certificate="$(mktemp)"
request_pid=

cleanup() {
  kill "${request_pid:-}" 2>/dev/null || true
  rm -f \
    "${request_status}" \
    "${request_headers}" \
    "${request_error}" \
    "${ca_certificate}"
}
trap cleanup EXIT

ctrl() {
  kubectl --kubeconfig "${ctrl_kubeconfig}" "$@"
}

assert_application_healthy() {
  ctrl get application "${application}" --namespace=argocd \
    --output=json | jq --exit-status '
      .status.sync.status == "Synced" and
      .status.health.status == "Healthy"
    ' >/dev/null
}

wait_for_application_healthy() {
  deadline="$(($(date +%s) + 180))"
  until assert_application_healthy; do
    test "$(date +%s)" -lt "${deadline}" || return 1
    sleep 2
  done
}

wait_for_zero() {
  deadline="$(($(date +%s) + 240))"
  while :; do
    deployment_json="$(
      kubectl get deployment/"${deployment}" \
        --namespace="${namespace}" --output=json
    )"
    pods_json="$(
      kubectl get pods --namespace="${namespace}" \
        --selector=app.kubernetes.io/name="${deployment}" \
        --output=json
    )"
    if printf '%s' "${deployment_json}" | jq --exit-status '
      .spec.replicas == 0 and
      (.status.replicas // 0) == 0 and
      (.status.availableReplicas // 0) == 0
    ' >/dev/null && printf '%s' "${pods_json}" \
      | jq --exit-status '(.items | length) == 0' >/dev/null; then
      return
    fi
    test "$(date +%s)" -lt "${deadline}" || {
      printf '%s\n%s\n' "${deployment_json}" "${pods_json}" >&2
      return 1
    }
    sleep 2
  done
}

assert_cold_start_response() {
  status="$(cat "${request_status}")"
  if test "${status}" != 200; then
    printf 'Cold-start request returned HTTP %s, expected 200.\n' "${status}" >&2
    cat "${request_error}" >&2
    return 1
  fi
  if ! tr -d '\r' <"${request_headers}" | awk '
    tolower($1) == "x-keda-http-cold-start:" && tolower($2) == "true" {
      found = 1
    }
    END { exit found ? 0 : 1 }
  '; then
    printf '%s\n' 'Cold-start response omitted x-keda-http-cold-start: true.' >&2
    cat "${request_headers}" >&2
    return 1
  fi
}

wait_for_zero
wait_for_application_healthy

kubectl get configmap/cluster-local-ca-source \
  --namespace=cert-manager \
  --output='jsonpath={.data.ca\.crt}' >"${ca_certificate}"

curl --cacert "${ca_certificate}" --silent --show-error \
  --noproxy '*' --max-time 150 \
  --dump-header "${request_headers}" \
  --output /dev/null --write-out '%{http_code}' \
  "https://svelte-web.cell-eaws-lh1.c.${INSTALLATION_PUBLIC_DOMAIN:?}/" \
  >"${request_status}" 2>"${request_error}" &
request_pid=$!

kubectl wait deployment/"${deployment}" --namespace="${namespace}" \
  --for=jsonpath='{.spec.replicas}'=1 --timeout=120s >/dev/null
kubectl wait pod --namespace="${namespace}" \
  --selector=app.kubernetes.io/name="${deployment}" \
  --for=create --timeout=120s >/dev/null
pod_json="$(
  kubectl get pods --namespace="${namespace}" \
    --selector=app.kubernetes.io/name="${deployment}" \
    --output=json
)"
pod_uid="$(
  printf '%s' "${pod_json}" | jq --raw-output '
    if (.items | length) == 1 then .items[0].metadata.uid else "" end
  '
)"
test -n "${pod_uid}"

deadline="$(($(date +%s) + 120))"
workload_name=
while test -z "${workload_name}"; do
  workloads_json="$(
    kubectl get workloads --namespace="${namespace}" --output=json
  )"
  workload_name="$(
    printf '%s' "${workloads_json}" \
      | jq --raw-output --arg uid "${pod_uid}" '
        first(.items[] | select(
          .metadata.labels["kueue.x-k8s.io/job-uid"] == $uid or
          any(.metadata.ownerReferences[]?; .uid == $uid)
        ) | .metadata.name) // ""
      '
  )"
  test "$(date +%s)" -lt "${deadline}" || {
    printf '%s\n' "${workloads_json}" >&2
    exit 1
  }
  test -n "${workload_name}" || sleep 2
done
kubectl wait workload/"${workload_name}" --namespace="${namespace}" \
  --for=condition=Admitted --timeout=120s >/dev/null
kubectl get workload/"${workload_name}" --namespace="${namespace}" \
  --output=json | jq --exit-status '
    .spec.queueName == "be" and
    .status.admission.clusterQueue == "be" and
    any(.status.conditions[];
      .type == "QuotaReserved" and .status == "True") and
    any(.status.conditions[];
      .type == "Admitted" and .status == "True")
  ' >/dev/null

deadline="$(($(date +%s) + 120))"
while :; do
  deployment_json="$(
    kubectl get deployment/"${deployment}" \
      --namespace="${namespace}" --output=json
  )"
  if printf '%s' "${deployment_json}" | jq --exit-status '
    .spec.replicas == 1 and
    .status.observedGeneration == .metadata.generation and
    .status.updatedReplicas == 1 and
    .status.availableReplicas == 1
  ' >/dev/null; then
    break
  fi
  test "$(date +%s)" -lt "${deadline}" || {
    printf '%s\n' "${deployment_json}" >&2
    exit 1
  }
  sleep 2
done
if ! wait "${request_pid}"; then
  cat "${request_error}" >&2
  exit 1
fi
request_pid=
assert_cold_start_response
assert_application_healthy

wait_for_zero
assert_application_healthy
