#!/bin/sh
# shellcheck disable=SC2312
# Tests snapshot synchronization workflows, manifest generation, and retention handling in kopia-sync.sh.

set -eu

subject="${1:-$(cd -- "$(dirname -- "$0")" && pwd)/kopia-sync.sh}"
if [ ! -f "${subject}" ]; then
  printf 'Error: subject script %s not found\n' "${subject}" >&2
  exit 1
fi

jq_bin="${2:-$(command -v jq)}"
if [ ! -f "${jq_bin}" ] && ! command -v "${jq_bin}" >/dev/null 2>&1; then
  printf 'Error: jq not found at %s\n' "${jq_bin}" >&2
  exit 1
fi

test_dir="$(mktemp -d)"
cleanup() {
  rm -rf "${test_dir}"
}
trap cleanup EXIT

mkdir -p "${test_dir}/bin" "${test_dir}/volume_1" "${test_dir}/volume_2" "${test_dir}/volume_3"
cp "${jq_bin}" "${test_dir}/bin/jq"
PATH="${test_dir}/bin:${PATH}"
export PATH

# Create mock kopia
cat >"${test_dir}/bin/kopia" <<"MOCK_EOF"
#!/bin/sh
set -eu
cmd="$1 $2"
case "$cmd" in
  "repository status")
    if [ -f "${MOCK_KOPIA_CONNECTED:-}" ]; then
      exit 0
    fi
    exit 1
    ;;
  "repository connect")
    printf '%s\n' "$*" >>"${MOCK_CONNECT_LOG:?}"
    touch "${MOCK_KOPIA_CONNECTED:?}"
    exit 0
    ;;
  "snapshot create")
    printf '%s\n' "$*" >>"${MOCK_CREATE_LOG:?}"
    if [ -n "${MOCK_SNAPSHOT_OUTPUT:-}" ]; then
      printf '%s\n' "$MOCK_SNAPSHOT_OUTPUT"
    else
      cat <<"JSON_EOF"
[{"id":"snap-mock-123","summary":{"totalSizeBytes":4096,"totalFileCount":12}}]
JSON_EOF
    fi
    exit 0
    ;;
  *)
    printf 'Unexpected kopia invocation: %s\n' "$*" >&2
    exit 99
    ;;
esac
MOCK_EOF
chmod +x "${test_dir}/bin/kopia"

# Create mock aws
cat >"${test_dir}/bin/aws" <<"MOCK_EOF"
#!/bin/sh
set -eu
if [ "$1" = "s3" ] && [ "$2" = "cp" ]; then
  src="$3"
  dst="$4"
  printf '%s -> %s\n' "$src" "$dst" >>"${MOCK_AWS_LOG:?}"
  cat "$src" >"${MOCK_UPLOADED_MANIFEST:?}"
  exit 0
fi
printf 'Unexpected aws command: %s\n' "$*" >&2
exit 98
MOCK_EOF
chmod +x "${test_dir}/bin/aws"

# -----------------------------------------------------------------------------
# Test 1: Schema 3 manifest JSON generation and validation with jq
# -----------------------------------------------------------------------------
printf 'Running Test 1: Schema 3 manifest JSON generation...\n'

connect_log="${test_dir}/connect_1.log"
create_log="${test_dir}/create_1.log"
aws_log="${test_dir}/aws_1.log"
uploaded_manifest="${test_dir}/uploaded_manifest_1.json"
connected_marker="${test_dir}/connected_1.marker"

MOCK_CONNECT_LOG="${connect_log}" \
  MOCK_CREATE_LOG="${create_log}" \
  MOCK_AWS_LOG="${aws_log}" \
  MOCK_UPLOADED_MANIFEST="${uploaded_manifest}" \
  MOCK_KOPIA_CONNECTED="${connected_marker}" \
  PATH="${test_dir}/bin:${PATH}" \
  WORKSPACE_LINEAGE="lineage-abc-123" \
  WORKSPACE_PARENT_LINEAGE="lineage-parent-000" \
  WORKSPACE_PARENT_SNAPSHOT="snap-parent-999" \
  WORKSPACE_IS_ROOT="false" \
  CODER_WORKSPACE_OWNER_ID="123e4567-e89b-42d3-a456-426614174000" \
  KOPIA_REPOSITORY_BUCKET="test-snapshots-bucket" \
  SNAPSHOT_ID="snap-test-001" \
  SNAPSHOT_DISPLAY="Workspace Dev | 2026-09-19 | feature-branch" \
  WORKSPACE_CELL="cell-aws-usw2" \
  WORKSPACE_CELL_INCARNATION="cell-aws-usw2-1" \
  WORKSPACE_MACHINE="machine-test-host" \
  WORKSPACE_USERNAME="developer" \
  "${subject}" "${test_dir}/volume_1" >/dev/null

