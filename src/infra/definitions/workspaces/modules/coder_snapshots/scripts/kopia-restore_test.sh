#!/bin/sh
# shellcheck disable=SC2312
# Tests snapshot resolution, Kopia restore commands, error conditions, and atomic cutover in kopia-restore.sh.

set -eu

subject="${1:-$(cd -- "$(dirname -- "$0")" && pwd)/kopia-restore.sh}"
if [ ! -f "${subject}" ]; then
  printf 'Error: subject script %s not found\n' "${subject}" >&2
  exit 1
fi

test_dir="$(mktemp -d)"
cleanup() {
  rm -rf "${test_dir}"
}
trap cleanup EXIT

mkdir -p "${test_dir}/bin" "${test_dir}/target"

# Create mock kopia
cat >"${test_dir}/bin/kopia" <<"MOCK_EOF"
#!/bin/sh
set -eu
cmd="$1 $2"
case "$cmd" in
  "repository status")
    exit 0
    ;;
  "snapshot restore")
    if [ -z "${KOPIA_CONFIG_PATH:-}" ]; then
      printf 'Mock kopia: KOPIA_CONFIG_PATH is not set\n' >&2
      exit 1
    fi
    has_progress=0
    for arg in "$@"; do
      if [ "$arg" = "--progress" ]; then
        has_progress=1
      fi
      staging_dir="$arg"
    done
    if [ "$has_progress" -ne 1 ]; then
      printf 'Mock kopia: --progress flag was not provided\n' >&2
      exit 1
    fi
    printf 'Mock kopia: Restoring to local filesystem (%s)...\n' "$staging_dir" >&2
    printf 'Mock kopia: Processed 2 (100 B) of 2 (100 B).\n' >&2
    printf 'Mock kopia: Restored 2 files, 0 directories (100 B).\n' >&2
    if [ -n "${MOCK_RESTORE_FAIL:-}" ]; then
      printf 'Mock kopia restore simulated failure\n' >&2
      exit 1
    fi
    # If custom stage hook is provided, execute it
    if [ -n "${MOCK_RESTORE_HOOK:-}" ] && [ -x "$MOCK_RESTORE_HOOK" ]; then
      "$MOCK_RESTORE_HOOK" "$staging_dir"
    else
      mkdir -p "$staging_dir"
      printf 'restored-content-1\n' >"$staging_dir/restored_1.txt"
      printf 'restored-content-2\n' >"$staging_dir/restored_2.txt"
    fi
    exit 0
    ;;
  *)
    exit 0
    ;;
esac
MOCK_EOF
chmod +x "${test_dir}/bin/kopia"

get_inode() {
  stat -c %i "$1" 2>/dev/null || stat -f %i "$1"
}

# -----------------------------------------------------------------------------
# Test 1: Empty selector and __start-fresh__ no-op
# -----------------------------------------------------------------------------
printf 'Running Test 1: Empty selector and __start-fresh__ no-op...\n'

target_dir="${test_dir}/target_1"
mkdir -p "${target_dir}"
printf 'untouched-data\n' >"${target_dir}/original.txt"

# Empty selector
PATH="${test_dir}/bin:${PATH}" \
  KOPIA_RESTORE_SELECTOR="" \
  TARGET_DIR="${target_dir}" \
  "${subject}" >/dev/null

if [ ! -f "${target_dir}/original.txt" ]; then
  printf 'FAIL: Original file was removed on empty selector\n' >&2
  exit 1
fi

if [ -d "${target_dir}/.restore-staging" ] || [ -d "${target_dir}/.restore-backup" ]; then
  printf 'FAIL: Staging or backup directory created on empty selector\n' >&2
  exit 1
fi

# __start-fresh__ selector
PATH="${test_dir}/bin:${PATH}" \
  KOPIA_RESTORE_SELECTOR="__start-fresh__" \
  TARGET_DIR="${target_dir}" \
  "${subject}" >/dev/null

if [ ! -f "${target_dir}/original.txt" ]; then
  printf 'FAIL: Original file was removed on __start-fresh__\n' >&2
  exit 1
