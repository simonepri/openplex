#!/usr/bin/env bash
# Test commit message validation logic for valid subjects, placeholder bypasses, and invalid formats.

set -euo pipefail

checker_path="${1:?commit message checker is required}"
committed_path="${2:?committed executable is required}"
checker_dir="$(cd "$(dirname "${checker_path}")" && pwd)"
checker_base="$(basename "${checker_path}")"
checker="${checker_dir}/${checker_base}"
committed_dir="$(cd "$(dirname "${committed_path}")" && pwd)"
committed_base="$(basename "${committed_path}")"
committed="${committed_dir}/${committed_base}"
test_dir="$(mktemp -d)"
trap 'rm -rf "${test_dir}"' EXIT
repo="${test_dir}/repo"
mkdir "${repo}"
git -C "${repo}" init --quiet
git -C "${repo}" config user.email test@example.invalid
git -C "${repo}" config user.name "Test User"
git -C "${repo}" commit --quiet --allow-empty --message 'Create fixture'

cat <<'EOF' >"${repo}/committed.toml"
style = "conventional"
subject_capitalized = false
subject_length = 72
line_length = 72
imperative_subject = true
subject_not_punctuated = true
no_fixup = true
no_wip = true
merge_commit = true
allowed_types = [
  "build",
  "chore",
  "ci",
  "docs",
  "feat",
  "fix",
  "perf",
  "refactor",
  "revert",
  "style",
  "test",
]
EOF

printf '.\n' >"${test_dir}/placeholder"
(cd "${repo}" && "${checker}" "${committed}" "${test_dir}/placeholder")

printf 'feat: add inventory consumers\n' >"${test_dir}/valid"
(cd "${repo}" && "${checker}" "${committed}" "${test_dir}/valid")

printf 'feat(inventory): add inventory consumers\n' >"${test_dir}/scoped"
if (cd "${repo}" && "${checker}" "${committed}" "${test_dir}/scoped") 2>/dev/null; then
  printf '%s\n' 'scoped conventional subject unexpectedly passed' >&2
  exit 1
fi

printf 'Add inventory consumers\n' >"${test_dir}/untyped"
if (cd "${repo}" && "${checker}" "${committed}" "${test_dir}/untyped") 2>/dev/null; then
  printf '%s\n' 'untyped subject unexpectedly passed' >&2
  exit 1
fi

printf 'feat: add inventory consumers.\n' >"${test_dir}/punctuated"
if (cd "${repo}" && "${checker}" "${committed}" "${test_dir}/punctuated") 2>/dev/null; then
  printf '%s\n' 'punctuated normal subject unexpectedly passed' >&2
  exit 1
fi
