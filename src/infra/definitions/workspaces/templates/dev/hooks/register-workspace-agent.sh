#!/bin/sh
# shellcheck disable=SC2310
# Registers planned workspace agent instances with the enrollment broker before pod creation to obtain auth tokens.

set -eu

: "${CODER_WORKSPACE_AGENT_REGISTRATION_URL:?workspace-agent registration URL was not injected}"
: "${CODER_WORKSPACE_AGENT_REGISTRATION_TOKEN_FILE:?workspace-agent registration token path was not injected}"
: "${CODER_WORKSPACE_OWNER_SESSION_TOKEN:?Coder owner session token is required}"
: "${CODER_AGENT_TOKEN:?Coder agent token is required}"
: "${CODER_WORKSPACE_BUILD_ID:?Coder build ID is required}"
: "${WORKSPACE_CELL:?workspace cell is required}"
: "${WORKSPACE_CELL_INCARNATION:?cell incarnation is required}"
: "${CODER_WORKSPACE_IS_PREBUILD_CLAIM:?Coder prebuild-claim state is required}"
: "${WORKSPACE_MACHINE:?workspace machine name is required}"
: "${CODER_WORKSPACE_OWNER_ID:?workspace owner ID is required}"
: "${WORKSPACE_TEAM:?workspace team is required}"
: "${CODER_WORKSPACE_ID:?workspace ID is required}"
: "${WORKSPACE_LINEAGE:?workspace lineage is required}"
: "${CODER_WORKSPACE_NAME:?workspace name is required}"

workspace_volume=/var/lib/workspace

case "${CODER_WORKSPACE_AGENT_REGISTRATION_URL}" in
  https://headscale-workspace-registration.headscale.svc.cluster.local:8443/v1/workspace-agents/register) ;;
  *)
    printf '%s\n' 'workspace-agent registration must use the internal TLS service' >&2
    exit 2
    ;;
