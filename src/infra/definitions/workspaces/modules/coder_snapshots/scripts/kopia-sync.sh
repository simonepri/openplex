#!/usr/bin/env bash
# Synchronizes workspace home directories to S3-compatible snapshot repositories using Kopia CLI snapshots.

set -euo pipefail

password_file="${KOPIA_PASSWORD_FILE:-/var/run/workspace/snapshot-repository/password}"
if [[ ! -f ${password_file} ]]; then
  exit 0
fi

# Kopia reads the repository password only from KOPIA_PASSWORD.
KOPIA_PASSWORD="$(<"${password_file}")"
export KOPIA_PASSWORD

workspace_volume="${1:-${TARGET_DIR:-}}"
if [[ -z ${workspace_volume} ]]; then
  if [[ -d /var/lib/workspace ]]; then
    workspace_volume="/var/lib/workspace"
  else
    workspace_volume="/home/developer"
  fi
fi
repository_bucket="${KOPIA_REPOSITORY_BUCKET:-repository}"
owner_id="${CODER_WORKSPACE_OWNER_ID:-00000000-0000-0000-0000-000000000000}"
workspace_name="${CODER_WORKSPACE_NAME:-workspace}"
workspace_machine="${WORKSPACE_MACHINE:-$(hostname)}"
workspace_username="${WORKSPACE_USERNAME:-${USER:-developer}}"
workspace_cell="${WORKSPACE_CELL:-local}"
workspace_cell_incarnation="${WORKSPACE_CELL_INCARNATION:-${workspace_cell}}"
workspace_lineage="${WORKSPACE_LINEAGE:-main}"
parent_lineage="${WORKSPACE_PARENT_LINEAGE:-}"
parent_snapshot="${WORKSPACE_PARENT_SNAPSHOT:-}"
if [[ -z ${parent_snapshot} && -f "${workspace_volume}/.workspace/last-snapshot-selector" ]]; then
  parent_snapshot="$(head -n 1 "${workspace_volume}/.workspace/last-snapshot-selector" 2>/dev/null | tr -d '[:space:]' || true)"
fi
max_bandwidth_mbps="${KOPIA_MAX_BANDWIDTH_MBPS:-0}"
s3_endpoint="${KOPIA_S3_ENDPOINT:-${KOPIA_REPOSITORY_ENDPOINT:-}}"

compute_sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 | cut -d' ' -f1
  else
    openssl dgst -sha256 | cut -d' ' -f2
  fi
}

if [[ -n ${WORKSPACE_IS_ROOT:-} ]]; then
  if [[ ${WORKSPACE_IS_ROOT} == "true" ]]; then
    is_root=true
  else
    is_root=false
  fi
elif [[ -n ${parent_snapshot} || -n ${parent_lineage} ]]; then
  is_root=false
else
  is_root=true
fi

if [[ -n ${SOURCE_HOST_DIGEST:-} ]]; then
  source_host_digest="${SOURCE_HOST_DIGEST}"
elif [[ -n ${KOPIA_RESTORE_SOURCE_HOST_DIGEST:-} ]]; then
  source_host_digest="${KOPIA_RESTORE_SOURCE_HOST_DIGEST}"
else
  source_host_digest="$(printf '%s/%s' "${workspace_machine}" "${workspace_cell}" | compute_sha256)"
fi

if [[ -n ${SCOPE_DIGEST:-} ]]; then
  scope_digest="${SCOPE_DIGEST}"
else
  scope_input="${WORKSPACE_SCOPE:-${workspace_cell}/${workspace_cell_incarnation}}"
  scope_digest="$(printf '%s' "${scope_input}" | compute_sha256)"
fi

if [[ -n ${SNAPSHOT_DISPLAY:-} ]]; then
  display_name="${SNAPSHOT_DISPLAY}"
