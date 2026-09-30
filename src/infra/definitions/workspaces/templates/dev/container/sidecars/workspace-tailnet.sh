#!/bin/sh
# shellcheck disable=SC2310
# Manages readiness markers for the brokerless workspace tailnet sidecar.

set -eu

socket=${1:-/var/run/workspace/tailnet/tailscaled.sock}
state_dir=$(dirname -- "${socket}")
ready_file="${state_dir}/workspace-tailnet-ready"
ssh_ready_file="${state_dir}/workspace-ssh-ready"
ssh_access=${SSH_ACCESS:-disable}

case "${ssh_access}" in
  enable | disable) ;;
  *)
    printf 'Unsupported SSH access mode: %s\n' "${ssh_access}" >&2
    exit 1
    ;;
esac

cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  rm -f -- "${ready_file}" "${ssh_ready_file}"
  exit "${status}"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

mkdir -p "${state_dir}"
touch "${ready_file}"
if [ "${ssh_access}" = enable ]; then
  touch "${ssh_ready_file}"
fi

while :; do
  sleep 3600 &
  wait $!
done
