#!/bin/sh
# Runs in-cluster Chainsaw synthetic tests, generates structured reports, and reports status to Kuberhealthy.

set -eu

check_name="${1:-${CHECK_NAME:?check name is required}}"
test_path="${2:-${TEST_PATH:-/tests}}"

target_url="${KH_REPORTING_URL:-http://kuberhealthy.kuberhealthy.svc.cluster.local:8080/check}"
case "${target_url}" in
  *://*:*/*) ;;
  http://*/*) target_url="$(echo "${target_url}" | sed 's|http://\([^/]*\)/|http://\1:8080/|')" ;;
  *) ;;
esac
target_url="$(echo "${target_url}" | sed 's|/externalCheckStatus$|/check|')"

report_path="/tmp"
report_name="report.json"
report_file="${report_path}/${report_name}"
rm -f "${report_file}"

jitter_max="${JITTER_MAX_SECONDS:-90}"
if [ "${jitter_max}" -gt 0 ]; then
  # shellcheck disable=SC3028
  jitter_delay="$((RANDOM % jitter_max + 1))"
  printf 'Applying startup jitter delay: %ss (max %ss)\n' "${jitter_delay}" "${jitter_max}"
  sleep "${jitter_delay}"
fi

staging_dir="/tmp/run-tests"
rm -rf "${staging_dir}"
mkdir -p "${staging_dir}"
cp -RL "${test_path}"/* "${staging_dir}/" 2>/dev/null || true
find "${staging_dir}" -name "..*" -exec rm -rf {} + 2>/dev/null || true

# Recreate fixtures subdirectories if test references fixtures/<suite>/
mkdir -p "${staging_dir}/fixtures/storage" "${staging_dir}/fixtures/networking" "${staging_dir}/fixtures/observability" "${staging_dir}/fixtures/secrets"
for f in "${staging_dir}"/*.yaml; do
  [ -f "${f}" ] || continue
  base="$(basename "${f}")"
  case "${base}" in
    gateway-name-resolution-probe.test.k8s.yaml) cp "${f}" "${staging_dir}/fixtures/storage/" ;;
    policy-conformance.test.k8s.yaml) cp "${f}" "${staging_dir}/fixtures/networking/" ;;
    door-query.test.k8s.yaml) cp "${f}" "${staging_dir}/fixtures/observability/" ;;
    chainsaw-fixture.test.k8s.yaml) cp "${f}" "${staging_dir}/fixtures/secrets/" ;;
    *) ;;
  esac
done

chainsaw_status=0
values_arg=""
if [ -f "${staging_dir}/values.yaml" ]; then
  values_arg="--values ${staging_dir}/values.yaml"
fi

# shellcheck disable=SC2086
chainsaw test "${staging_dir}" ${values_arg} --report-format JSON --report-path "${report_path}" --report-name "${report_name}" || chainsaw_status=$?

errors=""
if [ ! -f "${report_file}" ]; then
  errors='"Chainsaw failed without producing report"'
elif grep -q '"status": "failed"' "${report_file}"; then
  failed_tests="$(grep -B 2 '"status": "failed"' "${report_file}" | grep '"name":' | sed -e 's/.*"name": *"//' -e 's/".*//' | tr '\n' ' ' | sed 's/[[:space:]]*$//')"
  if [ -n "${failed_tests}" ]; then
    errors="\"Chainsaw test failed: ${failed_tests}\""
  else
    errors='"Chainsaw reported test failures"'
  fi
elif [ "${chainsaw_status}" -ne 0 ]; then
  errors="\"Chainsaw exited with non-zero status ${chainsaw_status}\""
elif ! grep -q '"status": "passed"' "${report_file}"; then
  errors='"Chainsaw executed no tests"'
fi

if [ -z "${errors}" ]; then
  printf '{"OK": true, "Errors": []} - %s check OK finished successfully\n' "${check_name}"
  wget -q -O- --timeout=10 --header="kh-run-uuid: ${KH_RUN_UUID:-}" \
    --header="Content-Type: application/json" \
    --post-data='{"OK": true, "Errors": []}' \
    "${target_url}" || true
else
  errors_json="[${errors}]"
  printf '{"OK": false, "Errors": %s} - %s failed\n' "${errors_json}" "${check_name}" >&2
  wget -q -O- --timeout=10 --header="kh-run-uuid: ${KH_RUN_UUID:-}" \
    --header="Content-Type: application/json" \
    --post-data="{\"OK\": false, \"Errors\": ${errors_json}}" \
    "${target_url}" || true
  exit 1
fi
