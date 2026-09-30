#!/usr/bin/env bash
# Executes Chainsaw behavioral test suites across control and cell clusters to defend infrastructure contracts against behavioral regressions.

set -euo pipefail

chainsaw="${1:?chainsaw binary required}"
if [[ ${chainsaw#/} == "${chainsaw}" ]]; then chainsaw="${PWD}/${chainsaw}"; fi
jq="${2:?jq binary required}"
if [[ ${jq#/} == "${jq}" ]]; then jq="${PWD}/${jq}"; fi
shift 2

root="${BUILD_WORKSPACE_DIRECTORY:-$(git rev-parse --show-toplevel)}"
cd "${root}"

suite_dir="src/infra/definitions/conformance"
test_file=""
report_format=JSON
report_name=report
report_path=""
args=(test "${suite_dir}" --config "${suite_dir}/.chainsaw.yaml" --values "${suite_dir}/values.yaml")
while [[ $# -gt 0 ]]; do
  case "$1" in
    --test)
      test_name="${2:?--test requires a test name}"
      args+=(--include-test-regex "^chainsaw/${test_name}$")
      shift
      ;;
    --test=*)
      test_name="${1#*=}"
      : "${test_name:?--test requires a test name}"
      args+=(--include-test-regex "^chainsaw/${test_name}$")
      ;;
    --test-file)
      test_file="${2:?--test-file requires a suite filename}"
      shift
      ;;
    --test-file=*)
      test_file="${1#*=}"
      : "${test_file:?--test-file requires a suite filename}"
      ;;
    --report-format)
      report_format="${2:?report format required}"
      shift
      ;;
    --report-format=*) report_format="${1#*=}" ;;
    --report-name)
      report_name="${2:?report name required}"
      shift
      ;;
    --report-name=*) report_name="${1#*=}" ;;
    --report-path)
      report_path="${2:?report path required}"
      shift
      ;;
    --report-path=*) report_path="${1#*=}" ;;
    -h | --help) exec "${chainsaw}" test --help ;;
    *) args+=("$1") ;;
  esac
  shift
done
if [[ ${report_format} != JSON ]]; then
  printf 'JSON reports are required to verify test execution.\n' >&2
  exit 1
fi
if [[ ${report_name##*/} != *.* ]]; then report_name="${report_name}.json"; fi

shopt -s nullglob
suites=("${suite_dir}"/*.test.k8s.yaml)
if [[ -n ${test_file} ]]; then
  suites=("${suite_dir}/${test_file}")
  if [[ ${test_file} == */* || ${test_file} != *.test.k8s.yaml ]]; then
    printf 'Expected a top-level suite filename: %s\n' "${test_file}" >&2
    exit 1
  fi
fi
if [[ ${#suites[@]} -eq 0 ]]; then
  printf 'No Chainsaw suites found in %s\n' "${suite_dir}" >&2
  exit 1
fi

mkdir -p "${suite_dir}/.tmp"
run_dir="$(mktemp -d "${suite_dir}/.tmp/run.XXXXXX")"
trap 'rm -rf "$run_dir"' EXIT
report_path="${report_path:-${run_dir}}"
# A unique relative filename keeps fixture discovery separate while preserving
# the source directory as the base for suite commands and resource paths.
for suite in "${suites[@]}"; do
  if [[ ! -f ${suite} ]]; then
    printf 'Chainsaw suite does not exist: %s\n' "${suite}" >&2
    exit 1
  fi
  printf '\n---\n' >>"${run_dir}/tests.yaml"
  cat "${suite}" >>"${run_dir}/tests.yaml"
done
rm -f "${report_path}/${report_name}"
"${chainsaw}" "${args[@]}" \
  --test-file "${run_dir#"${suite_dir}/"}/tests.yaml" \
  --report-format JSON --report-path "${report_path}" --report-name "${report_name}"
if ! "${jq}" --exit-status 'any(.tests[]?; .status != "skipped")' "${report_path}/${report_name}" >/dev/null; then
  printf 'Chainsaw executed no tests; check suite selection.\n' >&2
  exit 1
fi
