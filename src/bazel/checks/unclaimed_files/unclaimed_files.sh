#!/usr/bin/env bash
# Verify that all tracked git repository files are claimed by at least one Bazel target or package source rule.

set -euo pipefail

cd "${BUILD_WORKSPACE_DIRECTORY:?unclaimed_files must be run with bazel run}"

covered="$(mktemp)"
tracked="$(mktemp)"
uncovered="$(mktemp)"
trap 'rm -f "$covered" "$tracked" "$uncovered"' EXIT

bazel --output_user_root="${BAZEL_OUTPUT_ROOT:-${PWD}/.tmp/state/bazel}" \
  query 'kind("source file", deps(//...))' --output=label 2>/dev/null \
  | grep -E '^//' \
  | sed 's|^//||; s|:|/|; s|^/||' \
  | LC_ALL=C sort -u >"${covered}"

# Only files that exist: the index still lists files deleted in the worktree,
# and comparing the index to the graph reports those as uncovered forever.
while IFS= read -r file; do
  [[ -f ${file} ]] && printf '%s\n' "${file}"
done < <(git ls-files || true) | LC_ALL=C sort -u >"${tracked}"

LC_ALL=C comm -23 "${tracked}" "${covered}" \
  | grep -v 'BUILD.bazel$' \
    >"${uncovered}" || true

if [[ -s ${uncovered} ]]; then
  echo 'Files outside the Bazel graph. bazel-diff cannot see them and no lint' >&2
  echo 'aspect can reach them. Add them to a target, usually package_sources():' >&2
  sed 's/^/  /' "${uncovered}" >&2
  exit 1
fi
