#!/usr/bin/env bash
# Launches the background workspace-nohang daemon to freeze runaway memory processes via SIGSTOP.

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
nohang_py="${1:-}"

if [[ -z ${nohang_py} ]]; then
  if [[ -f "/etc/workspace/access/workspace_nohang.py" ]]; then
    nohang_py="/etc/workspace/access/workspace_nohang.py"
  else
    nohang_py="${script_dir}/workspace_nohang.py"
  fi
fi

if [[ ! -f ${nohang_py} ]]; then
  printf 'workspace-nohang script not found: %s\n' "${nohang_py}" >&2
  exit 1
fi

runtime_dir="${XDG_RUNTIME_DIR:-/tmp}"
pid_file="${runtime_dir}/workspace-nohang.pid"
mkdir -p "${runtime_dir}"

printf 'Starting Nohang memory guard daemon...\n'

if [[ -r ${pid_file} ]]; then
  pid="$(<"${pid_file}")"
  if [[ ${pid} =~ ^[1-9][0-9]*$ ]] && kill -0 "${pid}" 2>/dev/null; then
    command="$(ps -p "${pid}" -o args= 2>/dev/null || true)"
    if [[ ${command} == *"${nohang_py}"* ]]; then
      printf 'Nohang memory guard is already active (pid %s).\n' "${pid}"
      exit 0
    fi
  fi
fi

nohup python3 "${nohang_py}" </dev/null >>"${runtime_dir}/workspace-nohang.log" 2>&1 &
pid=$!
sleep 1
if ! kill -0 "${pid}" 2>/dev/null; then
  printf 'workspace-nohang exited during startup; inspect %s/workspace-nohang.log\n' "${runtime_dir}" >&2
  wait "${pid}"
  exit 1
fi
temporary_pid_file="$(mktemp "${pid_file}.XXXXXX")"
printf '%s\n' "${pid}" >"${temporary_pid_file}"
mv -f -- "${temporary_pid_file}" "${pid_file}"
printf 'Nohang memory guard is active (pid %s).\n' "${pid}"
