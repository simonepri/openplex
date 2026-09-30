#!/bin/sh
# Runs Chainsaw synthetic smoke check verifying cluster CoreDNS resolution.

# shellcheck disable=SC2310

set -eu

target_url="${KH_REPORTING_URL:-http://kuberhealthy.kuberhealthy.svc.cluster.local:8080/externalCheckStatus}"
case "${target_url}" in
  *://*:*/*) ;;
  http://*/*) target_url="$(echo "${target_url}" | sed 's|http://\([^/]*\)/|http://\1:8080/|')" ;;
  *) ;;
esac

resolve_host() {
  host="$1"
  out="$(wget --spider --dns-timeout=5 --connect-timeout=2 --tries=1 --no-check-certificate "http://${host}" 2>&1 || true)"
  if echo "${out}" | grep -qiE "unable to resolve|name or service not known"; then
    return 1
  fi
  return 0
}

errors=""

# Verify cluster CoreDNS resolution
if ! resolve_host "kubernetes.default.svc.cluster.local"; then
  errors="${errors}\"CoreDNS resolution failed for kubernetes.default.svc.cluster.local\", "
fi

if ! resolve_host "kuberhealthy.kuberhealthy.svc.cluster.local"; then
  errors="${errors}\"CoreDNS resolution failed for kuberhealthy.kuberhealthy.svc.cluster.local\", "
fi

if [ -z "${errors}" ]; then
  printf '{"OK": true, "Errors": []} - chainsaw-smoke-dns check OK finished successfully\n'
  wget -q -O- --timeout=10 --header="kh-run-uuid: ${KH_RUN_UUID:-}" \
    --header="Content-Type: application/json" \
    --post-data='{"OK": true, "Errors": []}' \
    "${target_url}" || true
else
  errors_json="[$(echo "${errors}" | sed 's/, $//')]"
  printf '{"OK": false, "Errors": %s} - chainsaw-smoke-dns check failed\n' "${errors_json}" >&2
  wget -q -O- --timeout=10 --header="kh-run-uuid: ${KH_RUN_UUID:-}" \
    --header="Content-Type: application/json" \
    --post-data="{\"OK\": false, \"Errors\": ${errors_json}}" \
    "${target_url}" || true
  exit 1
fi