else
  time_display="$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || date +"%Y-%m-%d")"
  git_branch=""
  if [[ -d "${workspace_volume}/.git" ]] && command -v git >/dev/null 2>&1; then
    git_branch="$(git -C "${workspace_volume}" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  fi
  if [[ -n ${git_branch} ]]; then
    display_name="${workspace_name} | ${time_display} | ${git_branch}"
  else
    display_name="${workspace_name} | ${time_display}"
  fi
fi

kopia_path="$(command -v kopia || true)"
if [[ -n ${KOPIA_CMD:-} ]]; then
  read -ra kopia <<<"${KOPIA_CMD}"
elif [[ -n ${kopia_path} && ${kopia_path} != *"mise"* ]]; then
  kopia=(kopia)
elif command -v mise >/dev/null 2>&1; then
  kopia=(mise exec -- kopia)
elif command -v kopia >/dev/null 2>&1; then
  kopia=(kopia)
else
  kopia=(kopia)
fi

export KOPIA_CHECK_FOR_UPDATES=false
if [[ -n ${KOPIA_REPOSITORY_ACCESS_KEY_ID:-} ]]; then
  export AWS_ACCESS_KEY_ID="${KOPIA_REPOSITORY_ACCESS_KEY_ID}"
fi
if [[ -n ${KOPIA_REPOSITORY_SECRET_ACCESS_KEY:-} ]]; then
  export AWS_SECRET_ACCESS_KEY="${KOPIA_REPOSITORY_SECRET_ACCESS_KEY}"
fi

sync_config_dir=""
if [[ -z ${KOPIA_CONFIG_PATH:-} && -f /tmp/workspace-kopia/repository.config ]]; then
  export KOPIA_CONFIG_PATH=/tmp/workspace-kopia/repository.config
fi

if [[ ${KOPIA_CONFIG_PATH:-} == "/tmp/workspace-kopia/repository.config" ]]; then
  if [[ ${s3_endpoint} == *"s3-gateway"* || ${s3_endpoint} == *"127.0.0.1:19847"* || -z ${s3_endpoint} ]]; then
    s3_endpoint="http://127.0.0.1:19847"
    repository_bucket="repository"
  fi
elif [[ -z ${KOPIA_CONFIG_PATH:-} ]]; then
  sync_config_dir="$(mktemp -d "${TMPDIR:-/tmp}/workspace-kopia-sync.XXXXXX")"
  export KOPIA_CONFIG_PATH="${sync_config_dir}/repository.config"
fi

cleanup() {
  if [[ -n ${sync_config_dir} && -d ${sync_config_dir} ]]; then
    rm -rf -- "${sync_config_dir}"
  fi
}
trap cleanup EXIT

if [[ -d ${workspace_volume} ]]; then
  ignore_file="${workspace_volume}/.kopiaignore"
  touch "${ignore_file}" 2>/dev/null || true
  if [[ -w ${ignore_file} ]]; then
    for pattern in \
      '.restore-*' \
      '.workspace/ssh' \
      '.cache' \
      '.config/kopia' \
      '.local/share/mise' \
      '.npm' \
      '.paseo' \
      '.venv' \
      'node_modules' \
      'venv'; do
      if ! grep -Fqx -- "${pattern}" "${ignore_file}" 2>/dev/null; then
        printf '%s\n' "${pattern}" >>"${ignore_file}" 2>/dev/null || true
      fi
    done
  fi
fi

