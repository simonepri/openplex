#!/usr/bin/env bash
# Verifies workspace-mounts filesystem accessibility, probe checks, and ready-marker generation.

set -euo pipefail

subject="${1:-$(cd -- "$(dirname -- "$0")" && pwd)/workspace-mounts.sh}"
test_root="$(mktemp -d)"
trap 'rm -rf -- "$test_root"' EXIT

volume="${test_root}/volume"
runtime="${test_root}/runtime"
mkdir -p "${volume}" "${runtime}"

# 1. Missing volume directory must fail
if env WORKSPACE_BOOT_TOKEN=boot-token XDG_RUNTIME_DIR="${runtime}" \
  bash "${subject}" "${test_root}/nonexistent" >/dev/null 2>&1; then
  printf 'Expected missing volume to fail\n' >&2
  exit 1
fi

# 2. Non-writable volume directory must fail
unwritable="${test_root}/unwritable"
mkdir -p "${unwritable}"
chmod 0500 "${unwritable}"
if env WORKSPACE_BOOT_TOKEN=boot-token XDG_RUNTIME_DIR="${runtime}" \
  bash "${subject}" "${unwritable}" >/dev/null 2>&1; then
  chmod 0700 "${unwritable}"
  printf 'Expected unwritable volume to fail\n' >&2
  exit 1
fi
chmod 0700 "${unwritable}"

# 3. Valid volume must write ready marker
env WORKSPACE_BOOT_TOKEN=boot-token XDG_RUNTIME_DIR="${runtime}" \
  bash "${subject}" "${volume}"

ready_marker="${runtime}/workspace-mounts-ready"
test -f "${ready_marker}"
[[ "$(<"${ready_marker}")" == "boot-token" ]]

printf 'All workspace-mounts tests passed.\n'
