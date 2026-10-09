#!/usr/bin/env bash
# Connects to the Kopia snapshot repository using the repository password file, verifies retention policies, and executes snapshot restores if requested.

set -euo pipefail

: "${WORKSPACE_CELL:?workspace cell was not injected}"
: "${WORKSPACE_CHECKOUT_PATH:?workspace checkout path was not injected}"
: "${WORKSPACE_CELL_INCARNATION:?cell incarnation was not injected}"
: "${WORKSPACE_MACHINE:?workspace machine name was not injected}"
: "${KOPIA_REPOSITORY_ACCESS_KEY_ID:?workspace backup proxy access key was not injected}"
: "${KOPIA_REPOSITORY_SECRET_ACCESS_KEY:?workspace backup proxy secret key was not injected}"
: "${WORKSPACE_USERNAME:?workspace user was not injected}"
: "${WORKSPACE_BOOT_TOKEN:?workspace boot token was not injected}"

restore_script="${1:-/etc/workspace/config/kopia-restore.sh}"
workspace_volume="${2:-/var/lib/workspace}"
runtime_dir="${XDG_RUNTIME_DIR:-/tmp}"
mkdir -p "${runtime_dir}"
chmod 0700 "${runtime_dir}"
mounts_ready_file="${runtime_dir}/workspace-mounts-ready"
snapshots_ready_file="${runtime_dir}/workspace-snapshots-ready"
restore_ready_file="${runtime_dir}/workspace-restore-ready"
repository_endpoint=http://127.0.0.1:19847

responder_pid=

stop_healthcheck_responder() {
  if [[ -n ${responder_pid:-} ]]; then
    kill "${responder_pid}" 2>/dev/null || true
    wait "${responder_pid}" 2>/dev/null || true
    responder_pid=
  fi
}

cleanup() {
  stop_healthcheck_responder
}
trap cleanup EXIT INT TERM

start_healthcheck_responder() {
  local ports=()
  read -r -a ports <<<"${WORKSPACE_APP_HEALTHCHECK_PORTS:-6768 8048 13337 13339}"
  python3 - "${ports[@]}" <<'EOF' >/dev/null 2>&1 &
import http.server
import os
import signal
import socketserver
import sys
import threading
import time

class RestoringHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"status":"restoring"}\n'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        body = b'{"status":"restoring"}\n'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()

    def log_message(self, format, *args):
        pass

class ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True

servers = []
for arg in sys.argv[1:]:
    try:
        port = int(arg)
        s = ReusableTCPServer(("127.0.0.1", port), RestoringHandler)
        servers.append(s)
    except Exception:
        pass

def sig_handler(sig, frame):
    for s in servers:
        try:
            s.server_close()
        except Exception:
            pass
    os._exit(0)

signal.signal(signal.SIGTERM, sig_handler)
signal.signal(signal.SIGINT, sig_handler)

for s in servers:
    threading.Thread(target=s.serve_forever, daemon=True).start()

while True:
    time.sleep(3600)
EOF
  responder_pid=$!
}

write_ready_marker() {
  local file=$1 temporary

  temporary="$(mktemp "${file}.XXXXXX")"
  printf '%s\n' "${WORKSPACE_BOOT_TOKEN}" >"${temporary}"
  mv -f -- "${temporary}" "${file}"
}

wait_for_mounts() {
  for ((i = 0; i < 60; i++)); do
    if [[ -f ${mounts_ready_file} ]] && [[ "$(<"${mounts_ready_file}")" == "${WORKSPACE_BOOT_TOKEN}" ]]; then
      return 0
    fi
    sleep 0.5
  done
  printf 'Error: Storage mounts verification timed out\n' >&2
  exit 1
}

wait_for_backup_proxy() {
  for _ in {1..930}; do
    if curl --connect-timeout 1 --max-time 1 --noproxy '*' \
      --output /dev/null --silent "${repository_endpoint}/"; then
      return
    fi
    sleep 1
  done
  printf '%s\n' 'workspace backup proxy did not acquire the lineage lease' >&2
  exit 1
}

