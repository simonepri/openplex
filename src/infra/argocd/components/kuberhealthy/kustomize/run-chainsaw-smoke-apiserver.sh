#!/bin/sh
# Runs Chainsaw synthetic smoke check verifying Kubernetes API server health.

set -eu

target_url="${KH_REPORTING_URL:-http://kuberhealthy.kuberhealthy.svc.cluster.local:8080/check}"
case "${target_url}" in
  *://*:*/*) ;;
  http://*/*) target_url="$(echo "${target_url}" | sed 's|http://\([^/]*\)/|http://\1:8080/|')" ;;
  *) ;;
esac
target_url="$(echo "${target_url}" | sed 's|/externalCheckStatus$|/check|')"

errors=""

# Verify Kubernetes API server TLS /version and /livez endpoints
sa_token=""
if [ -f "/var/run/secrets/kubernetes.io/serviceaccount/token" ]; then
  sa_token="$(cat /var/run/secrets/kubernetes.io/serviceaccount/token 2>/dev/null || true)"
fi

if [ -z "${sa_token}" ]; then
  errors="${errors}\"Service account token missing or unreadable\", "
else
  auth_header="Authorization: Bearer ${sa_token}"
  if ! wget -q -O- --timeout=5 --no-check-certificate --header="${auth_header}" https://kubernetes.default.svc.cluster.local/version >/dev/null 2>&1; then
    errors="${errors}\"Kubernetes API server TLS /version probe failed\", "
  fi

  if ! wget -q -O- --timeout=5 --no-check-certificate --header="${auth_header}" https://kubernetes.default.svc.cluster.local/livez >/dev/null 2>&1; then
    errors="${errors}\"Kubernetes API server TLS /livez probe failed\", "
  fi
fi

if [ -z "${errors}" ]; then
  printf '{"OK": true, "Errors": []} - chainsaw-smoke-apiserver check OK finished successfully\n'
  wget -q -O- --timeout=10 --header="kh-run-uuid: ${KH_RUN_UUID:-}" \
    --header="Content-Type: application/json" \
    --post-data='{"OK": true, "Errors": []}' \
    "${target_url}" || true
else
  errors_json="[$(echo "${errors}" | sed 's/, $//')]"
  printf '{"OK": false, "Errors": %s} - chainsaw-smoke-apiserver check failed\n' "${errors_json}" >&2
  wget -q -O- --timeout=10 --header="kh-run-uuid: ${KH_RUN_UUID:-}" \
    --header="Content-Type: application/json" \
    --post-data="{\"OK\": false, \"Errors\": ${errors_json}}" \
    "${target_url}" || true
  exit 1
fi
