#!/bin/sh
# Validates authenticated Coder username inputs and populates NSS user mappings for UID and GID 1000.

set -eu

username=${1:?username is required}
runtime_dir=${2:?runtime directory is required}

if ! printf '%s\n' "${username}" | LC_ALL=C grep -Eq '^[a-z][a-z0-9_-]{0,31}$'; then
  printf 'Unsafe SSH login name: %s\n' "${username}" >&2
  exit 1
fi

mkdir -p "${runtime_dir}"
printf '%s:x:1000:1000:Workspace:/home/coder:/usr/bin/zsh\n' "${username}" >"${runtime_dir}/passwd"
printf '%s:x:1000:\n' "${username}" >"${runtime_dir}/group"