repository_args=(
  --bucket "${repository_bucket}"
)
if [[ -n ${s3_endpoint} ]]; then
  endpoint_clean="${s3_endpoint#*://}"
  endpoint_clean="${endpoint_clean%/}"
  repository_args+=(--endpoint "${endpoint_clean}")
  if [[ ${s3_endpoint} == http://* || ${KOPIA_S3_DISABLE_TLS:-} == "true" ]]; then
    repository_args+=(--disable-tls)
  fi
fi
if [[ -n ${workspace_machine} ]]; then
  repository_args+=(--override-hostname "${workspace_machine}")
fi
if [[ -n ${workspace_username} ]]; then
  repository_args+=(--override-username "${workspace_username}")
fi

if [[ -n ${KOPIA_MAX_DOWNLOAD_SPEED:-} ]]; then
  repository_args+=(--max-download-speed "${KOPIA_MAX_DOWNLOAD_SPEED}")
elif ((max_bandwidth_mbps > 0)); then
  repository_args+=(--max-download-speed "$((max_bandwidth_mbps * 1024 * 1024))")
fi

if [[ -n ${KOPIA_MAX_UPLOAD_SPEED:-} ]]; then
  repository_args+=(--max-upload-speed "${KOPIA_MAX_UPLOAD_SPEED}")
elif ((max_bandwidth_mbps > 0)); then
  repository_args+=(--max-upload-speed "$((max_bandwidth_mbps * 1024 * 1024))")
fi

if ! "${kopia[@]}" repository status >/dev/null 2>&1; then
  mkdir -p "$(dirname "${KOPIA_CONFIG_PATH}")" 2>/dev/null || true
  "${kopia[@]}" repository connect s3 "${repository_args[@]}" >&2
fi

create_args=(
  snapshot create
  --json
  --tags "cell:${workspace_cell}"
  --tags "lineage:${workspace_lineage}"
  --tags "user-id:${owner_id}"
)
if [[ -n ${workspace_cell_incarnation} ]]; then
  create_args+=(--tags "incarnation:${workspace_cell_incarnation}")
fi
create_args+=("${workspace_volume}")

snapshot_output="$("${kopia[@]}" "${create_args[@]}")"

kopia_snapshot_id="$(printf '%s\n' "${snapshot_output}" | jq -r '
  (if type == "array" then .[0] else . end) |
  (.id // .snapshotId // empty)
' 2>/dev/null || true)"

if [[ -z ${kopia_snapshot_id} ]]; then
  kopia_snapshot_id="${KOPIA_SNAPSHOT_ID:-k$(date +%s)}"
fi

snapshot_id="${SNAPSHOT_ID:-${SNAPSHOT_SELECTOR:-${kopia_snapshot_id}}}"

size_bytes="$(printf '%s\n' "${snapshot_output}" | jq -r '
  (if type == "array" then .[0] else . end) |
  (.summary.totalSizeBytes // .stats.totalSize // .summary.size // .sizeBytes // empty)
' 2>/dev/null || true)"

if [[ -z ${size_bytes} || ${size_bytes} == "null" ]]; then
  if [[ -n ${SNAPSHOT_SIZE_BYTES:-} ]]; then
    size_bytes="${SNAPSHOT_SIZE_BYTES}"
  elif [[ -d ${workspace_volume} ]]; then
    size_bytes="$(du -k -s "${workspace_volume}" 2>/dev/null | awk '{print $1 * 1024}')"
    size_bytes="${size_bytes:-0}"
  else
    size_bytes=0
  fi
fi

files_count="$(printf '%s\n' "${snapshot_output}" | jq -r '
  (if type == "array" then .[0] else . end) |
  (.summary.totalFileCount // .stats.fileCount // .summary.files // .filesCount // empty)
' 2>/dev/null || true)"

if [[ -z ${files_count} || ${files_count} == "null" ]]; then
  if [[ -n ${SNAPSHOT_FILES_COUNT:-} ]]; then
    files_count="${SNAPSHOT_FILES_COUNT}"
  elif [[ -d ${workspace_volume} ]]; then
    files_count="$(find "${workspace_volume}" -type f 2>/dev/null | wc -l | tr -d ' ')"
    files_count="${files_count:-0}"
  else
    files_count=0
  fi
fi

timestamp="${SNAPSHOT_TIMESTAMP:-$(date +%s)}"

manifest_json="$(
  jq -n \
    --argjson schema 3 \
    --arg selector "${snapshot_id}" \
    --arg display "${display_name}" \
    --arg lineageToken "${workspace_lineage}" \
    --arg parentLineage "${parent_lineage}" \
    --arg parentSnapshot "${parent_snapshot}" \
    --argjson isRoot "${is_root}" \
    --arg sourceHostDigest "${source_host_digest}" \
    --arg scopeDigest "${scope_digest}" \
    --argjson timestamp "${timestamp}" \
    --argjson sizeBytes "${size_bytes}" \
    --argjson filesCount "${files_count}" \
    --arg kopiaSnapshotId "${kopia_snapshot_id}" \
    '{
    schema: $schema,
    selector: $selector,
    display: $display,
    lineageToken: $lineageToken,
    parentLineage: $parentLineage,
    parentSnapshot: $parentSnapshot,
    isRoot: $isRoot,
    sourceHostDigest: $sourceHostDigest,
    scopeDigest: $scopeDigest,
    timestamp: $timestamp,
    sizeBytes: $sizeBytes,
    filesCount: $filesCount,
    kopiaSnapshotId: $kopiaSnapshotId
  }'
)"

manifest_file="$(mktemp "${TMPDIR:-/tmp}/snapshot-manifest.XXXXXX")"
printf '%s\n' "${manifest_json}" >"${manifest_file}"

s3_key="owners/${owner_id}/snapshots/${snapshot_id}.json"
s3_uri="s3://${repository_bucket}/${s3_key}"

has_aws=false
if command -v aws >/dev/null 2>&1; then
  aws_bin="$(command -v aws)"
  if [[ ${aws_bin} == *"mise"* ]]; then
    if command -v mise >/dev/null 2>&1 && mise which aws >/dev/null 2>&1; then
      has_aws=true
    fi
  else
    has_aws=true
  fi
fi

if [[ ${has_aws} == "true" ]]; then
  aws_args=(s3 cp "${manifest_file}" "${s3_uri}")
  if [[ -n ${s3_endpoint} ]]; then
    aws_endpoint="${s3_endpoint}"
    if [[ ${aws_endpoint} != http* ]]; then
      aws_endpoint="http://${aws_endpoint}"
    fi
    aws_args+=(--endpoint-url "${aws_endpoint}")
  fi
  # The upload uses the repository keys from the environment; the owner's own AWS
  # config and profile must not change or break it.
  env -u AWS_PROFILE AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null \
    aws "${aws_args[@]}" >&2
elif command -v curl >/dev/null 2>&1; then
  endpoint="${s3_endpoint:-https://s3.amazonaws.com}"
  if [[ ${endpoint} != http* ]]; then
    endpoint="https://${endpoint}"
  fi
  curl_url="${endpoint%/}/${repository_bucket}/${s3_key}"
  curl_args=(-fsSL -X PUT -T "${manifest_file}" -H "Content-Type: application/json")
  s3_key_id="${AWS_ACCESS_KEY_ID:-${KOPIA_REPOSITORY_ACCESS_KEY_ID:-}}"
  s3_secret_key="${AWS_SECRET_ACCESS_KEY:-${KOPIA_REPOSITORY_SECRET_ACCESS_KEY:-}}"
  s3_region="${AWS_DEFAULT_REGION:-${AWS_REGION:-us-east-1}}"
  if [[ -n ${s3_key_id} && -n ${s3_secret_key} ]]; then
    curl_args+=(--aws-sigv4 "aws:amz:${s3_region}:s3" --user "${s3_key_id}:${s3_secret_key}")
  fi
  curl "${curl_args[@]}" "${curl_url}" >&2
else
  printf '%s\n' 'Neither aws nor curl is available for S3 upload' >&2
  rm -f "${manifest_file}"
  exit 1
fi
rm -f "${manifest_file}"

if [[ -d ${workspace_volume} ]]; then
  mkdir -p "${workspace_volume}/.workspace" 2>/dev/null || true
  printf '%s\n' "${snapshot_id}" >"${workspace_volume}/.workspace/last-snapshot-selector" 2>/dev/null || true
fi

printf '%s\n' "${manifest_json}"
