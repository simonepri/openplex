#!/bin/sh
# Runs Chainsaw synthetic smoke check verifying Chainsaw CLI readiness.

set -eu

target_url="${KH_REPORTING_URL:-http://kuberhealthy.kuberhealthy.svc.cluster.local:8080/check}"
case "${target_url}" in
  *://*:*/*) ;;
  http://*/*) target_url="$(echo "${target_url}" | sed 's|http://\([^/]*\)/|http://\1:8080/|')" ;;
  *) ;;
esac
target_url="$(echo "${target_url}" | sed 's|/externalCheckStatus$|/check|')"

errors=""

# Verify Chainsaw CLI readiness
if ! chainsaw version >/dev/null 2>&1 || ! chainsaw test --help >/dev/null 2>&1; then
  errors="${errors}\"Chainsaw CLI probe failed\", "
fi

if [ -z "${errors}" ]; then
  printf '{"OK": true, "Errors": []} - chainsaw-smoke-cli check OK finished successfully\n'
  wget -q -O- --timeout=10 --header="kh-run-uuid: ${KH_RUN_UUID:-}" \
    --header="Content-Type: application/json" \
    --post-data='{"OK": true, "Errors": []}' \
    "${target_url}" || true
else
  errors_json="[$(echo "${errors}" | sed 's/, $//')]"
  printf '{"OK": false, "Errors": %s} - chainsaw-smoke-cli check failed\n' "${errors_json}" >&2
  wget -q -O- --timeout=10 --header="kh-run-uuid: ${KH_RUN_UUID:-}" \
    --header="Content-Type: application/json" \
    --post-data="{\"OK\": false, \"Errors\": ${errors_json}}" \
    "${target_url}" || true
  exit 1
fi