# Verify manifest exists and matches schema 3
if [ ! -f "${uploaded_manifest}" ]; then
  printf 'FAIL: Uploaded manifest file not created\n' >&2
  exit 1
fi

schema_val="$(jq -r .schema "${uploaded_manifest}")"
if [ "${schema_val}" != "3" ]; then
  printf 'FAIL: Expected schema 3, got: %s\n' "${schema_val}" >&2
  exit 1
fi

selector_val="$(jq -r .selector "${uploaded_manifest}")"
if [ "${selector_val}" != "snap-test-001" ]; then
  printf 'FAIL: Expected selector snap-test-001, got: %s\n' "${selector_val}" >&2
  exit 1
fi

display_val="$(jq -r .display "${uploaded_manifest}")"
if [ "${display_val}" != "Workspace Dev | 2026-09-19 | feature-branch" ]; then
  printf 'FAIL: Expected display string, got: %s\n' "${display_val}" >&2
  exit 1
fi

lineage_val="$(jq -r .lineageToken "${uploaded_manifest}")"
if [ "${lineage_val}" != "lineage-abc-123" ]; then
  printf 'FAIL: Expected lineageToken lineage-abc-123, got: %s\n' "${lineage_val}" >&2
  exit 1
fi

parent_lineage_val="$(jq -r .parentLineage "${uploaded_manifest}")"
if [ "${parent_lineage_val}" != "lineage-parent-000" ]; then
  printf 'FAIL: Expected parentLineage lineage-parent-000, got: %s\n' "${parent_lineage_val}" >&2
  exit 1
fi

parent_snap_val="$(jq -r .parentSnapshot "${uploaded_manifest}")"
if [ "${parent_snap_val}" != "snap-parent-999" ]; then
  printf 'FAIL: Expected parentSnapshot snap-parent-999, got: %s\n' "${parent_snap_val}" >&2
  exit 1
fi

is_root_val="$(jq -r .isRoot "${uploaded_manifest}")"
if [ "${is_root_val}" != "false" ]; then
  printf 'FAIL: Expected isRoot false, got: %s\n' "${is_root_val}" >&2
  exit 1
fi

# Verify SHA256 digests
digest_pattern='^[0-9a-f]{64}$'
if ! jq -e --arg p "${digest_pattern}" '.sourceHostDigest | test($p)' "${uploaded_manifest}" >/dev/null; then
  printf 'FAIL: sourceHostDigest is not a valid 64-char hex SHA256\n' >&2
  exit 1
fi

if ! jq -e --arg p "${digest_pattern}" '.scopeDigest | test($p)' "${uploaded_manifest}" >/dev/null; then
  printf 'FAIL: scopeDigest is not a valid 64-char hex SHA256\n' >&2
  exit 1
fi

# Verify sizes and counts
size_val="$(jq -r .sizeBytes "${uploaded_manifest}")"
if [ "${size_val}" != "4096" ]; then
  printf 'FAIL: Expected sizeBytes 4096, got: %s\n' "${size_val}" >&2
  exit 1
fi

files_val="$(jq -r .filesCount "${uploaded_manifest}")"
if [ "${files_val}" != "12" ]; then
  printf 'FAIL: Expected filesCount 12, got: %s\n' "${files_val}" >&2
  exit 1
fi

kopia_id_val="$(jq -r .kopiaSnapshotId "${uploaded_manifest}")"
if [ "${kopia_id_val}" != "snap-mock-123" ]; then
  printf 'FAIL: Expected kopiaSnapshotId snap-mock-123, got: %s\n' "${kopia_id_val}" >&2
  exit 1
fi

# Verify S3 destination format
# nosemgrep: repository.storage.canonical-virtual-s3-uri -- test fixture asserts physical S3 URI
if ! grep -q "s3://test-snapshots-bucket/owners/123e4567-e89b-42d3-a456-426614174000/snapshots/snap-test-001.json" "${aws_log}"; then
  printf 'FAIL: S3 destination in aws log was incorrect:\n%s\n' "$(cat "${aws_log}")" >&2
  exit 1
