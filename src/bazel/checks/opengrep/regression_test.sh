#!/usr/bin/env bash
# Verify that OpenGrep rules detect violations in synthetic regression fixtures and exit with expected codes.

set -euo pipefail

# The sandbox strips the locale; the tool's embedded Python must not fall back
# to ASCII when a rule message uses more.
export PYTHONUTF8=1

semgrep="${PWD}/${1:?semgrep path}"
rules="${PWD}/${2:?rules path}"
jq="${PWD}/${3:?jq path}"
# Runfiles entries are symlinks and semgrep refuses to scan those, so scan a copy.
fixture_source="${PWD}/${4:?fixture path}"
fixture_base="$(basename "${fixture_source}")"
tmp_dir="${TEST_TMPDIR:-}"
if [[ -z ${tmp_dir} ]]; then
  tmp_dir="$(mktemp -d)"
fi
fixture="${tmp_dir}/${fixture_base}"
cp "${fixture_source}" "${fixture}"
output="${tmp_dir}/semgrep-regression.json"

set +e
"${semgrep}" scan --config "${rules}" --error --strict \
  --json --json-output "${output}" "${fixture}"
status=$?
set -e

if [[ ${status} -ne 1 ]]; then
  echo "expected the known Semgrep violation to return 1, got ${status}" >&2
  exit 1
fi
# jq rather than an inline python3 heredoc: a program embedded in a string is
# unreachable by ruff and shellcheck, which is the defect this repository bans.
if ! "${jq}" -e '(.errors | length) == 0' "${output}" >/dev/null; then
  echo 'Semgrep reported tool or rule errors' >&2
  exit 1
fi
# shellcheck disable=SC2016 # $rule is a jq variable.
if ! "${jq}" -e --arg rule .python.lang.security.audit.subprocess-shell-true \
  'any(.results[]; .check_id | endswith($rule))' "${output}" >/dev/null; then
  echo 'Semgrep did not report the expected regression rule' >&2
  exit 1
fi
