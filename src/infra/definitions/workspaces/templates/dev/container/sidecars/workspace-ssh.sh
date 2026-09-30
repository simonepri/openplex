#!/bin/sh
# Configures host keys and starts an unprivileged loopback OpenSSH server daemon for direct workspace connections.

set -eu

if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
  printf '%s\n' 'usage: workspace-ssh.sh RUNTIME_DIRECTORY RESTORE_READY_FILE [WORKSPACE_VOLUME]' >&2
  exit 2
fi

: "${WORKSPACE_USERNAME:?workspace user was not injected}"
: "${CODER_WORKSPACE_NAME:?workspace name was not injected}"
: "${WORKSPACE_BOOT_TOKEN:?workspace boot token was not injected}"
: "${SSH_ACCESS:=disable}"

runtime_dir="$1"
restore_ready_file="$2"
workspace_volume="${3:-/var/lib/workspace}"
restore_wait_limit=3600
host_key_dir="${workspace_volume}/.workspace/ssh"
host_key="${host_key_dir}/ssh_host_ed25519_key"
authorized_keys="${host_key_dir}/authorized_keys"
access_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
identity_script="${access_dir}/workspace-identity.sh"
ssh_keygen="$(command -v ssh-keygen)"
sshd="$(command -v sshd)"
case "${SSH_ACCESS}" in
  disable) ;;
  enable)
    if [ -z "${SSH_PUBLIC_KEY:-}" ]; then
      printf '%s\n' 'SSH_PUBLIC_KEY must be nonempty when SSH access is enabled.' >&2
      exit 1
    fi
    ;;
  *)
    printf 'Unsupported SSH access mode: %s\n' "${SSH_ACCESS}" >&2
    exit 1
    ;;
esac
if [ -n "${KOPIA_RESTORE_SELECTOR:-}" ]; then
  attempt=0
  while :; do
    if [ -f "${restore_ready_file}" ]; then
      ready_token=$(cat "${restore_ready_file}")
      if [ "${ready_token}" = "${WORKSPACE_BOOT_TOKEN}" ]; then
        break
      fi
    fi
    attempt=$((attempt + 1))
    if [ "${attempt}" -ge "${restore_wait_limit}" ]; then
      printf '%s\n' 'Workspace restore did not finish before the SSH identity deadline.' >&2
      exit 1
    fi
    sleep 1
  done
fi
if [ "${SSH_ACCESS}" = disable ]; then
  rm -f -- "${authorized_keys}"
  exit 0
fi
umask 077
ignore_file="${workspace_volume}/.kopiaignore"
touch "${ignore_file}"
if ! grep -Fqx -- '.workspace/ssh' "${ignore_file}"; then
  printf '%s\n' '.workspace/ssh' >>"${ignore_file}"
fi
mkdir -p "${host_key_dir}"
chmod 700 "${host_key_dir}"

"${identity_script}" "${WORKSPACE_USERNAME}" "${runtime_dir}"
printf '%s\n' "${SSH_PUBLIC_KEY}" >"${authorized_keys}"
chmod 600 "${authorized_keys}"

if [ ! -f "${host_key}" ]; then
  "${ssh_keygen}" -q -t ed25519 -N '' -f "${host_key}"
fi
chmod 600 "${host_key}"

nss_wrapper="$(find /usr/lib -name libnss_wrapper.so -print -quit)"
export LD_PRELOAD="${nss_wrapper}"
export NSS_WRAPPER_PASSWD="${runtime_dir}/passwd"
export NSS_WRAPPER_GROUP="${runtime_dir}/group"
session_environment="LD_PRELOAD=${nss_wrapper} NSS_WRAPPER_GROUP=${runtime_dir}/group NSS_WRAPPER_PASSWD=${runtime_dir}/passwd MISE_GLOBAL_CONFIG_FILE=/etc/workspace/config/config.toml WORKSPACE_USERNAME=${WORKSPACE_USERNAME} CODER_WORKSPACE_NAME=${CODER_WORKSPACE_NAME} PATH=/home/coder/.local/share/mise/shims:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin${CODER_URL:+ CODER_URL=${CODER_URL}}${CODER_SESSION_TOKEN:+ CODER_SESSION_TOKEN=${CODER_SESSION_TOKEN}}${CODER_CLIENT_TLS_CA_FILE:+ CODER_CLIENT_TLS_CA_FILE=${CODER_CLIENT_TLS_CA_FILE}}"

exec "${sshd}" -D -e -f /dev/null \
  -o AllowUsers="${WORKSPACE_USERNAME}" \
  -o AuthorizedKeysFile="${authorized_keys}" \
  -o HostKey="${host_key}" \
  -o KbdInteractiveAuthentication=no \
  -o ListenAddress=0.0.0.0 \
  -o PasswordAuthentication=no \
  -o PermitRootLogin=no \
  -o PidFile="${runtime_dir}/sshd.pid" \
  -o Port=2222 \
  -o SetEnv="${session_environment}" \
  -o StrictModes=no \
  -o UsePAM=no
