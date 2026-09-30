#!/usr/bin/env bash
# Guard test ensuring no file under src/infra/argocd and no critical Bazel build input is an unresolved Git LFS pointer.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "${SCRIPT_DIR}" rev-parse --show-toplevel 2>/dev/null || true)"

if [[ -z ${REPO_ROOT} ]]; then
  dir="${SCRIPT_DIR}"
  while [[ ${dir} != "/" ]]; do
    if [[ -f "${dir}/MODULE.bazel" ]] || [[ -d "${dir}/.git" ]]; then
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

check_file() {
  local file="$1"
  if [[ ! -f ${file} ]]; then
    echo "Error: expected file does not exist: ${file}" >&2
    failed=1
    return
  fi

  local rel_file="${file#"${REPO_ROOT}/"}"
  local blob_header=""
  if [[ -d "${REPO_ROOT}/.git" ]] || git -C "${REPO_ROOT}" rev-parse --git-dir >/dev/null 2>&1; then
    blob_header="$(git -C "${REPO_ROOT}" cat-file -p ":${rel_file}" 2>/dev/null | head -n 1 || true)"
    if [[ -z ${blob_header} ]]; then
      blob_header="$(git -C "${REPO_ROOT}" show "HEAD:${rel_file}" 2>/dev/null | head -n 1 || true)"
    fi
  fi

  if [[ -n ${blob_header} ]] && echo "${blob_header}" | grep -q "^${LFS_HEADER_PREFIX}$"; then
    echo "Error: committed git blob is an unresolved Git LFS pointer: ${rel_file}" >&2
    failed=1
    return
  fi

  if head -n 1 "${file}" 2>/dev/null | grep -q "^${LFS_HEADER_PREFIX}$"; then
    echo "Error: working file is an unresolved Git LFS pointer: ${file}" >&2
    failed=1
    return
  fi
}

echo "Checking Bazel build inputs for Git LFS pointers..."
check_file "${REPO_ROOT}/src/infra/tools/coder_snapshot_portal/web/static/favicon.svg"
check_file "${REPO_ROOT}/src/infra/docs/artwork/wordmark.svg"

echo "Checking files under src/infra/argocd for Git LFS pointers..."
THIS_SCRIPT="${SCRIPT_DIR}/$(basename "${BASH_SOURCE[0]}")"

# shellcheck disable=SC2312
while IFS= read -r -d '' file; do
  if [[ ${file} == "${THIS_SCRIPT}" ]]; then
    continue
  fi
  check_file "${file}"
done < <(find "${REPO_ROOT}/src/infra/argocd" -type f ! -path "*/.git/*" -print0)

if [[ ${failed} -ne 0 ]]; then
  echo "Git LFS guard check failed: found files containing LFS pointer header." >&2
  exit 1
fi

echo "Git LFS guard check passed: no Git LFS pointers found in src/infra/argocd or Bazel build inputs."
exit 0