fi

# Verify last-snapshot-selector was persisted
last_sel_file="${test_dir}/volume_1/.workspace/last-snapshot-selector"
if [ ! -f "${last_sel_file}" ] || [ "$(cat "${last_sel_file}")" != "snap-test-001" ]; then
  printf 'FAIL: Expected last-snapshot-selector to contain snap-test-001\n' >&2
  exit 1
fi

printf 'PASS: Test 1 Schema 3 manifest validation succeeded.\n'

# -----------------------------------------------------------------------------
# Test 2: Argument handling, bandwidth limits, S3 endpoints, and env fallbacks
# -----------------------------------------------------------------------------
printf 'Running Test 2: Argument handling and env variable fallback...\n'

connect_log="${test_dir}/connect_2.log"
create_log="${test_dir}/create_2.log"
aws_log="${test_dir}/aws_2.log"
uploaded_manifest="${test_dir}/uploaded_manifest_2.json"
connected_marker="${test_dir}/connected_2.marker"

MOCK_CONNECT_LOG="${connect_log}" \
  MOCK_CREATE_LOG="${create_log}" \
  MOCK_AWS_LOG="${aws_log}" \
  MOCK_UPLOADED_MANIFEST="${uploaded_manifest}" \
  MOCK_KOPIA_CONNECTED="${connected_marker}" \
  PATH="${test_dir}/bin:${PATH}" \
  KOPIA_MAX_BANDWIDTH_MBPS="10" \
  KOPIA_S3_ENDPOINT="http://127.0.0.1:9000" \
  "${subject}" "${test_dir}/volume_2" >/dev/null

# Verify bandwidth arguments were passed to kopia connect: 10 MB/s = 10485760 bytes/s
if ! grep -q -- "--max-download-speed 10485760" "${connect_log}"; then
  printf 'FAIL: Expected --max-download-speed 10485760 in connect log:\n%s\n' "$(cat "${connect_log}")" >&2
  exit 1
fi

if ! grep -q -- "--max-upload-speed 10485760" "${connect_log}"; then
  printf 'FAIL: Expected --max-upload-speed 10485760 in connect log:\n%s\n' "$(cat "${connect_log}")" >&2
  exit 1
fi

# Verify s3 endpoint and disable-tls flags
if ! grep -q -- "--endpoint 127.0.0.1:9000" "${connect_log}"; then
  printf 'FAIL: Expected --endpoint 127.0.0.1:9000 in connect log\n' >&2
  exit 1
fi

if ! grep -q -- "--disable-tls" "${connect_log}"; then
  printf 'FAIL: Expected --disable-tls in connect log\n' >&2
  exit 1
fi

# Verify fallback defaults
is_root_val="$(jq -r .isRoot "${uploaded_manifest}")"
if [ "${is_root_val}" != "true" ]; then
  printf 'FAIL: Expected isRoot true when no parent provided, got: %s\n' "${is_root_val}" >&2
  exit 1
fi

printf 'PASS: Test 2 argument and fallback handling succeeded.\n'

# -----------------------------------------------------------------------------
# Test 3: Upload fallback with curl when aws CLI is not present
# -----------------------------------------------------------------------------
printf 'Running Test 3: S3 upload fallback with curl...\n'

mkdir -p "${test_dir}/curl_only_bin"
# Copy kopia mock and jq to curl_only_bin
cp "${test_dir}/bin/kopia" "${test_dir}/curl_only_bin/kopia"
cp "${test_dir}/bin/jq" "${test_dir}/curl_only_bin/jq"

# Create mock curl in curl_only_bin
cat >"${test_dir}/curl_only_bin/curl" <<"MOCK_EOF"
#!/bin/sh
set -eu
url=""
for arg in "$@"; do
  url="$arg"
done
printf '%s\n' "$url" >>"${MOCK_CURL_URL_LOG:?}"

# Extract -T argument
upload_file=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-T" ]; then
    upload_file="$2"
    break
  fi
  shift
done

if [ -n "$upload_file" ] && [ -f "$upload_file" ]; then
  cat "$upload_file" >"${MOCK_CURL_BODY:?}"
fi
exit 0
MOCK_EOF
chmod +x "${test_dir}/curl_only_bin/curl"

curl_url_log="${test_dir}/curl_url.log"
curl_body="${test_dir}/curl_body.json"
connect_log="${test_dir}/connect_3.log"
create_log="${test_dir}/create_3.log"
connected_marker="${test_dir}/connected_3.marker"

