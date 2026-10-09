#!/usr/bin/env bash
# Audit locked source dependencies against SPDX license allowlists and report vulnerability advisories.

set -euo pipefail

tool="${1:-osv-scanner}"
if [[ ${tool#/} == "${tool}" ]] && [[ -f ${tool} ]]; then
  tool="${PWD}/${tool}"
fi

jq_bin="jq"
if [[ $# -ge 2 ]] && [[ $2 == *jq* ]] && [[ -f $2 ]]; then
  jq_bin="$2"
  if [[ ${jq_bin#/} == "${jq_bin}" ]]; then
    jq_bin="${PWD}/${jq_bin}"
  fi
fi

root="${BUILD_WORKSPACE_DIRECTORY:-$(git rev-parse --show-toplevel)}"
cd "${root}"

config="src/bazel/checks/license_images/osv-scanner.toml"

# Shipped dependencies allowlist
shipped_allowed="0BSD,Apache-2.0,BSD-2-Clause,BSD-3-Clause,BSL-1.0,CC0-1.0,ISC,MIT,MIT-0,MPL-2.0,PSF-2.0,PostgreSQL,Python-2.0,Unicode-3.0,Unlicense,Zlib"
# Dev tool dependencies allowlist (includes copyleft)
dev_allowed="${shipped_allowed},CNRI-Python,GPL-2.0-only,GPL-2.0-or-later,GPL-3.0-only,GPL-3.0-or-later,LGPL-2.1-only,LGPL-2.1-or-later,LGPL-3.0-only,LGPL-3.0-or-later"

scan_tier() {
  local tier_name="$1" allowed="$2"
  shift 2
  local report
  report="$(mktemp)"

  local args=(
    scan source
    --config "${config}"
    "--licenses=${allowed}"
    --format json
    --output-file "${report}"
    --verbosity error
    --all-packages
  )
  for lock in "$@"; do
    args+=(--lockfile "${lock}")
  done

  set +e
  "${tool}" "${args[@]}"
  local tool_status=$?
  set -e
  if [[ ${tool_status} -gt 1 ]]; then
    echo "osv-scanner failed (exit ${tool_status})" >&2
    rm -f "${report}"
    return 1
  fi

  local vulns=""
  if [[ -s ${report} ]]; then
    vulns="$("${jq_bin}" -r '[.results[]?.packages[]? | select((.vulnerabilities // []) | length > 0) | "\(.package.name)@\(.package.version): \((.vulnerabilities // []) | length) advisory matches"] | unique | .[]' "${report}" 2>/dev/null)"
  fi
  if [[ -n ${vulns} ]]; then
    echo "Advisories identified in ${tier_name} dependencies (logged for audit; vulnerability gating enforced by Trivy):" >&2
    printf "  %s\n" "${vulns}" >&2
  fi

  local violations
  violations="$("${jq_bin}" -r '[.results[]?.packages[]? | select((.license_violations // []) | length > 0) | "\(.package.name)@\(.package.version): \((.licenses // []) | join(","))"] | .[]' "${report}")"
  rm -f "${report}"

  if [[ -n ${violations} ]]; then
    echo "Dependencies carrying licenses outside the allowlist (${tier_name}):" >&2
    printf "  %s\n" "${violations}" >&2
    return 1
  fi
}

echo "Scanning shipped dependencies for license compliance..."
scan_tier "shipped" "${shipped_allowed}" requirements_lock.txt pnpm-lock.yaml

echo "Scanning development dependencies for license compliance..."
scan_tier "dev_tools" "${dev_allowed}" requirements_dev_lock.txt

echo "License check passed."
