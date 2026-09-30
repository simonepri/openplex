#!/usr/bin/env bash
# Verifies Kopia backup proxy connection, retention policy configuration, and snapshot restore behavior.

set -euo pipefail

subject="${1:-$(cd -- "$(dirname -- "$0")" && pwd)/workspace-snapshots.sh}"
test_root="$(mktemp -d)"
trap 'rm -rf -- "$test_root"' EXIT

bin="${test_root}/bin"
volume="${test_root}/volume"
runtime="${test_root}/runtime"
events="${test_root}/events"
mkdir -p "${bin}" "${volume}" "${runtime}"
: >"${events}"

cat >"${bin}/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${*: -1}" == 'http://127.0.0.1:19847/' ]]; then
  exit 0
fi
printf '%s\n' '{"repositoryPassword":"0123456789012345678901234567890123456789012"}'
EOF

cat >"${bin}/mise" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'mise %s\n' "$*" >>"$TEST_EVENTS"
case "$*" in
  'exec -- kopia repository connect s3 '*) exit 0 ;;
  'exec -- kopia policy set '*)
    [[ "$*" == *"--keep-latest 3 --keep-hourly 12 --keep-daily 7 --keep-weekly 4 --keep-monthly 0 --keep-annual 0"* ]]
    exit 0
    ;;
  *) exit 2 ;;
esac
EOF

cat >"${bin}/restore" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'restored\n'
printf 'restore\n' >>"$TEST_EVENTS"
if [[ -n ${TEST_VERIFY_RESPONDER_PORT:-} ]]; then
  body="$(python3 -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:' + '${TEST_VERIFY_RESPONDER_PORT}' + '/healthz').read().decode().strip())" 2>/dev/null || true)"
  printf 'responder:%s\n' "${body}" >>"$TEST_EVENTS"
fi
EOF

chmod +x "${bin}"/*

# Pre-populate mounts ready
printf 'boot-token\n' >"${runtime}/workspace-mounts-ready"

common_env=(
  "CODER_AGENT_TOKEN=fixture-agent-token"
  "KOPIA_REPOSITORY_ACCESS_KEY_ID=test-key"
  "KOPIA_REPOSITORY_SECRET_ACCESS_KEY=test-secret"
  "KOPIA_SNAPSHOT_BROKER_URL=https://ctrl-test-services.tailnet.k8s.example:8444"
  "PATH=${bin}:${PATH}"
  "TEST_EVENTS=${events}"
  "WORKSPACE_BOOT_TOKEN=boot-token"
  "WORKSPACE_CELL=cell-test"
  "WORKSPACE_CELL_INCARNATION=0123456789"
  "WORKSPACE_CHECKOUT_PATH=${test_root}/checkout"
  "WORKSPACE_MACHINE=test-machine"
  "WORKSPACE_USERNAME=testuser"
  "XDG_RUNTIME_DIR=${runtime}"
)

env "${common_env[@]}" bash "${subject}" "${bin}/restore" "${volume}"

[[ "$(<"${runtime}/workspace-snapshots-ready")" == "boot-token" ]]
[[ "$(<"${runtime}/workspace-restore-ready")" == "boot-token" ]]
grep -Fx restore "${events}" >/dev/null
grep -Fx "mise exec -- kopia policy set ${volume} --keep-latest 3 --keep-hourly 12 --keep-daily 7 --keep-weekly 4 --keep-monthly 0 --keep-annual 0" "${events}" >/dev/null
grep -Fx '.workspace/ssh' "${volume}/.kopiaignore" >/dev/null
grep -Fx 'home/.paseo' "${volume}/.kopiaignore" >/dev/null
grep -Fx 'local/.venv' "${volume}/.kopiaignore" >/dev/null
grep -Fx 'repo/.venv' "${volume}/.kopiaignore" >/dev/null

# Verify corp/local domain support
rm -f "${runtime}/workspace-snapshots-ready" "${runtime}/workspace-restore-ready"
env "${common_env[@]}" "KOPIA_SNAPSHOT_BROKER_URL=https://ctrl-eaws-lh1-services.tailnet.c.corp.local.internal:8444" \
  bash "${subject}" "${bin}/restore" "${volume}"
[[ "$(<"${runtime}/workspace-snapshots-ready")" == "boot-token" ]]

# Verify healthcheck responder runs during restore and stops when finished
test_port=17891
rm -f "${runtime}/workspace-snapshots-ready" "${runtime}/workspace-restore-ready"
env "${common_env[@]}" \
  "KOPIA_RESTORE_SELECTOR=snap-12345" \
  "WORKSPACE_APP_HEALTHCHECK_PORTS=${test_port}" \
  "TEST_VERIFY_RESPONDER_PORT=${test_port}" \
  bash "${subject}" "${bin}/restore" "${volume}"

[[ "$(<"${runtime}/workspace-snapshots-ready")" == "boot-token" ]]
[[ "$(<"${runtime}/workspace-restore-ready")" == "boot-token" ]]
grep -Fx 'responder:{"status":"restoring"}' "${events}" >/dev/null

# Verify port is released after restore finishes
python3 -c "import socket; s = socket.socket(); s.settimeout(0.5); res = s.connect_ex(('127.0.0.1', ${test_port})); s.close(); exit(0 if res != 0 else 1)"

printf 'All workspace-snapshots tests passed.\n'