MOCK_CONNECT_LOG="${connect_log}" \
  MOCK_CREATE_LOG="${create_log}" \
  MOCK_CURL_URL_LOG="${curl_url_log}" \
  MOCK_CURL_BODY="${curl_body}" \
  MOCK_KOPIA_CONNECTED="${connected_marker}" \
  PATH="${test_dir}/curl_only_bin:/usr/bin:/bin:/usr/sbin:/sbin" \
  CODER_WORKSPACE_OWNER_ID="user-curl-test" \
  KOPIA_REPOSITORY_BUCKET="curl-bucket" \
  SNAPSHOT_ID="snap-curl-001" \
  KOPIA_S3_ENDPOINT="http://minio.local:9000" \
  "${subject}" "${test_dir}/volume_3" >/dev/null

if [ ! -f "${curl_url_log}" ]; then
  printf 'FAIL: curl was not executed for S3 upload\n' >&2
  exit 1
fi

expected_url="http://minio.local:9000/curl-bucket/owners/user-curl-test/snapshots/snap-curl-001.json"
actual_url="$(cat "${curl_url_log}")"
if [ "${actual_url}" != "${expected_url}" ]; then
  printf 'FAIL: Expected curl upload URL %s, got: %s\n' "${expected_url}" "${actual_url}" >&2
  exit 1
fi

if [ ! -f "${curl_body}" ]; then
  printf 'FAIL: curl body was not uploaded\n' >&2
  exit 1
fi

curl_schema="$(jq -r .schema "${curl_body}")"
if [ "${curl_schema}" != "3" ]; then
  printf 'FAIL: Expected schema 3 in curl uploaded body, got: %s\n' "${curl_schema}" >&2
  exit 1
fi

printf 'PASS: Test 3 curl upload fallback succeeded.\n'

# -----------------------------------------------------------------------------
# Test 4: Sequential snapshot chaining via last-snapshot-selector state
# -----------------------------------------------------------------------------
printf 'Running Test 4: Sequential snapshot chaining via last-snapshot-selector...\n'

connect_log="${test_dir}/connect_4.log"
create_log="${test_dir}/create_4.log"
aws_log="${test_dir}/aws_4.log"
uploaded_manifest="${test_dir}/uploaded_manifest_4.json"
connected_marker="${test_dir}/connected_4.marker"

# Run snapshot again on volume_1 without WORKSPACE_PARENT_SNAPSHOT.
# It should pick up snap-test-001 from volume_1/.workspace/last-snapshot-selector.
MOCK_CONNECT_LOG="${connect_log}" \
  MOCK_CREATE_LOG="${create_log}" \
  MOCK_AWS_LOG="${aws_log}" \
  MOCK_UPLOADED_MANIFEST="${uploaded_manifest}" \
  MOCK_KOPIA_CONNECTED="${connected_marker}" \
  PATH="${test_dir}/bin:${PATH}" \
  CODER_WORKSPACE_OWNER_ID="123e4567-e89b-42d3-a456-426614174000" \
  KOPIA_REPOSITORY_BUCKET="test-snapshots-bucket" \
  SNAPSHOT_ID="snap-test-002" \
  "${subject}" "${test_dir}/volume_1" >/dev/null

parent_snap_val="$(jq -r .parentSnapshot "${uploaded_manifest}")"
if [ "${parent_snap_val}" != "snap-test-001" ]; then
  printf 'FAIL: Expected parentSnapshot snap-test-001 from state file, got: %s\n' "${parent_snap_val}" >&2
  exit 1
fi

is_root_val="$(jq -r .isRoot "${uploaded_manifest}")"
if [ "${is_root_val}" != "false" ]; then
  printf 'FAIL: Expected isRoot false for chained snapshot, got: %s\n' "${is_root_val}" >&2
  exit 1
fi

# Verify last-snapshot-selector was updated to snap-test-002
last_sel_file="${test_dir}/volume_1/.workspace/last-snapshot-selector"
if [ ! -f "${last_sel_file}" ] || [ "$(cat "${last_sel_file}")" != "snap-test-002" ]; then
  printf 'FAIL: Expected last-snapshot-selector to be updated to snap-test-002\n' >&2
  exit 1
fi

printf 'PASS: Test 4 sequential snapshot chaining succeeded.\n'
printf '%s\n' 'ALL KOPIA-SYNC TESTS PASSED.'
