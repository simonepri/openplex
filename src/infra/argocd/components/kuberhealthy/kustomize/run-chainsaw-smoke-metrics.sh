#!/bin/sh
# Runs Chainsaw synthetic smoke check verifying Kubernetes metrics API availability and node metrics.

set -eu

target_url="${KH_REPORTING_URL:-http://kuberhealthy.kuberhealthy.svc.cluster.local:8080/externalCheckStatus}"
case "${target_url}" in
  *://*:*/*) ;;
  http://*/*) target_url="$(echo "${target_url}" | sed 's|http://\([^/]*\)/|http://\1:8080/|')" ;;
  *) ;;
esac

errors=""

sa_token="${KH_SA_TOKEN:-}"
if [ -z "${sa_token}" ] && [ -f "/var/run/secrets/kubernetes.io/serviceaccount/token" ]; then
  sa_token="$(cat /var/run/secrets/kubernetes.io/serviceaccount/token 2>/dev/null || true)"
fi

if [ -z "${sa_token}" ]; then
  errors="${errors}\"Service account token missing or unreadable\", "
else
  auth_header="Authorization: Bearer ${sa_token}"

  # Verify v1beta1.metrics.k8s.io APIService is Available
  apiservice_resp="$(wget -q -O- --timeout=5 --no-check-certificate --header="${auth_header}" https://kubernetes.default.svc.cluster.local/apis/apiregistration.k8s.io/v1/apiservices/v1beta1.metrics.k8s.io 2>/dev/null || true)"
  apiservice_status="$(echo "${apiservice_resp}" | awk -v RS='}' '
    $0 ~ /"type"[[:space:]]*:[[:space:]]*"Available"/ {
      if (match($0, /"status"[[:space:]]*:[[:space:]]*"[^"]+"/)) {
        s = substr($0, RSTART, RLENGTH)
        sub(/.*:[[:space:]]*"/, "", s)
        sub(/".*/, "", s)
        print s
      }
    }
  ')"
  if [ -z "${apiservice_resp}" ]; then
    errors="${errors}\"Kubernetes metrics APIService v1beta1.metrics.k8s.io probe failed\", "
  elif [ "${apiservice_status}" != "True" ]; then
    errors="${errors}\"Kubernetes metrics APIService v1beta1.metrics.k8s.io is not Available\", "
  fi

  # Verify node metrics endpoint returns metrics
  node_metrics_resp="$(wget -q -O- --timeout=5 --no-check-certificate --header="${auth_header}" https://kubernetes.default.svc.cluster.local/apis/metrics.k8s.io/v1beta1/nodes 2>/dev/null || true)"
  if [ -z "${node_metrics_resp}" ]; then
    errors="${errors}\"Kubernetes node metrics endpoint probe failed\", "
  elif ! echo "${node_metrics_resp}" | grep -q '"kind":[[:space:]]*"NodeMetricsList"'; then
    errors="${errors}\"Kubernetes node metrics endpoint did not return NodeMetricsList\", "
  elif ! echo "${node_metrics_resp}" | grep -q '"usage":'; then
    errors="${errors}\"Kubernetes node metrics endpoint returned no node metrics\", "
  fi
fi

if [ -z "${errors}" ]; then
  printf '{"OK": true, "Errors": []} - chainsaw-smoke-metrics check OK finished successfully\n'
  wget -q -O- --timeout=10 --header="kh-run-uuid: ${KH_RUN_UUID:-}" \
    --header="Content-Type: application/json" \
    --post-data='{"OK": true, "Errors": []}' \
    "${target_url}" || true
else
  errors_json="[$(echo "${errors}" | sed 's/, $//')]"
  printf '{"OK": false, "Errors": %s} - chainsaw-smoke-metrics check failed\n' "${errors_json}" >&2
  wget -q -O- --timeout=10 --header="kh-run-uuid: ${KH_RUN_UUID:-}" \
    --header="Content-Type: application/json" \
    --post-data="{\"OK\": false, \"Errors\": ${errors_json}}" \
    "${target_url}" || true
  exit 1
fi