fi

if [ -d "${target_dir}/.restore-staging" ] || [ -d "${target_dir}/.restore-backup" ]; then
  printf 'FAIL: Staging or backup directory created on __start-fresh__\n' >&2
  exit 1
fi

printf 'PASS: Test 1 empty selector and __start-fresh__ no-op succeeded.\n'

# -----------------------------------------------------------------------------
# Test 2: Successful atomic cutover with dummy files
# -----------------------------------------------------------------------------
printf 'Running Test 2: Successful atomic cutover...\n'

target_dir="${test_dir}/target_2"
mkdir -p "${target_dir}"
printf 'old-data-file\n' >"${target_dir}/old_file.txt"
printf 'stale-data\n' >"${target_dir}/stale_file.txt"

hook_script="${test_dir}/hook_success.sh"
cat >"${hook_script}" <<"HOOK_EOF"
#!/bin/sh
set -eu
staging="$1"
mkdir -p "$staging"
printf 'new-version-1\n' >"$staging/new_file.txt"
printf 'new-version-2\n' >"$staging/another_file.txt"
HOOK_EOF
chmod +x "${hook_script}"

output="$(PATH="${test_dir}/bin:${PATH}" \
  KOPIA_RESTORE_SELECTOR="snap-success-123" \
  TARGET_DIR="${target_dir}" \
  MOCK_RESTORE_HOOK="${hook_script}" \
  "${subject}")"

if [ "${output}" != "restored" ]; then
  printf 'FAIL: expected "restored" stdout from restore script, got "%s"\n' "${output}" >&2
  exit 1
fi

# Verify new files exist in target dir
if [ ! -f "${target_dir}/new_file.txt" ]; then
  printf 'FAIL: new_file.txt does not exist in target dir after cutover\n' >&2
  exit 1
fi

if [ "$(cat "${target_dir}/new_file.txt")" != "new-version-1" ]; then
  printf 'FAIL: new_file.txt has unexpected content\n' >&2
  exit 1
fi

if [ ! -f "${target_dir}/another_file.txt" ]; then
  printf 'FAIL: another_file.txt does not exist in target dir after cutover\n' >&2
  exit 1
fi

# Verify old files were replaced
if [ -f "${target_dir}/old_file.txt" ]; then
  printf 'FAIL: old_file.txt still exists in target dir after cutover\n' >&2
  exit 1
fi

if [ -f "${target_dir}/stale_file.txt" ]; then
  printf 'FAIL: stale_file.txt still exists in target dir after cutover\n' >&2
  exit 1
fi

# Verify temporary directories cleaned up
if [ -d "${target_dir}/.restore-staging" ]; then
  printf 'FAIL: .restore-staging still exists after cutover\n' >&2
  exit 1
fi

if [ -d "${target_dir}/.restore-backup" ]; then
  printf 'FAIL: .restore-backup still exists after cutover\n' >&2
  exit 1
fi

printf 'PASS: Test 2 successful atomic cutover succeeded.\n'

# -----------------------------------------------------------------------------
# Test 3: Rollback when restore command fails midway
# -----------------------------------------------------------------------------
printf 'Running Test 3: Rollback when restore command fails midway...\n'

target_dir="${test_dir}/target_3"
mkdir -p "${target_dir}"
printf 'precious-user-work\n' >"${target_dir}/precious.txt"
printf 'dont-touch-me\n' >"${target_dir}/important.dat"

# Run with restore command failure
set +e
PATH="${test_dir}/bin:${PATH}" \
  KOPIA_RESTORE_SELECTOR="snap-fail-123" \
  TARGET_DIR="${target_dir}" \
  MOCK_RESTORE_FAIL=1 \
  "${subject}" >/dev/null 2>"${test_dir}/test3.err"
status=$?
set -e

if [ "${status}" -eq 0 ]; then
  printf 'FAIL: Expected non-zero exit code on restore failure\n' >&2
  exit 1
fi

