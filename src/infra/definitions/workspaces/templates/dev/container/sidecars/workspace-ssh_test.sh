#!/usr/bin/env bash
# shellcheck disable=SC2312
# Tests loopback OpenSSH daemon startup flags, key permissions, and restore synchronization in workspace-ssh.sh.

set -euo pipefail

source_subject="${1:-$(cd -- "$(dirname -- "$0")" && pwd)/workspace-ssh.sh}"
test_root="$(mktemp -d)"
trap 'rm -rf -- "${test_root}"' EXIT
mkdir -p "${test_root}/access" "${test_root}/bin" "${test_root}/volume"
events="${test_root}/events"
subject="${test_root}/access/workspace-ssh.sh"
cp "${source_subject}" "${subject}"

cat >"${test_root}/access/workspace-identity.sh" <<'EOF'
#!/bin/sh
set -eu
grep -Fx '.workspace/ssh' "$TEST_WORKSPACE_VOLUME/.kopiaignore" >/dev/null
printf '%s\n' identity >>"$TEST_EVENTS"
mkdir -p "$2"
printf '%s\n' passwd >"$2/passwd"
printf '%s\n' group >"$2/group"
EOF
cat >"${test_root}/bin/ssh-keygen" <<'EOF'
#!/bin/sh
set -eu
while [ "$1" != -f ]; do shift; done
touch "$2" "$2.pub"
printf '%s\n' keygen >>"$TEST_EVENTS"
EOF
cat >"${test_root}/bin/sshd" <<'EOF'
#!/bin/sh
printf 'sshd %s\n' "$*" >>"$TEST_EVENTS"
EOF
mkdir -p "${test_root}/fixture"
touch "${test_root}/fixture/libnss_wrapper.so"
cat >"${test_root}/bin/find" <<EOF
#!/bin/sh
printf '%s\n' "${test_root}/fixture/libnss_wrapper.so"
EOF
cat >"${test_root}/bin/sleep" <<'EOF'
#!/bin/sh
printf '%s\n' wait >>"$TEST_EVENTS"
printf '%s\n' "$WORKSPACE_BOOT_TOKEN" >"$TEST_RESTORE_READY_FILE"
EOF
chmod +x "${test_root}/access/workspace-identity.sh" "${test_root}/bin"/*

common_env=(
  "WORKSPACE_USERNAME=alice"
  "TEST_EVENTS=${events}"
  "TEST_RESTORE_READY_FILE=${test_root}/restore-ready"
  "CODER_WORKSPACE_NAME=dev"
  "WORKSPACE_BOOT_TOKEN=boot-current"
  "TEST_WORKSPACE_VOLUME=${test_root}/volume"
  "PATH=${test_root}/bin:${PATH}"
)
subject_command=(sh "${subject}" "${test_root}/runtime" "${test_root}/restore-ready" "${test_root}/volume")

env "${common_env[@]}" "${subject_command[@]}"
[[ ! -e ${events} ]]

if env "${common_env[@]}" SSH_ACCESS=enable "${subject_command[@]}" \
  >"${test_root}/missing-key.out" 2>&1; then
  printf '%s\n' 'SSH enable accepted an empty public key' >&2
  exit 1
fi
grep -F 'SSH_PUBLIC_KEY must be nonempty' "${test_root}/missing-key.out" >/dev/null

: >"${events}"
env "${common_env[@]}" \
  SSH_ACCESS=enable \
  'SSH_PUBLIC_KEY=ssh-ed25519 AAAAfixture alice' \
  "${subject_command[@]}"
if grep -Fx wait "${events}" >/dev/null; then
  printf '%s\n' 'fresh startup waited for a restore marker' >&2
  exit 1
fi
grep -Fx identity "${events}" >/dev/null
grep -F 'ListenAddress=0.0.0.0' "${events}" >/dev/null
if grep -F 'ForceCommand=' "${events}" >/dev/null; then
  printf '%s\n' 'SSH replaced the user command instead of using the native login shell.' >&2
  exit 1
fi
host_key="${test_root}/volume/.workspace/ssh/ssh_host_ed25519_key"
authorized_keys="${test_root}/volume/.workspace/ssh/authorized_keys"
[[ "$(stat -c '%a' "${test_root}/volume/.workspace/ssh" 2>/dev/null || stat -f '%Lp' "${test_root}/volume/.workspace/ssh")" == 700 ]]
[[ "$(stat -c '%a' "${host_key}" 2>/dev/null || stat -f '%Lp' "${host_key}")" == 600 ]]
[[ "$(stat -c '%a' "${authorized_keys}" 2>/dev/null || stat -f '%Lp' "${authorized_keys}")" == 600 ]]

: >"${events}"
env "${common_env[@]}" \
  SSH_ACCESS=enable \
  'SSH_PUBLIC_KEY=ssh-ed25519 AAAAreplacement alice' \
  "${subject_command[@]}"
if grep -Fx keygen "${events}" >/dev/null; then
  printf '%s\n' 'existing SSH host key was replaced' >&2
  exit 1
fi
grep -Fx 'ssh-ed25519 AAAAreplacement alice' "${authorized_keys}" >/dev/null

: >"${events}"
env "${common_env[@]}" SSH_ACCESS=disable "${subject_command[@]}"
[[ ! -e ${authorized_keys} ]]
[[ -e ${host_key} ]]
[[ ! -s ${events} ]]

if env "${common_env[@]}" SSH_ACCESS=keep "${subject_command[@]}" \
  >"${test_root}/invalid-mode.out" 2>&1; then
  printf '%s\n' 'unsupported SSH mode was accepted' >&2
  exit 1
fi
grep -F 'Unsupported SSH access mode: keep' "${test_root}/invalid-mode.out" >/dev/null

: >"${events}"
printf '%s\n' boot-stale >"${test_root}/restore-ready"
env "${common_env[@]}" \
  KOPIA_RESTORE_SELECTOR=manifest-1 \
  SSH_ACCESS=enable \
  'SSH_PUBLIC_KEY=ssh-ed25519 AAAAfixture alice' \
  "${subject_command[@]}"
[[ "$(sed -n '1p' "${events}")" == wait ]]
[[ "$(sed -n '2p' "${events}")" == identity ]]
[[ "$(<"${test_root}/restore-ready")" == boot-current ]]

: >"${events}"
printf '%s\n' boot-stale >"${test_root}/restore-ready"
env "${common_env[@]}" \
  KOPIA_RESTORE_SELECTOR=manifest-2 \
  SSH_ACCESS=disable \
  "${subject_command[@]}"
[[ "$(sed -n '1p' "${events}")" == wait ]]
[[ ! -e ${authorized_keys} ]]