prepare_backup_excludes() {
  local ignore_file="${workspace_volume}/.kopiaignore"
  local pattern

  touch "${ignore_file}"
  for pattern in \
    '.workspace/ssh' \
    'home/.cache' \
    'home/.config/kopia' \
    'home/.local/share/mise' \
    'home/.npm' \
    'home/.paseo' \
    'home/.venv' \
    'home/node_modules' \
    'home/venv' \
    'local/.venv' \
    'repo/.tmp' \
    'repo/.venv' \
    'repo/node_modules' \
    'repo/venv'; do
    if ! grep -Fqx -- "${pattern}" "${ignore_file}"; then
      printf '%s\n' "${pattern}" >>"${ignore_file}"
    fi
  done
}

snapshot="${KOPIA_RESTORE_SELECTOR:-}"
is_restore=true
if [[ -z ${snapshot} || ${snapshot} == "__start-fresh__" ]]; then
  is_restore=false
fi

if [[ ${is_restore} == true ]]; then
  start_healthcheck_responder
fi

wait_for_mounts
if [[ ${is_restore} == false ]]; then
  write_ready_marker "${restore_ready_file}"
fi

password_file="${KOPIA_PASSWORD_FILE:-/var/run/workspace/snapshot-repository/password}"

if [[ -z ${password_file} || ! -f ${password_file} ]]; then
  if [[ ${is_restore} == true ]]; then
    printf '%s\n' 'snapshot restore requested but snapshot repository password file is not configured' >&2
    exit 1
  fi
  printf 'Snapshots are off: snapshot repository password file is not configured.\n'
  exit 0
fi

# Kopia reads the repository password only from KOPIA_PASSWORD.
KOPIA_PASSWORD="$(<"${password_file}")"
export KOPIA_PASSWORD

printf 'Connecting to Kopia snapshot repository...\n'

export AWS_ACCESS_KEY_ID="${KOPIA_REPOSITORY_ACCESS_KEY_ID}"
export AWS_SECRET_ACCESS_KEY="${KOPIA_REPOSITORY_SECRET_ACCESS_KEY}"
export KOPIA_CHECK_FOR_UPDATES=false
export KOPIA_CONFIG_PATH=/tmp/workspace-kopia/repository.config
mkdir -p "$(dirname "${KOPIA_CONFIG_PATH}")"
rm -f "${KOPIA_CONFIG_PATH}"

wait_for_backup_proxy

kopia=(mise exec -- kopia)

repository_args=(
  --bucket repository
  --endpoint "${repository_endpoint#*://}"
  --override-hostname "${WORKSPACE_MACHINE}"
  --override-username "${WORKSPACE_USERNAME}"
)
if [[ ${repository_endpoint} == http://* ]]; then
  repository_args+=(--disable-tls)
fi
if ! "${kopia[@]}" repository connect s3 "${repository_args[@]}"; then
  "${kopia[@]}" repository create s3 "${repository_args[@]}" \
    || "${kopia[@]}" repository connect s3 "${repository_args[@]}"
fi
"${kopia[@]}" policy set "${workspace_volume}" --keep-latest 3 --keep-hourly 12 --keep-daily 7 --keep-weekly 4 --keep-monthly 0 --keep-annual 0

if [[ -f ${restore_script} && -x ${restore_script} ]]; then
  restore_result="$("${restore_script}" "${workspace_volume}")"
  case "${restore_result}" in
    "") ;;
    restored) ;;
    *)
      printf 'unexpected kopia restore result: %s\n' "${restore_result}" >&2
      exit 1
      ;;
  esac
fi

prepare_backup_excludes
write_ready_marker "${restore_ready_file}"
stop_healthcheck_responder
write_ready_marker "${snapshots_ready_file}"
printf 'Kopia snapshots and retention policies configured successfully.\n'
