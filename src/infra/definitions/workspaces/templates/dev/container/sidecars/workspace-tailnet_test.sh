#!/usr/bin/env bash
# Tests readiness markers, SSH mode handling, and signal cleanup in workspace-tailnet.sh.

set -euo pipefail

subject="${1:-$(cd -- "$(dirname -- "$0")" && pwd)/workspace-tailnet.sh}"
test_dir="$(mktemp -d)"
subject_pid=
cleanup() {
  if [[ -n ${subject_pid} ]]; then
    kill "${subject_pid}" 2>/dev/null || true
    wait "${subject_pid}" 2>/dev/null || true
  fi
  rm -rf -- "${test_dir}"
}
trap cleanup EXIT
mkdir -p "${test_dir}/tmp"

# 1. SSH_ACCESS=enable: creates both ready markers and cleans up on TERM
SSH_ACCESS=enable sh "${subject}" "${test_dir}/tmp/tailscaled.sock" >"${test_dir}/stdout" 2>"${test_dir}/stderr" &
subject_pid=$!

for _ in {1..100}; do
  if [[ -f "${test_dir}/tmp/workspace-tailnet-ready" && -f "${test_dir}/tmp/workspace-ssh-ready" ]]; then
    break
  fi
  sleep 0.05
done

test -f "${test_dir}/tmp/workspace-tailnet-ready"
test -f "${test_dir}/tmp/workspace-ssh-ready"

kill -TERM "${subject_pid}"
set +e
wait "${subject_pid}"
status=$?
set -e
test "${status}" -eq 130
subject_pid=

test ! -e "${test_dir}/tmp/workspace-tailnet-ready"
test ! -e "${test_dir}/tmp/workspace-ssh-ready"

# 2. SSH_ACCESS=disable: creates only tailnet ready marker and cleans up on TERM
SSH_ACCESS=disable sh "${subject}" "${test_dir}/tmp/tailscaled.sock" >"${test_dir}/stdout" 2>"${test_dir}/stderr" &
subject_pid=$!

for _ in {1..100}; do
  if [[ -f "${test_dir}/tmp/workspace-tailnet-ready" ]]; then
    break
  fi
  sleep 0.05
done

test -f "${test_dir}/tmp/workspace-tailnet-ready"
test ! -e "${test_dir}/tmp/workspace-ssh-ready"

kill -TERM "${subject_pid}"
set +e
wait "${subject_pid}"
status=$?
set -e
test "${status}" -eq 130
subject_pid=

test ! -e "${test_dir}/tmp/workspace-tailnet-ready"

# 3. Invalid SSH_ACCESS mode: rejects with error and non-zero exit code
set +e
SSH_ACCESS=invalid sh "${subject}" "${test_dir}/tmp/tailscaled.sock" >"${test_dir}/invalid.out" 2>"${test_dir}/invalid.err"
status=$?
set -e
test "${status}" -eq 1
grep -Fq 'Unsupported SSH access mode: invalid' "${test_dir}/invalid.err"
test ! -e "${test_dir}/tmp/workspace-tailnet-ready"
test ! -e "${test_dir}/tmp/workspace-ssh-ready"
