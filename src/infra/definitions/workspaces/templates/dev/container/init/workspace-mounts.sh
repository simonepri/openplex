#!/usr/bin/env bash
# Verifies EBS persistent storage volumes and S3 filesystem mounts before workspace initialization.

set -euo pipefail

: "${WORKSPACE_BOOT_TOKEN:?workspace boot token was not injected}"

workspace_volume="${1:-/var/lib/workspace}"
runtime_dir="${XDG_RUNTIME_DIR:-/tmp}"
mkdir -p "${runtime_dir}"
chmod 0700 "${runtime_dir}"
mounts_ready_file="${runtime_dir}/workspace-mounts-ready"

write_ready_marker() {
  local file=$1 temporary

  temporary="$(mktemp "${file}.XXXXXX")"
  printf '%s\n' "${WORKSPACE_BOOT_TOKEN}" >"${temporary}"
  mv -f -- "${temporary}" "${file}"
}

printf 'Starting storage mounts verification...\n'

# 1. EBS workspace volume check
if [[ ! -d ${workspace_volume} ]]; then
  printf 'Error: EBS workspace volume directory %s does not exist\n' "${workspace_volume}" >&2
  exit 1
fi

probe_file="${workspace_volume}/.mount-probe-${WORKSPACE_BOOT_TOKEN}"
if ! touch "${probe_file}" 2>/dev/null; then
  printf 'Error: EBS workspace volume %s is not writable\n' "${workspace_volume}" >&2
  exit 1
fi
rm -f -- "${probe_file}"
printf '[mounts] EBS workspace volume verified: %s\n' "${workspace_volume}"

# 2. Local scratch storage check if present
if [[ -d /fs/local ]]; then
  scratch_probe="/fs/local/.mount-probe-${WORKSPACE_BOOT_TOKEN}"
  if touch "${scratch_probe}" 2>/dev/null; then
    rm -f -- "${scratch_probe}"
    printf '[mounts] Local scratch storage verified: /fs/local\n'
  fi
fi

# 3. S3 storage mounts check if present
if [[ -d /fs/s3 ]]; then
  shopt -s nullglob
  for s3_mount in /fs/s3/* /fs/s3/*/* /fs/s3/*/*/*; do
    if [[ -d ${s3_mount} ]]; then
      if ls -d "${s3_mount}" >/dev/null 2>&1; then
        printf '[mounts] S3 mount verified: %s\n' "${s3_mount}"
      else
        printf 'Warning: S3 mount path %s is not accessible\n' "${s3_mount}" >&2
      fi
    fi
  done
  shopt -u nullglob
fi

if [[ -z ${WORKSPACE_S3_TEAMS:-} ]]; then
  printf 'No team storage: %s is not a member of any team on this cell; only legacy research data is mounted.\n' "${USER:-}"
else
  printf 'Team storage: %s; files written outside /fs/s3/<cell>/{home,scratch}/<team> are ephemeral.\n' "${WORKSPACE_S3_TEAMS}"
fi

write_ready_marker "${mounts_ready_file}"
printf 'Storage mounts verified successfully.\n'
