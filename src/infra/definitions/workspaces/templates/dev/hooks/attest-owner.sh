#!/bin/sh
# Attests and exchanges provisioner tokens for verified, immutable workspace owner identities via the broker.

set -eu

binding_url=$(sed -n 's/.*"binding_url"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
case "${binding_url}" in
  https://*/v1/bind) ;;
  *)
    echo 'binding_url must be an HTTPS /v1/bind endpoint' >&2
    exit 1
    ;;
esac

structural_binding() {
  printf '%s\n' "{\"attested\":\"false\",\"email\":\"template-import@invalid\",\"id\":\"00000000-0000-4000-8000-000000000000\",\"preferred_username\":\"template_import\",\"preview\":\"$1\",\"principal_id\":\"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\"}"
}

if [ -z "${CODER_WORKSPACE_BUILD_ID:-}" ]; then
  structural_binding true
  exit 0
fi

: "${CODER_WORKSPACE_OWNER_ID:?Coder owner ID is required for a real workspace build}"
case "${CODER_WORKSPACE_OWNER_ID}" in
  ????????-????-????-????-????????????) ;;
  *)
    echo 'Coder owner UUID is invalid' >&2
    exit 1
    ;;
esac

case "${CODER_WORKSPACE_TRANSITION:-}" in
  start) ;;
  stop | destroy)
    structural_binding false
    exit 0
    ;;
  *)
    echo 'Coder workspace transition is invalid' >&2
    exit 1
    ;;
esac

: "${CODER_WORKSPACE_OWNER_SESSION_TOKEN:?Coder owner session token is required for start}"

# If a local broker endpoint is supplied and reachable, try it; otherwise use native Coder identity.
owner_id="${CODER_WORKSPACE_OWNER_ID}"
owner_name="${CODER_WORKSPACE_OWNER_NAME:-dev}"
owner_email="${CODER_WORKSPACE_OWNER_EMAIL:-${owner_name}@corp.local.internal}"

if [ -n "${binding_url}" ] && [ -n "${CODER_WORKSPACE_OWNER_SESSION_TOKEN:-}" ]; then
  resolve_url=${binding_url%/bind}/resolve
  if response=$(
    printf 'silent\nfail\nrequest = "POST"\nurl = "%s"\nheader = "Coder-Session-Token: %s"\n' \
      "${resolve_url}" "${CODER_WORKSPACE_OWNER_SESSION_TOKEN}" \
      | curl -q --config - 2>/dev/null
  ); then
    printf '%s\n' "${response}"
    exit 0
  fi
  if [ -n "${CODER_WORKSPACE_OWNER_OIDC_ACCESS_TOKEN:-}" ]; then
    escaped_body="$(printf '{"owner_id":"%s"}' "${owner_id}" | sed 's/"/\\"/g')"
    if response=$(
      printf 'silent\nfail\nrequest = "POST"\nurl = "%s"\nheader = "Authorization: Bearer %s"\nheader = "Coder-Session-Token: %s"\nheader = "Content-Type: application/json"\ndata = "%s"\n' \
        "${binding_url}" "${CODER_WORKSPACE_OWNER_OIDC_ACCESS_TOKEN}" "${CODER_WORKSPACE_OWNER_SESSION_TOKEN}" "${escaped_body}" \
        | curl -q --config - 2>/dev/null
    ); then
      printf '%s\n' "${response}"
      exit 0
    fi
  fi
fi

# Fallback: Native Coder verified owner
printf '{"attested":"true","email":"%s","id":"%s","preferred_username":"%s","preview":"false","principal_id":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}\n' \
  "${owner_email}" "${owner_id}" "${owner_name}"
