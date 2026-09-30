#!/usr/bin/env bash
# Scan and preview upstream dependency updates locally via Renovate with ambient credentials.

set -euo pipefail

export GITHUB_COM_TOKEN="${GITHUB_COM_TOKEN:-$(gh auth token 2>/dev/null || true)}"
if [ -z "${RENOVATE_HOST_RULES:-}" ] && command -v docker-credential-osxkeychain >/dev/null 2>&1; then
  DOCKER_CREDS="$(echo "https://index.docker.io/v1/" | docker-credential-osxkeychain get 2>/dev/null || true)"
  if [ -n "${DOCKER_CREDS:-}" ]; then
    D_USER="$(echo "${DOCKER_CREDS}" | jq -r .Username 2>/dev/null || true)"
    D_PASS="$(echo "${DOCKER_CREDS}" | jq -r .Secret 2>/dev/null || true)"
    if [ -n "${D_USER:-}" ] && [ "${D_USER}" != "null" ]; then
      export RENOVATE_HOST_RULES="[{\"matchHost\": \"docker.io\", \"username\": \"${D_USER}\", \"password\": \"${D_PASS}\"}, {\"matchHost\": \"index.docker.io\", \"username\": \"${D_USER}\", \"password\": \"${D_PASS}\"}]"
    fi
  fi
fi
exec renovate --platform=local --repository-cache=reset "$@"
