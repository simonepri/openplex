#!/usr/bin/env bash
# Guard test ensuring no file under src/infra/argocd and no critical Bazel build input is an unresolved Git LFS pointer.

set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(dirname "${SCRIPT_PATH}")"
REPO_ROOT="${BUILD_WORKSPACE_DIRECTORY:-}"

if [[ -z ${REPO_ROOT} ]]; then
  REPO_ROOT="$(git -C "${SCRIPT_DIR}" rev-parse --show-toplevel 2>/dev/null || true)"
fi

if [[ -z ${REPO_ROOT} ]]; then
  dir="${SCRIPT_DIR}"
  while [[ ${dir} != "/" ]]; do
    if [[ -d "${dir}/.git" ]]; then
      REPO_ROOT="${dir}"
      break
    fi
    dir="$(dirname "${dir}")"
  done
fi

if [[ -z ${REPO_ROOT} ]]; then
  echo "Error: could not determine repository root" >&2
  exit 1
fi

LFS_HEADER_PREFIX="version https://git-lfs.github.com/spec/v1"
failed=0

check_blob() {
  local rel_file="$1"
  local blob_hash="$2"

  if git -C "${REPO_ROOT}" cat-file -p "${blob_hash}" 2>/dev/null | head -c 100 | grep -q "^${LFS_HEADER_PREFIX}"; then
    echo "Error: committed git blob is an unresolved Git LFS pointer: ${rel_file}" >&2
    failed=1
  fi
}

echo "Checking Bazel build inputs for Git LFS pointers..."
CRITICAL_FILES=(
  "src/infra/tools/coder_snapshot_portal/web/static/favicon.svg"
  "src/infra/docs/artwork/wordmark.svg"
)

for rel_file in "${CRITICAL_FILES[@]}"; do
  blob_entry="$(git -C "${REPO_ROOT}" ls-files -s -- "${rel_file}")"
  if [[ -z ${blob_entry} ]]; then
    echo "Error: expected critical file is not tracked in git: ${rel_file}" >&2
    failed=1
    continue
  fi
  blob_hash="$(awk '{print $2}' <<<"${blob_entry}")"
  check_blob "${rel_file}" "${blob_hash}"
done

echo "Checking files under src/infra/argocd for Git LFS pointers..."
# shellcheck disable=SC2312
while read -r _mode hash _stage file; do
  check_blob "${file}" "${hash}"
done < <(git -C "${REPO_ROOT}" ls-files -s -- src/infra/argocd)

if [[ ${failed} -ne 0 ]]; then
  echo "Git LFS guard check failed: found committed git blobs containing LFS pointer header." >&2
  exit 1
fi

echo "Git LFS guard check passed: no Git LFS pointers found in src/infra/argocd or Bazel build inputs."
exit 0
