#!/usr/bin/env bash
# Runs automated code review in CI.

set -euo pipefail

if [[ -n ${BUILD_WORKSPACE_DIRECTORY:-} ]]; then
  cd "${BUILD_WORKSPACE_DIRECTORY}"
fi

# Locate mise from Bazel data dependencies or system PATH
MISE_BIN=""
if [[ -n ${RUNFILES_DIR:-} ]] && [[ -x "${RUNFILES_DIR}/_main/external/mise_linux_amd64/file/downloaded" ]]; then
  MISE_BIN="${RUNFILES_DIR}/_main/external/mise_linux_amd64/file/downloaded"
elif [[ -n ${RUNFILES_DIR:-} ]] && [[ -x "${RUNFILES_DIR}/_main/external/mise_linux_arm64/file/downloaded" ]]; then
  MISE_BIN="${RUNFILES_DIR}/_main/external/mise_linux_arm64/file/downloaded"
elif command -v mise >/dev/null 2>&1; then
  MISE_BIN="$(command -v mise)"
fi

if [[ -n ${MISE_BIN} ]]; then
  mkdir -p "${HOME}/.local/bin"
  ln -sf "${MISE_BIN}" "${HOME}/.local/bin/mise"
  export PATH="${HOME}/.local/bin:${PATH}"
  mise trust --yes 2>/dev/null || true
  mise install --yes claude codex agy node 2>/dev/null || true
  eval "$(mise env --shell bash claude codex agy node 2>/dev/null || true)"
fi

mkdir -p .review
export GITHUB_OUTPUT="${PWD}/.review/outputs"
if command -v python3 >/dev/null 2>&1; then
  GH_TOKEN="$(python3 -c 'import netrc; print(netrc.netrc().authenticators("api.github.com")[2])' 2>/dev/null || echo "${GH_TOKEN:-}")"
  export GH_TOKEN
fi

repo="$(git remote get-url origin 2>/dev/null | sed -E 's#^.*github\.com[:/]##; s#\.git$##' || echo "")"
pr_num="${GIT_PR_NUMBER:-}"
commit="${GIT_COMMIT:-HEAD}"
inv_id="${BUILDBUDDY_INVOCATION_ID:-}"
pr=(--repo "${repo}" --pr "${pr_num}" --sha "${commit}" --job-url "https://app.buildbuddy.io/invocation/${inv_id}")

if [[ -n ${repo} && -n ${pr_num} ]]; then
  python3 -m src.infra.tools.review.cli comment check-override --repo "${repo}" --pr "${pr_num}" || true
  python3 -m src.infra.tools.review.cli comment start "${pr[@]}" || true
fi

outcome="success"
base_branch="origin/${GIT_BASE_BRANCH:-main}"
python3 -m src.infra.tools.review.cli --agent "${REVIEW_AGENT:-auto}" --base "${base_branch}" --target HEAD -o .review/review-bundle.md || outcome="failure"
[[ -f .review/review-result.json ]] || outcome="failure"

OVERRIDE_ACTIVE="$(sed -n 's/^active=//p' "${GITHUB_OUTPUT}" 2>/dev/null || echo "false")"
OVERRIDE_REASON="$(sed -n 's/^reason=//p' "${GITHUB_OUTPUT}" 2>/dev/null || echo "")"
export OVERRIDE_ACTIVE OVERRIDE_REASON

if [[ -n ${repo} && -n ${pr_num} ]]; then
  python3 -m src.infra.tools.review.cli comment finish "${pr[@]}" \
    --review-file .review/review-result.json \
    --meta-file .review/review-meta.json \
    --err-file .review/review-error.txt \
    --outcome "${outcome}" || true
fi

gate="$(sed -n 's/^gate=//p' "${GITHUB_OUTPUT}" 2>/dev/null || echo "pass")"
echo "Review gate: ${gate}"
[[ ${gate} == "pass" || ${gate} == "override" ]]
