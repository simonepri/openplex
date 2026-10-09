#!/usr/bin/env bash
# Resolves workspace snapshot selectors, executes Kopia repository restore, and ensures atomic cutover.

set -euo pipefail

export KOPIA_CHECK_FOR_UPDATES=false
export KOPIA_CONFIG_PATH="${KOPIA_CONFIG_PATH:-/tmp/workspace-kopia/repository.config}"

password_file="${KOPIA_PASSWORD_FILE:-/var/run/workspace/snapshot-repository/password}"
if [[ -f ${password_file} ]]; then
  # Kopia reads the repository password only from KOPIA_PASSWORD.
  KOPIA_PASSWORD="$(<"${password_file}")"
  export KOPIA_PASSWORD
fi

workspace_volume="${TARGET_DIR:-${1:-/home/developer}}"

snapshot="${KOPIA_RESTORE_SELECTOR:-}"
if [[ -z ${snapshot} || ${snapshot} == "__start-fresh__" ]]; then
  exit 0
fi

if [[ ! ${snapshot} =~ ^[A-Za-z0-9._-]+$ ]]; then
  printf '%s\n' 'restore selector is invalid' >&2
  exit 1
fi

marker="${workspace_volume}/.workspace/restored/${snapshot}"

# Optional manifest validation if a manifest file is passed
if [[ -n ${KOPIA_MANIFEST_PATH:-} && -f ${KOPIA_MANIFEST_PATH:-} ]]; then
  manifest_selector="$(grep -o '"selector"[[:space:]]*:[[:space:]]*"[^"]*"' "${KOPIA_MANIFEST_PATH}" | head -n1 | sed -E 's/.*"selector"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/' || true)"
  if [[ -n ${manifest_selector} && ${manifest_selector} != "${snapshot}" ]]; then
    printf 'Manifest selector %s does not match requested selector %s\n' "${manifest_selector}" "${snapshot}" >&2
    exit 1
  fi
fi

staging_dir="${workspace_volume}/.restore-staging"
backup_dir="${workspace_volume}/.restore-backup"
cutover_in_progress=0

# shellcheck disable=SC2329 # invoked indirectly via trap
cleanup() {
  local exit_code=$?
  if [[ ${exit_code} -ne 0 ]]; then
    if [[ ${cutover_in_progress} -eq 1 && -d ${backup_dir} ]]; then
      shopt -s dotglob nullglob
      local entry name item
      for entry in "${workspace_volume}"/*; do
        name="$(basename "${entry}")"
        if [[ ${name} == .restore-* ]]; then
          continue
        fi
        if [[ -d ${entry} && ! -L ${entry} && -d "${backup_dir}/${name}" && ! -L "${backup_dir}/${name}" ]]; then
          for item in "${entry}"/*; do
            rm -rf -- "${item}"
          done
        else
          rm -rf -- "${entry}"
        fi
      done
      for entry in "${backup_dir}"/*; do
        name="$(basename "${entry}")"
        if [[ -d ${entry} && ! -L ${entry} && -d "${workspace_volume}/${name}" && ! -L "${workspace_volume}/${name}" ]]; then
          for item in "${entry}"/*; do
            mv -- "${item}" "${workspace_volume}/${name}/"
          done
        else
          mv -- "${entry}" "${workspace_volume}/${name}"
        fi
      done
      shopt -u dotglob nullglob
    fi
  fi
  rm -rf -- "${staging_dir}" "${backup_dir}"
}
trap cleanup EXIT

# Clean any stale staging from interrupted previous attempts
rm -rf -- "${staging_dir}"
mkdir -p "${staging_dir}"

# Configure Kopia command
kopia_bin="kopia"
if ! command -v "${kopia_bin}" >/dev/null 2>&1; then
  if command -v mise >/dev/null 2>&1; then
    kopia_cmd=(mise exec -- kopia)
  else
    printf '%s\n' 'kopia executable not found' >&2
    exit 1
  fi
else
  kopia_cmd=("${kopia_bin}")
fi

restore_args=(snapshot restore --progress "${snapshot}")
if [[ ${KOPIA_MAX_BANDWIDTH_MBPS:-0} -gt 0 ]]; then
  bytes_per_sec=$((KOPIA_MAX_BANDWIDTH_MBPS * 1024 * 1024))
  restore_args+=(--max-download-speed "${bytes_per_sec}")
fi
restore_args+=("${staging_dir}")

printf 'Restoring snapshot %s with Kopia...\n' "${snapshot}" >&2
"${kopia_cmd[@]}" "${restore_args[@]}" >&2

# Verify staging directory has restored files
staged_count=0
shopt -s dotglob nullglob
for _ in "${staging_dir}"/*; do
  ((staged_count++)) || true
done
shopt -u dotglob nullglob

if [[ ${staged_count} -eq 0 ]]; then
  printf 'Restore failed: staging directory is empty\n' >&2
  exit 1
fi

# Atomic cutover phase
mkdir -p "${backup_dir}"

shopt -s dotglob nullglob
for entry in "${workspace_volume}"/*; do
  name="$(basename "${entry}")"
  if [[ ${name} == .restore-* ]]; then
    continue
  fi
  if [[ -d ${entry} && ! -L ${entry} && -d "${staging_dir}/${name}" && ! -L "${staging_dir}/${name}" ]]; then
    mkdir -p "${backup_dir}/${name}"
    for item in "${entry}"/*; do
      mv -- "${item}" "${backup_dir}/${name}/"
    done
  else
    mv -- "${entry}" "${backup_dir}/${name}"
  fi
done

cutover_in_progress=1

if [[ ${SIMULATE_CUTOVER_FAILURE:-} == "true" ]]; then
  printf 'Simulating cutover failure\n' >&2
  exit 1
fi

for entry in "${staging_dir}"/*; do
  name="$(basename "${entry}")"
  if [[ -d ${entry} && ! -L ${entry} && -d "${workspace_volume}/${name}" && ! -L "${workspace_volume}/${name}" ]]; then
    for item in "${entry}"/*; do
      mv -- "${item}" "${workspace_volume}/${name}/"
    done
  else
    mv -- "${entry}" "${workspace_volume}/${name}"
  fi
done
shopt -u dotglob nullglob

# Mark restore success
mkdir -p "${workspace_volume}/.workspace/restored"
touch "${marker}"
printf '%s\n' "${snapshot}" >"${workspace_volume}/.workspace/last-snapshot-selector" 2>/dev/null || true

cutover_in_progress=0
rm -rf -- "${staging_dir}" "${backup_dir}"
printf 'restored\n'
exit 0
