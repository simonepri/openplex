#!/usr/bin/env bash
# Validate git commit messages against conventional commit rules while permitting placeholder subjects.

set -euo pipefail

committed="${1:?committed executable is required}"
message_file="${*: -1}"

if [[ "$(<"${message_file}")" == "." ]]; then
  exit 0
fi

# Reject scoped conventional commits to enforce strictly unscoped commit messages.
subject="$(head -n 1 "${message_file}")"
if printf '%s\n' "${subject}" | grep -q -E '^[a-z]+\([^)]+\):'; then
  echo "Error: Commit scopes are not permitted; use strictly unscoped conventional commits (e.g., 'type: description')." >&2
  exit 1
fi

config_args=()
if [[ $# -ge 3 ]]; then
  config_args=(--config "$2")
elif [[ -f "src/bazel/checks/commit_message/committed.toml" ]]; then
  config_args=(--config "src/bazel/checks/commit_message/committed.toml")
elif [[ -f "$(dirname "${BASH_SOURCE[0]}")/committed.toml" ]]; then
  config_args=(--config "$(dirname "${BASH_SOURCE[0]}")/committed.toml")
elif [[ -f "committed.toml" ]]; then
  config_args=(--config "committed.toml")
fi

exec "${committed}" "${config_args[@]}" --commit-file "${message_file}"
