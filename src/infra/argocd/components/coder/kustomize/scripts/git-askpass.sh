#!/bin/sh
# Supplies read-only Git credentials for template checkout without leaking CLI arguments.

set -eu

: "${GIT_USERNAME_FILE:?}"
: "${GIT_TOKEN_FILE:?}"

case "${1:-}" in
  *Username*) credential_file="${GIT_USERNAME_FILE}" ;;
  *Password*) credential_file="${GIT_TOKEN_FILE}" ;;
  *) exit 1 ;;
esac

if [ ! -f "${credential_file}" ] || [ -L "${credential_file}" ]; then
  exit 1
fi
credential=$(cat "${credential_file}")
case "${credential}" in
  '' | *'
'*) exit 1 ;;
  *) ;;
esac
printf '%s\n' "${credential}"
