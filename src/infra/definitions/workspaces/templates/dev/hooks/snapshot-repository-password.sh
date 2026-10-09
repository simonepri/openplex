#!/bin/sh
# Derives workspace snapshot repository passwords using HMAC-SHA256 from the cluster snapshot root key.

set -eu

key_file="${WORKSPACE_SNAPSHOT_ROOT_KEY_FILE:-}"
if [ -z "${key_file}" ]; then
  printf '%s\n' "WORKSPACE_SNAPSHOT_ROOT_KEY_FILE environment variable is not set" >&2
  exit 1
fi

if [ ! -f "${key_file}" ]; then
  printf '%s\n' "WORKSPACE_SNAPSHOT_ROOT_KEY_FILE '${key_file}' does not exist" >&2
  exit 1
fi

if [ ! -r "${key_file}" ]; then
  printf '%s\n' "WORKSPACE_SNAPSHOT_ROOT_KEY_FILE '${key_file}' is not readable" >&2
  exit 1
fi

root_key=$(tr -d '\r\n' <"${key_file}")
if [ -z "${root_key}" ]; then
  printf '%s\n' "WORKSPACE_SNAPSHOT_ROOT_KEY_FILE '${key_file}' is empty" >&2
  exit 1
fi

input=$(cat)
owner_id=$(printf '%s' "${input}" | sed -n 's/.*"owner_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')

if [ -z "${owner_id}" ]; then
  printf '%s\n' "owner_id is missing from input JSON query" >&2
  exit 1
fi

# The Coder server image ships openssl but not python3.
password=$(printf 'workspace-snapshot-repository-password\0%s' "${owner_id}" | openssl dgst -sha256 -mac HMAC -macopt "key:${root_key}" -binary | openssl base64 -e | tr -d '\r\n=' | tr '+/' '-_' | cut -c 1-43)

if ! printf '%s\n' "${password}" | grep -Eq '^[A-Za-z0-9_-]{43}$'; then
  printf '%s\n' "Derived password did not match expected format [A-Za-z0-9_-]{43}" >&2
  exit 1
fi

printf '{"password":"%s"}\n' "${password}"