# Verify original files remain completely intact
if [ ! -f "${target_dir}/precious.txt" ]; then
  printf 'FAIL: precious.txt was lost during failed restore\n' >&2
  exit 1
fi

if [ "$(cat "${target_dir}/precious.txt")" != "precious-user-work" ]; then
  printf 'FAIL: precious.txt was corrupted during failed restore\n' >&2
  exit 1
fi

if [ ! -f "${target_dir}/important.dat" ]; then
  printf 'FAIL: important.dat was lost during failed restore\n' >&2
  exit 1
fi

# Verify cleanup of temporary directories
if [ -d "${target_dir}/.restore-staging" ]; then
  printf 'FAIL: .restore-staging was not cleaned up after failure\n' >&2
  exit 1
fi

if [ -d "${target_dir}/.restore-backup" ]; then
  printf 'FAIL: .restore-backup was not cleaned up after failure\n' >&2
  exit 1
fi

printf 'PASS: Test 3 restore failure rollback succeeded.\n'

# -----------------------------------------------------------------------------
# Test 4: Rollback when cutover fails midway (after backup dir created)
# -----------------------------------------------------------------------------
printf 'Running Test 4: Rollback when cutover fails midway...\n'

target_dir="${test_dir}/target_4"
mkdir -p "${target_dir}"
printf 'critical-source-code\n' >"${target_dir}/main.py"
printf 'config-data\n' >"${target_dir}/config.json"

hook_script4="${test_dir}/hook4.sh"
cat >"${hook_script4}" <<"HOOK_EOF"
#!/bin/sh
set -eu
staging="$1"
mkdir -p "$staging"
printf 'unwanted-staged-content\n' >"$staging/staged_file.txt"
HOOK_EOF
chmod +x "${hook_script4}"

set +e
PATH="${test_dir}/bin:${PATH}" \
  KOPIA_RESTORE_SELECTOR="snap-cutover-fail" \
  TARGET_DIR="${target_dir}" \
  MOCK_RESTORE_HOOK="${hook_script4}" \
  SIMULATE_CUTOVER_FAILURE="true" \
  "${subject}" >/dev/null 2>"${test_dir}/test4.err"
status=$?
set -e

if [ "${status}" -eq 0 ]; then
  printf 'FAIL: Expected non-zero exit code on midway cutover failure\n' >&2
  exit 1
fi

# Verify original files were restored back to target dir
if [ ! -f "${target_dir}/main.py" ]; then
  printf 'FAIL: main.py was lost on midway cutover failure\n' >&2
  exit 1
fi

if [ "$(cat "${target_dir}/main.py")" != "critical-source-code" ]; then
  printf 'FAIL: main.py has incorrect content after rollback\n' >&2
  exit 1
fi

if [ ! -f "${target_dir}/config.json" ]; then
  printf 'FAIL: config.json was lost on midway cutover failure\n' >&2
  exit 1
fi

if [ "$(cat "${target_dir}/config.json")" != "config-data" ]; then
  printf 'FAIL: config.json has incorrect content after rollback\n' >&2
  exit 1
fi

# Verify staged files did not leak into target dir
if [ -f "${target_dir}/staged_file.txt" ]; then
  printf 'FAIL: staged_file.txt was found in target dir after rollback\n' >&2
  exit 1
fi

# Verify temporary directories cleaned up
if [ -d "${target_dir}/.restore-staging" ]; then
  printf 'FAIL: .restore-staging was not cleaned up after midway cutover failure\n' >&2
  exit 1
fi

if [ -d "${target_dir}/.restore-backup" ]; then
  printf 'FAIL: .restore-backup was not cleaned up after midway cutover failure\n' >&2
  exit 1
fi

printf 'PASS: Test 4 midway cutover rollback succeeded.\n'

# -----------------------------------------------------------------------------
# Test 5: Verify manifest matching and verification failure rollback
# -----------------------------------------------------------------------------
printf 'Running Test 5: Verify manifest matching integrity...\n'

target_dir="${test_dir}/target_5"
mkdir -p "${target_dir}"
printf 'precious-5\n' >"${target_dir}/file5.txt"