esac
case "${CODER_WORKSPACE_AGENT_REGISTRATION_TOKEN_FILE}" in
  /*) ;;
  *)
    printf '%s\n' 'workspace-agent registration token path must be absolute' >&2
    exit 2
    ;;
esac
[ -r "${CODER_WORKSPACE_AGENT_REGISTRATION_TOKEN_FILE}" ] || {
  printf '%s\n' 'workspace-agent registration token is not readable' >&2
  exit 2
}

require_match() {
  name=$1
  value=$2
  pattern=$3
  if ! printf '%s\n' "${value}" | grep -Eq "${pattern}"; then
    printf '%s is invalid\n' "${name}" >&2
    exit 2
  fi
}

uuid_pattern='^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
dns_label_pattern='^[a-z][a-z0-9-]{1,61}[a-z0-9]$'
require_match 'Coder agent token' "${CODER_AGENT_TOKEN}" "${uuid_pattern}"
require_match 'Coder build ID' "${CODER_WORKSPACE_BUILD_ID}" "${uuid_pattern}"
require_match 'workspace cell' "${WORKSPACE_CELL}" "${dns_label_pattern}"
require_match 'cell incarnation' "${WORKSPACE_CELL_INCARNATION}" '^[a-z0-9][a-z0-9._-]{0,62}$'
require_match 'workspace machine name' "${WORKSPACE_MACHINE}" "${dns_label_pattern}"
require_match 'workspace owner ID' "${CODER_WORKSPACE_OWNER_ID}" "${uuid_pattern}"
require_match 'workspace team' "${WORKSPACE_TEAM}" "${dns_label_pattern}"
require_match 'workspace ID' "${CODER_WORKSPACE_ID}" "${uuid_pattern}"
require_match 'workspace lineage' "${WORKSPACE_LINEAGE}" '^([0-9a-f-]{36}-[0-9]{10,}|[0-9a-f]{40})$'
require_match 'workspace name' "${CODER_WORKSPACE_NAME}" "${dns_label_pattern}"
WORKSPACE_IS_ROOT="${WORKSPACE_IS_ROOT:-}"
WORKSPACE_PARENT_LINEAGE="${WORKSPACE_PARENT_LINEAGE:-}"
WORKSPACE_PARENT_SNAPSHOT="${WORKSPACE_PARENT_SNAPSHOT:-}"

if [ -n "${WORKSPACE_IS_ROOT}" ]; then
  case "${WORKSPACE_IS_ROOT}" in
    true | false) ;;
    *)
      printf 'workspace isRoot is invalid\n' >&2
      exit 2
      ;;
  esac
fi
if [ -n "${WORKSPACE_PARENT_LINEAGE}" ]; then
  require_match 'workspace parent lineage' "${WORKSPACE_PARENT_LINEAGE}" '^([0-9a-f-]{36}-[0-9]{10,}|[0-9a-f]{40})$'
fi
if [ -n "${WORKSPACE_PARENT_SNAPSHOT}" ]; then
  require_match 'workspace parent snapshot' "${WORKSPACE_PARENT_SNAPSHOT}" '^[A-Za-z0-9._-]+$'
fi
[ "${CODER_WORKSPACE_IS_PREBUILD_CLAIM}" = false ] || {
  printf '%s\n' 'prebuild claims cannot register workspace agents' >&2
  exit 2
}
service_account_token=$(cat "${CODER_WORKSPACE_AGENT_REGISTRATION_TOKEN_FILE}")
require_match 'workspace-agent registration token' "${service_account_token}" '^[A-Za-z0-9._~-]+$'
require_match 'Coder owner session token' "${CODER_WORKSPACE_OWNER_SESSION_TOKEN}" '^[A-Za-z0-9._~+/=-]+$'

agent_token=${CODER_AGENT_TOKEN}
owner_session_token=${CODER_WORKSPACE_OWNER_SESSION_TOKEN}
unset CODER_AGENT_TOKEN CODER_WORKSPACE_OWNER_SESSION_TOKEN
registration_response_file=$(mktemp)
trap 'rm -f "$registration_response_file"' EXIT
registration_reason=unavailable

request_body=$(printf '%s' "{\"agentToken\":\"${agent_token}\",\"buildId\":\"${CODER_WORKSPACE_BUILD_ID}\",\"cell\":\"${WORKSPACE_CELL}\",\"incarnation\":\"${WORKSPACE_CELL_INCARNATION}\",\"isPrebuildClaim\":false")
request_body=$(printf '%s,"lineage":"%s"' "${request_body}" "${WORKSPACE_LINEAGE}")
if [ -n "${WORKSPACE_PARENT_LINEAGE}" ]; then
  request_body=$(printf '%s,"parentLineage":"%s"' "${request_body}" "${WORKSPACE_PARENT_LINEAGE}")
fi
if [ -n "${WORKSPACE_PARENT_SNAPSHOT}" ]; then
  request_body=$(printf '%s,"parentSnapshot":"%s"' "${request_body}" "${WORKSPACE_PARENT_SNAPSHOT}")
fi
request_body=$(printf '%s,"machine":"%s","ownerId":"%s","team":"%s","workspaceId":"%s","workspaceName":"%s","workspaceVolume":"%s"}' \
  "${request_body}" "${WORKSPACE_MACHINE}" "${CODER_WORKSPACE_OWNER_ID}" "${WORKSPACE_TEAM}" "${CODER_WORKSPACE_ID}" "${CODER_WORKSPACE_NAME}" "${workspace_volume}")
curl_request_body=$(printf '%s' "${request_body}" | sed 's/\\/\\\\/g; s/"/\\"/g')

request_registration() {
  if response=$(
    printf 'silent\nshow-error\nrequest = "POST"\nurl = "%s"\nheader = "Authorization: Bearer %s"\nheader = "Coder-Session-Token: %s"\nheader = "Content-Type: application/json"\ndata-binary = "%s"\nconnect-timeout = 5\nmax-time = 15\nnoproxy = "*"\n' \
      "${CODER_WORKSPACE_AGENT_REGISTRATION_URL}" \
      "${service_account_token}" \
      "${owner_session_token}" \
      "${curl_request_body}" \
      | curl -q \
        --config - \
        --output "${registration_response_file}" \
        --write-out '%{http_code} %{size_download}'
  ); then
    :
  else
    return 75
  fi
  reason=$(cat "${registration_response_file}")
  : >"${registration_response_file}"
  case "${reason}" in
    'invalid workspace agent registration' | 'owner identity binding conflict' | \
      'unauthorized' | 'workspace agent registration conflict' | \
      'workspace agent registration unavailable' | \
      'workspace build does not match registration') registration_reason=${reason} ;;
    *) registration_reason=unavailable ;;
  esac
  case "${response}" in
    '202 0') return 0 ;;
    '401 0' | '409 0') return 64 ;;
    '429 0' | '502 0' | '503 0' | '504 0') return 75 ;;
    *) return 65 ;;
  esac
}

monotonic_seconds() {
  awk '{ print int($1) }' /proc/uptime
}

retry_registration() {
  retry_window=885
  retry_interval=5
  started_at=$(monotonic_seconds) || return 1
  deadline=$((started_at + retry_window))
  while :; do
    sleep "${retry_interval}" || return 1
    now=$(monotonic_seconds) || return 1
    [ "${now}" -lt "${deadline}" ] || return 0
    if request_registration; then
      :
    else
      status=$?
      case "${status}" in
        75) ;;
        *) return 0 ;;
      esac
    fi
  done
}

response=''
if ! request_registration; then
  http_status=${response%% *}
  case "${http_status}" in
    400 | 401 | 409 | 429 | 502 | 503 | 504) ;;
    *) http_status=unavailable ;;
  esac
  printf 'workspace-agent registration request was not accepted (HTTP %s: %s)\n' \
    "${http_status}" "${registration_reason}" >&2
  exit 1
fi

# The retry child retains the two credentials only in shell memory. It has no
# inherited file descriptors or environment entries carrying those values.
# Subshells do not inherit the EXIT trap, and the child recreates the response
# file after this shell has removed it, so the child removes it on exit too.
trap '' HUP
(
  trap 'rm -f "$registration_response_file"' EXIT
  retry_registration
) </dev/null >/dev/null 2>&1 &
retry_pid=$!
trap - HUP
if ! kill -0 "${retry_pid}" 2>/dev/null; then
  printf '%s\n' 'workspace-agent registration retry could not detach' >&2
  exit 1
fi
