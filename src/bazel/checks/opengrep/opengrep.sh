#!/usr/bin/env bash
# Execute OpenGrep static analysis rules across workspace sources and evaluate exception suppressions.

set -euo pipefail

# The sandbox strips the locale; the tool's embedded Python must not fall back
# to ASCII when a rule message uses more.
export PYTHONUTF8=1

workspace="${BUILD_WORKSPACE_DIRECTORY:-$(git rev-parse --show-toplevel)}"
opengrep="${1:?missing opengrep path}"
rules="${2:-${workspace}/src/bazel/checks/opengrep/rules.yaml}"
output="${TEST_TMPDIR:-$(mktemp -d)}/semgrep-results.json"

# Absolutize without dereferencing: the tool is a runfiles symlink whose
# ancestors carry the .runfiles tree its stub resolves against; realpath
# would strand it at the raw bazel-out file.
if [[ ${opengrep#/} == "${opengrep}" ]]; then opengrep="${PWD}/${opengrep}"; fi
rules="$(realpath "${rules}")"
rm -f "${output}"

if ! git -C "${workspace}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "BUILD_WORKSPACE_DIRECTORY is not a Git checkout: ${workspace}" >&2
  exit 2
fi

cd "${workspace}"

scan_targets=("${@:3}")
if ((${#scan_targets[@]} == 0)); then
  if [[ ${CHECK_MODE:-} == affected ]]; then
    rules_rel="${rules#"${workspace}/"}"
    if grep -q -F -x "${rules_rel}" <<<"${CHANGED_ALL:-}" \
      || grep -q -F -x "src/bazel/checks/opengrep/rules.yaml" <<<"${CHANGED_ALL:-}" \
      || grep -q -F -x "src/bazel/checks/opengrep/opengrep.sh" <<<"${CHANGED_ALL:-}"; then
      scan_targets=(".")
    else
      while IFS= read -r changed; do
        [[ -n ${changed} ]] || continue
        if [[ -f ${changed} && ${changed} != .tmp/* && ${changed} != .git/* && ${changed} != src/bazel/checks/opengrep/fixtures/* ]]; then
          scan_targets+=("${changed}")
        fi
      done <<<"${CHANGED_ALL:-}"
      if ((${#scan_targets[@]} == 0)); then
        exit 0
      fi
    fi
  else
    scan_targets=(".")
  fi
fi

# The native command line skips the Python wrapper, which spends half the
# run starting up and matching rule paths before the engine starts. The
# engine gains little past a few workers, and other gates share the machine.
cpus="$(getconf _NPROCESSORS_ONLN)"
jobs=$((cpus / 2 + cpus % 2))

output_dir="$(dirname "${output}")"
mkdir -p "${output_dir}"

batch_size=500
batch_outputs=()
overall_status=0

for ((i = 0; i < ${#scan_targets[@]}; i += batch_size)); do
  batch=("${scan_targets[@]:i:batch_size}")
  batch_file="${output_dir}/semgrep-batch-$((i / batch_size)).json"
  rm -f "${batch_file}"

  set +e
  "${opengrep}" scan \
    --experimental \
    --jobs "${jobs}" \
    --config "${rules}" \
    --error \
    --strict \
    --json \
    --json-output "${batch_file}" \
    --exclude .git \
    --exclude .tmp \
    --exclude src/bazel/checks/opengrep/fixtures \
    "${batch[@]}" >/dev/null
  batch_status=$?
  set -e

  if [[ ! -s ${batch_file} ]]; then
    echo "Semgrep did not write machine-readable output" >&2
    exit 2
  fi

  if ((batch_status != 0)); then
    if ((batch_status == 1)); then
      overall_status=1
    elif ((overall_status == 0)); then
      overall_status="${batch_status}"
    fi
  fi

  batch_outputs+=("${batch_file}")
done

if ((${#batch_outputs[@]} == 1)); then
  mv -f "${batch_outputs[0]}" "${output}"
else
  jq -s '{results: [.[].results[]?], errors: [.[].errors[]?]}' "${batch_outputs[@]}" >"${output}"
  rm -f "${batch_outputs[@]}"
fi

status="${overall_status}"

if [[ ! -s ${output} ]]; then
  echo "Semgrep did not write machine-readable output" >&2
  exit 2
fi
if ! jq -e '.errors | length == 0' "${output}" >/dev/null; then
  echo "Semgrep reported tool or rule errors" >&2
  exit 2
fi

if [[ ${status} -ne 0 ]]; then
  echo "OpenGrep findings:" >&2
  jq -r '.results[] | "  \(.path):\(.start.line): \(.check_id) - \(.extra.message)"' "${output}" >&2
fi

exit "${status}"