# Manifest specifying selector snap-xyz, but we request snap-different
manifest_file="${test_dir}/mismatch_manifest.json"
cat >"${manifest_file}" <<"JSON_EOF"
{
  "schema": 3,
  "selector": "snap-xyz",
  "filesCount": 5
}
JSON_EOF

set +e
PATH="${test_dir}/bin:${PATH}" \
  KOPIA_RESTORE_SELECTOR="snap-different" \
  TARGET_DIR="${target_dir}" \
  KOPIA_MANIFEST_PATH="${manifest_file}" \
  "${subject}" >/dev/null 2>"${test_dir}/test5.err"
status=$?
set -e

if [ "${status}" -eq 0 ]; then
  printf 'FAIL: Expected non-zero exit code when manifest selector mismatches\n' >&2
  exit 1
fi

if [ ! -f "${target_dir}/file5.txt" ]; then
  printf 'FAIL: file5.txt was lost when manifest mismatched\n' >&2
  exit 1
fi

if [ -d "${target_dir}/.restore-staging" ] || [ -d "${target_dir}/.restore-backup" ]; then
  printf 'FAIL: Staging or backup directory leaked after manifest mismatch\n' >&2
  exit 1
fi

printf 'PASS: Test 5 manifest matching integrity succeeded.\n'

# -----------------------------------------------------------------------------
# Test 6: Severed mounted directory inodes on successful cutover
# -----------------------------------------------------------------------------
printf 'Running Test 6: Preserve mounted directory inodes on cutover...\n'

target_dir="${test_dir}/target_6"
mkdir -p "${target_dir}/home" "${target_dir}/repo" "${target_dir}/local"
printf 'old-home-file\n' >"${target_dir}/home/old_home.txt"
printf 'old-bashrc\n' >"${target_dir}/home/.old_bashrc"
printf 'old-repo-file\n' >"${target_dir}/repo/old_repo.txt"
mkdir -p "${target_dir}/repo/old_sub"
printf 'old-sub-file\n' >"${target_dir}/repo/old_sub/file.txt"
printf 'old-local-cache\n' >"${target_dir}/local/old_cache.bin"
printf 'old-root-file\n' >"${target_dir}/root_file.txt"

home_inode_before="$(get_inode "${target_dir}/home")"
repo_inode_before="$(get_inode "${target_dir}/repo")"
local_inode_before="$(get_inode "${target_dir}/local")"

hook_script6="${test_dir}/hook6.sh"
cat >"${hook_script6}" <<"HOOK_EOF"
#!/bin/sh
set -eu
staging="$1"
mkdir -p "$staging/home" "$staging/repo/new_sub" "$staging/local" "$staging/new_dir"
printf 'new-home-content\n' >"$staging/home/new_home.txt"
printf 'new-bashrc\n' >"$staging/home/.new_bashrc"
printf 'new-repo-content\n' >"$staging/repo/new_repo.txt"
printf 'new-sub-content\n' >"$staging/repo/new_sub/nested.txt"
printf 'new-local-cache\n' >"$staging/local/new_cache.bin"
printf 'new-root-content\n' >"$staging/root_file.txt"
printf 'new-dir-file\n' >"$staging/new_dir/file.txt"
HOOK_EOF
chmod +x "${hook_script6}"

output="$(PATH="${test_dir}/bin:${PATH}" \
  KOPIA_RESTORE_SELECTOR="snap-preserve-inodes" \
  TARGET_DIR="${target_dir}" \
  MOCK_RESTORE_HOOK="${hook_script6}" \
  "${subject}" 2>"${test_dir}/test6.err")"

if [ "${output}" != "restored" ]; then
  printf 'FAIL: expected "restored" stdout from restore script, got "%s"\n' "${output}" >&2
  exit 1
fi

home_inode_after="$(get_inode "${target_dir}/home")"
repo_inode_after="$(get_inode "${target_dir}/repo")"
local_inode_after="$(get_inode "${target_dir}/local")"

