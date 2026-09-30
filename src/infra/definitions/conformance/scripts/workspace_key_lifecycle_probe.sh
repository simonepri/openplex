#!/bin/sh
# Exercises Tailscale auth key consumption inside workspace pods to defend single-use constraints and ten-minute expiration against credential reuse.

# shellcheck disable=SC2310
set -eu

: "${CODER_AGENT_TOKEN:?Coder agent token was not injected}"
: "${HEADSCALE_URL:?Headscale URL was not injected}"
: "${WORKSPACE_CELL:?workspace cell was not injected}"
: "${WORKSPACE_MACHINE:?workspace machine name was not injected}"

probe_prefix="${1:?probe prefix was not supplied}"
case "${probe_prefix}" in
  '' | *[!a-z0-9-]* | -* | *-)
    printf 'Invalid lifecycle probe prefix.\n' >&2
    exit 1
    ;;
  *) ;;
esac

daemon_pid=
socket=

stop_daemon() {
  if [ -n "${daemon_pid}" ]; then
    kill "${daemon_pid}" 2>/dev/null || true
    wait "${daemon_pid}" 2>/dev/null || true
    daemon_pid=
  fi
  if [ -n "${socket}" ]; then
    rm -f -- "${socket}"
    socket=
  fi
}
trap stop_daemon EXIT HUP INT TERM

request_key() {
  body="$(printf '{"cluster":"%s","machine":"%s"}' "${WORKSPACE_CELL}" "${WORKSPACE_MACHINE}")"
  enrollment="$(
    wget -qO- \
      --header="Authorization: Bearer ${CODER_AGENT_TOKEN}" \
      --header='Content-Type: application/json' \
      --post-data="${body}" \
      "${HEADSCALE_URL}/v1/enroll"
  )"
  key="$(printf '%s' "${enrollment}" | sed -n 's/.*"authKey":"\([^"]*\)".*/\1/p')"
  if [ -z "${key}" ]; then
    printf 'Enrollment broker did not return a key.\n' >&2
    return 1
  fi
  printf '%s' "${key}"
}

start_daemon() {
  name="${1}"
  socket="/tmp/${name}.sock"
  log="/tmp/${name}.log"
  rm -f -- "${socket}" "${log}"
  tailscaled \
    --tun=userspace-networking \
    --port=0 \
    --state=mem: \
    --socket="${socket}" \
    --no-logs-no-support \
    >"${log}" 2>&1 &
  daemon_pid="$!"
  attempt=0
  while [ ! -S "${socket}" ]; do
    attempt=$((attempt + 1))
    if [ "${attempt}" -eq 100 ]; then
      printf 'Lifecycle probe daemon did not create its socket.\n' >&2
      return 1
    fi
    sleep 0.1
  done
}

use_key() {
  name="${1}"
  key="${2}"
  tailscale --socket="${socket}" up \
    --reset \
    --accept-dns=false \
    --accept-routes=false \
    --auth-key="${key}" \
    --hostname="${name}" \
    --login-server="${HEADSCALE_URL}" \
    --ssh=false \
    --timeout=30s \
    >/dev/null 2>&1
}

used_name="${probe_prefix}-used"
reuse_name="${probe_prefix}-reuse"
expired_name="${probe_prefix}-expired"
fresh_name="${probe_prefix}-fresh"

used_key="$(request_key)"
start_daemon "${used_name}"
if ! use_key "${used_name}" "${used_key}"; then
  printf 'Fresh single-use key was rejected.\n' >&2
  exit 1
fi
stop_daemon

start_daemon "${reuse_name}"
if use_key "${reuse_name}" "${used_key}"; then
  printf 'Consumed single-use key was accepted again.\n' >&2
  exit 1
fi
stop_daemon
unset used_key

expired_key="$(request_key)"
sleep 615
start_daemon "${expired_name}"
if use_key "${expired_name}" "${expired_key}"; then
  printf 'Expired enrollment key was accepted.\n' >&2
  exit 1
fi
stop_daemon
unset expired_key

fresh_key="$(request_key)"
start_daemon "${fresh_name}"
if ! use_key "${fresh_name}" "${fresh_key}"; then
  printf 'Fresh positive-control key was rejected after the expiry wait.\n' >&2
  exit 1
fi
stop_daemon
unset fresh_key

printf 'Workspace enrollment keys are single-use and expire after ten minutes.\n'
