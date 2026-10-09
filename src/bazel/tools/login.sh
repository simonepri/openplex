#!/usr/bin/env bash
# Sign in to GitHub and BuildBuddy.

set -euo pipefail

# Coder workspaces get GitHub access from Coder's external auth and ship without gh.
if [[ -z ${CODER:-} ]]; then
  gh auth status >/dev/null 2>&1 || gh auth login
  gh auth setup-git
fi

bb_args=(--allow_existing)
if [[ -n ${BUILDBUDDY_ORG:-} ]]; then
  bb_args+=("--org=${BUILDBUDDY_ORG}")
fi
if [[ -n ${CODER:-} ]]; then
  bb_args+=(--no_launch_browser)
  printf '%s\n' "Open the printed link and paste the key at the prompt."
fi
bb login "${bb_args[@]}"