if [ "${home_inode_before}" != "${home_inode_after}" ]; then
  printf 'FAIL: home directory inode changed during cutover (%s != %s)\n' "${home_inode_before}" "${home_inode_after}" >&2
  exit 1
fi

if [ "${repo_inode_before}" != "${repo_inode_after}" ]; then
  printf 'FAIL: repo directory inode changed during cutover (%s != %s)\n' "${repo_inode_before}" "${repo_inode_after}" >&2
  exit 1
fi

if [ "${local_inode_before}" != "${local_inode_after}" ]; then
  printf 'FAIL: local directory inode changed during cutover (%s != %s)\n' "${local_inode_before}" "${local_inode_after}" >&2
  exit 1
fi

# Verify new files exist inside the preserved directories
if [ ! -f "${target_dir}/home/new_home.txt" ] || [ "$(cat "${target_dir}/home/new_home.txt")" != "new-home-content" ]; then
  printf 'FAIL: new_home.txt does not exist or has incorrect content in home\n' >&2
  exit 1
fi

if [ ! -f "${target_dir}/home/.new_bashrc" ] || [ "$(cat "${target_dir}/home/.new_bashrc")" != "new-bashrc" ]; then
  printf 'FAIL: .new_bashrc does not exist or has incorrect content in home\n' >&2
  exit 1
fi

if [ ! -f "${target_dir}/repo/new_repo.txt" ] || [ ! -f "${target_dir}/repo/new_sub/nested.txt" ]; then
  printf 'FAIL: new files do not exist in repo\n' >&2
  exit 1
fi

if [ ! -f "${target_dir}/local/new_cache.bin" ]; then
  printf 'FAIL: new_cache.bin does not exist in local\n' >&2
  exit 1
fi

if [ ! -f "${target_dir}/new_dir/file.txt" ]; then
  printf 'FAIL: new_dir/file.txt does not exist\n' >&2
  exit 1
fi

if [ "$(cat "${target_dir}/root_file.txt")" != "new-root-content" ]; then
  printf 'FAIL: root_file.txt does not contain new content\n' >&2
  exit 1
fi

# Verify old files inside the preserved directories were cleaned up
if [ -f "${target_dir}/home/old_home.txt" ] || [ -f "${target_dir}/home/.old_bashrc" ]; then
  printf 'FAIL: old files still exist in home after cutover\n' >&2
  exit 1
fi

if [ -f "${target_dir}/repo/old_repo.txt" ] || [ -d "${target_dir}/repo/old_sub" ]; then
  printf 'FAIL: old files or subdirectories still exist in repo after cutover\n' >&2
  exit 1
fi

if [ -f "${target_dir}/local/old_cache.bin" ]; then
  printf 'FAIL: old cache still exists in local after cutover\n' >&2
  exit 1
fi

# Verify temporary directories cleaned up
if [ -d "${target_dir}/.restore-staging" ] || [ -d "${target_dir}/.restore-backup" ]; then
  printf 'FAIL: Staging or backup directory leaked after cutover\n' >&2
  exit 1
fi

# Verify progress was emitted to stderr
if ! grep -q 'Mock kopia: Processed' "${test_dir}/test6.err"; then
  printf 'FAIL: Kopia restore progress was not logged to stderr\n' >&2
  exit 1
fi

printf 'PASS: Test 6 preserve mounted directory inodes on cutover succeeded.\n'

# -----------------------------------------------------------------------------
# Test 7: Severed mounted directory inodes on midway cutover rollback
# -----------------------------------------------------------------------------
printf 'Running Test 7: Preserve mounted directory inodes on cutover rollback...\n'

target_dir="${test_dir}/target_7"
mkdir -p "${target_dir}/home" "${target_dir}/repo" "${target_dir}/local"
printf 'precious-home-content\n' >"${target_dir}/home/precious.txt"
printf 'precious-bashrc\n' >"${target_dir}/home/.precious_bashrc"
printf 'precious-repo-code\n' >"${target_dir}/repo/precious.py"
printf 'precious-cache\n' >"${target_dir}/local/cache.bin"

home_inode_before="$(get_inode "${target_dir}/home")"
repo_inode_before="$(get_inode "${target_dir}/repo")"
local_inode_before="$(get_inode "${target_dir}/local")"

hook_script7="${test_dir}/hook7.sh"
cat >"${hook_script7}" <<"HOOK_EOF"
#!/bin/sh
set -eu
staging="$1"
mkdir -p "$staging/home" "$staging/repo" "$staging/local"
printf 'unwanted-staged-home\n' >"$staging/home/staged_home.txt"
printf 'unwanted-staged-repo\n' >"$staging/repo/staged_repo.txt"
printf 'unwanted-staged-local\n' >"$staging/local/staged_local.txt"
HOOK_EOF
chmod +x "${hook_script7}"

set +e
PATH="${test_dir}/bin:${PATH}" \
  KOPIA_RESTORE_SELECTOR="snap-rollback-inodes" \
  TARGET_DIR="${target_dir}" \
  MOCK_RESTORE_HOOK="${hook_script7}" \
  SIMULATE_CUTOVER_FAILURE="true" \
  "${subject}" >/dev/null 2>"${test_dir}/test7.err"
status=$?
set -e

if [ "${status}" -eq 0 ]; then
  printf 'FAIL: Expected non-zero exit code on midway cutover failure\n' >&2
  exit 1
fi

home_inode_after="$(get_inode "${target_dir}/home")"
repo_inode_after="$(get_inode "${target_dir}/repo")"
local_inode_after="$(get_inode "${target_dir}/local")"

if [ "${home_inode_before}" != "${home_inode_after}" ]; then
  printf 'FAIL: home directory inode changed during rollback (%s != %s)\n' "${home_inode_before}" "${home_inode_after}" >&2
  exit 1
fi

if [ "${repo_inode_before}" != "${repo_inode_after}" ]; then
  printf 'FAIL: repo directory inode changed during rollback (%s != %s)\n' "${repo_inode_before}" "${repo_inode_after}" >&2
  exit 1
fi

if [ "${local_inode_before}" != "${local_inode_after}" ]; then
  printf 'FAIL: local directory inode changed during rollback (%s != %s)\n' "${local_inode_before}" "${local_inode_after}" >&2
  exit 1
fi

# Verify original files were restored
if [ ! -f "${target_dir}/home/precious.txt" ] || [ "$(cat "${target_dir}/home/precious.txt")" != "precious-home-content" ]; then
  printf 'FAIL: precious.txt was lost or corrupted in home after rollback\n' >&2
  exit 1
fi

if [ ! -f "${target_dir}/home/.precious_bashrc" ] || [ "$(cat "${target_dir}/home/.precious_bashrc")" != "precious-bashrc" ]; then
  printf 'FAIL: .precious_bashrc was lost or corrupted in home after rollback\n' >&2
  exit 1
fi

if [ ! -f "${target_dir}/repo/precious.py" ] || [ "$(cat "${target_dir}/repo/precious.py")" != "precious-repo-code" ]; then
  printf 'FAIL: precious.py was lost or corrupted in repo after rollback\n' >&2
  exit 1
fi

if [ ! -f "${target_dir}/local/cache.bin" ]; then
  printf 'FAIL: cache.bin was lost in local after rollback\n' >&2
  exit 1
fi

# Verify staged files did not leak
if [ -f "${target_dir}/home/staged_home.txt" ] || [ -f "${target_dir}/repo/staged_repo.txt" ] || [ -f "${target_dir}/local/staged_local.txt" ]; then
  printf 'FAIL: Staged files leaked into target directories after rollback\n' >&2
  exit 1
fi

# Verify cleanup of temporary directories
if [ -d "${target_dir}/.restore-staging" ] || [ -d "${target_dir}/.restore-backup" ]; then
  printf 'FAIL: Staging or backup directory leaked after midway cutover rollback\n' >&2
  exit 1
fi

printf 'PASS: Test 7 preserve mounted directory inodes on cutover rollback succeeded.\n'
printf '%s\n' 'ALL KOPIA-RESTORE TESTS PASSED.'
