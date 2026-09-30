#!/usr/bin/env bash
# shellcheck disable=SC2312
# Tests Paseo daemon initialization, loopback interface binding, host allowlists, and volume restore sequencing.

set -euo pipefail

subject="${1:-$(cd -- "$(dirname -- "$0")" && pwd)/workspace-paseo.sh}"
jq_bin="${2:-$(command -v jq || true)}"
test_root="$(mktemp -d)"
cleanup() {
  if [[ -r "${test_root}/proxy.pid" ]]; then
    kill "$(sed -n '1p' "${test_root}/proxy.pid")" 2>/dev/null || true
  fi
  if [[ -f "${test_root}/child-pids" ]]; then
    while read -r child_pid; do
      kill "${child_pid}" 2>/dev/null || true
    done <"${test_root}/child-pids"
  fi
  rm -rf -- "${test_root}"
}
trap cleanup EXIT
mkdir -p "${test_root}/bin" "${test_root}/home" "${test_root}/runtime" "${test_root}/tailnet" "${test_root}/volume"
if [[ -n ${jq_bin} && -x ${jq_bin} ]]; then
  cp "${jq_bin}" "${test_root}/bin/jq"
elif host_jq=$(command -v jq 2>/dev/null); then
  cp "${host_jq}" "${test_root}/bin/jq"
else
  printf 'Error: jq executable was not found\n' >&2
  exit 1
fi
export PATH="${test_root}/bin:${PATH}"
events="${test_root}/events"

cat >"${test_root}/bin/mise" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'mise %s\n' "$*" >>"$TEST_EVENTS"
case "$*" in
  'exec -- paseo status '*)
    if [[ -n "${TEST_PASEO_STATUS:-}" && ! -f "$TEST_STATUS_USED" ]]; then
      touch "$TEST_STATUS_USED"
      printf '%s\n' "$TEST_PASEO_STATUS"
    elif [[ -f "$TEST_STARTED" ]]; then
      printf '{"localDaemon":"running","connectedDaemon":"reachable","listen":"127.0.0.1:%s","relay":"disabled"}\n' \
        6767
    else
      printf '%s\n' '{"localDaemon":"stopped","connectedDaemon":"unreachable"}'
    fi
    ;;
  'exec -- paseo daemon stop '*)
    printf '%s\n' stopped
    ;;
  'exec -- paseo plugin '*)
    printf '%s\n' '[]'
    ;;
  'exec -- paseo project create '*)
    [[ "$*" == *"$TEST_WORKSPACE_VOLUME/repo --host 127.0.0.1:6767"* ]]
    if IFS= read -r _; then
      printf '%s\n' 'Paseo project create inherited readable stdin' >&2
      exit 3
    fi
    printf '%s\n' created
    ;;
  'exec -- paseo daemon start '*)
    grep -Fx 'home/.paseo' "$TEST_WORKSPACE_VOLUME/.kopiaignore" >/dev/null
    [[ "$PASEO_SERVER_ID" == examples-dev ]]
    [[ "$PASEO_SERVICE_PROXY_ENABLED" == false ]]
    [[ -z "${PASEO_ALLOWED_HOSTS+x}" ]]
    [[ -z "${PASEO_CORS_ALLOWED_ORIGINS+x}" ]]
    [[ "$PASEO_CORS_ORIGINS" == https://paseo--dev--examples.coder.ctrl-eaws-lh1.k8s.unit.test ]]
    [[ "$PASEO_CORS_ORIGINS" != *evil.example* ]]
    [[ -z "${PASEO_HOSTNAMES+x}" ]]
    [[ -z "${PASEO_PASSWORD+x}" ]]
    [[ -z "${PASEO_WEB_UI_DIST_DIR+x}" ]]
    [[ "$PASEO_WEB_UI_ENABLED" == true ]]
    jq -e '
      .log.level == "debug" and
      .daemon.appendSystemPrompt == "custom prompt" and
      .pluginsEnabled == true and
      .daemon.browserTools.enabled == true and
      .agents.skills.selection.mode == "all" and
      .daemon.listen == "127.0.0.1:6767" and
      .daemon.relay.enabled == false and
      .features.webUi.enabled == true and
      (.daemon.hostnames | index("localhost")) != null and
      (.daemon.hostnames | index("127.0.0.1")) != null and
      (.daemon.hostnames | index(".coder.ctrl-eaws-lh1.k8s.unit.test")) != null and
      (.daemon | has("allowedHosts") | not) and
      (.daemon.cors.allowedOrigins | index("https://paseo--dev--examples.coder.ctrl-eaws-lh1.k8s.unit.test")) != null and
      (.daemon.cors.allowedOrigins | index("https://app.paseo.sh")) != null and
      (.daemon.cors.allowedOrigins | any(. == "https://evil.example") | not) and
      (.daemon.auth | has("password") | not) and
      (.daemon | has("serviceProxy") | not) and
      (.features.webUi | has("distDir") | not) and
      (has("tools") | not) and
      (.daemon | has("pluginsEnabled") | not) and
      .agents.providers["refined-antigravity-acp"].extends == "acp" and
      .agents.providers["refined-antigravity-acp"].command == ["refined-antigravity-acp"] and
      .agents.providers["refined-antigravity-acp"].label == "Antigravity" and
      .agents.providers["refined-antigravity-acp"].enabled == true
    ' "$HOME/.paseo/config.json" >/dev/null
    if IFS= read -r _; then
      printf '%s\n' 'Paseo inherited readable stdin' >&2
      exit 3
    fi
    printf '%s\n' started
    printf '%s\n' detached-stderr >&2
    /bin/sleep 30 &
    printf '%s\n' "$!" >>"$TEST_CHILD_PIDS"
    touch "$TEST_STARTED"
    if jq -e '.daemon.hostnames | any(. == "100.64.12.34")' "$HOME/.paseo/config.json" >/dev/null; then
      touch "$TEST_RECONCILED"
    fi
    ;;
  *) exit 2 ;;
esac
EOF
cat >"${test_root}/bin/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'curl %s\n' "$*" >>"$TEST_EVENTS"
[[ "$*" == *'--header Host: paseo--dev--examples.coder.ctrl-eaws-lh1.k8s.unit.test'* ]]
[[ "$*" == *'http://127.0.0.1:6768/'* ]]
EOF
cat >"${test_root}/bin/sleep" <<'EOF'
#!/bin/sh
printf '%s\n' wait >>"$TEST_EVENTS"
printf '%s\n' "$WORKSPACE_BOOT_TOKEN" \
  >"${TEST_READY_TARGET:-$XDG_RUNTIME_DIR/workspace-restore-ready}"
EOF
chmod +x "${test_root}/bin"/*

common_env=(
  "HOME=${test_root}/home"
  "WORKSPACE_CHECKOUT_PATH=${test_root}/volume/repo"
  "PASEO_APP_HOSTNAME=paseo--dev--examples.coder.ctrl-eaws-lh1.k8s.unit.test"
  "TEST_EVENTS=${events}"
  "TEST_CHILD_PIDS=${test_root}/child-pids"
  "TEST_RECONCILED=${test_root}/reconciled"
  "TEST_WORKSPACE_VOLUME=${test_root}/volume"
  "WORKSPACE_BOOT_TOKEN=boot-current"
  "TEST_STARTED=${test_root}/paseo-started"
  "TEST_STATUS_USED=${test_root}/status-used"
  "PASEO_ALLOWED_HOSTS=evil.example"
  "PASEO_CORS_ALLOWED_ORIGINS=https://evil.example"
  "PASEO_CORS_ORIGINS=https://evil.example"
  "PASEO_HOSTNAMES=evil.example"
  "PASEO_PASSWORD=stale-environment-password"
  "PASEO_SERVER_ID=stale-server-id"
  "PASEO_WEB_UI_DIST_DIR=/tmp/evil-ui"
  "XDG_RUNTIME_DIR=${test_root}/runtime"
  "PATH=${test_root}/bin:${PATH}"
)
cat >"${test_root}/proxy.py" <<'PY'
import sys
import time

assert sys.argv[1:] == [
    "--external-hostname",
    "paseo--dev--examples.coder.ctrl-eaws-lh1.k8s.unit.test",
    "--listen-port",
    "6768",
    "--upstream-port",
    "6767",
]
time.sleep(30)
PY

printf '%s\n' 100.64.12.34 >"${test_root}/tailnet/workspace-tailnet-ipv4"
touch "${test_root}/tailnet/workspace-tailnet-ready"
mkdir -p "${test_root}/home/.paseo"
cat >"${test_root}/home/.paseo/config.json" <<'EOF'
{
  "log": {"level": "debug"},
  "daemon": {
    "appendSystemPrompt": "custom prompt",
    "hostnames": true,
    "allowedHosts": ["evil.example"],
    "cors": {"allowedOrigins": ["https://evil.example"]},
    "auth": {"password": "stale-password"},
    "serviceProxy": {"enabled": true, "listen": "0.0.0.0:9999"}
  },
  "features": {"webUi": {"distDir": "/tmp/evil-ui"}}
}
EOF

env "${common_env[@]}" python3 - \
  "${subject}" "${test_root}/proxy.py" "${test_root}/proxy.pid" \
  "${test_root}/volume" "${test_root}/tailnet" "${test_root}/outer-output" <<'PY'
import pathlib
import subprocess
import sys

with pathlib.Path(sys.argv[6]).open("wb") as output:
    subprocess.run(
        ["bash", "-c", 'bash "$1" "$2" "$3" "$4" "$5" 2>&1 | cat', "_", *sys.argv[1:6]],
        stdout=output,
        timeout=15,
    )
PY
if grep -F 'detached-stderr' "${test_root}/outer-output" >/dev/null; then
  printf '%s\n' 'detached stderr was leaked to caller' >&2
  exit 1
fi
[[ "$(<"${test_root}/runtime/workspace-paseo-ready")" == boot-current ]]
if grep -Fx wait "${events}" >/dev/null; then
  printf '%s\n' 'fresh startup waited for a restore marker' >&2
  exit 1
fi
[[ "$(grep -c 'paseo daemon start --home' "${events}")" -eq 1 ]]
first_start="$(grep -m1 'paseo daemon start --home' "${events}")"
[[ ${first_start} == "mise exec -- paseo daemon start --home ${test_root}/home/.paseo" ]]
if grep -F 'paseo daemon stop' "${events}" >/dev/null; then
  printf '%s\n' 'fresh startup restarted a daemon that was never running' >&2
  exit 1
fi
[[ -e "${test_root}/reconciled" ]]
jq -e '
  .daemon.hostnames | any(. == "100.64.12.34")
' "${test_root}/home/.paseo/config.json" >/dev/null
grep -F 'paseo project create' "${events}" | grep -F -- "${test_root}/volume/repo --host 127.0.0.1:6767" >/dev/null
grep -F 'curl ' "${events}" | grep -F -- '--header Host: paseo--dev--examples.coder.ctrl-eaws-lh1.k8s.unit.test' >/dev/null
if grep -E 'tailscale|daemon pair|pairing' "${events}" >/dev/null; then
  printf '%s\n' 'Paseo startup called a sidecar-only or pairing interface' >&2
  exit 1
fi
if grep -F 'github-integration' "${events}" >/dev/null; then
  printf '%s\n' 'github-integration plugin was unexpectedly installed' >&2
  exit 1
fi
grep -F 'paseo plugin add' "${events}" >/dev/null
[[ -e "${test_root}/home/.paseo/plugins-seeded" ]]
grep -Fx started "${test_root}/home/.paseo/start.log" >/dev/null
grep -Fx detached-stderr "${test_root}/home/.paseo/start.log" >/dev/null

: >"${events}"
rm -f "${test_root}/reconciled"
rm -f "${test_root}/tailnet/workspace-tailnet-ready"
env "${common_env[@]}" \
  "TEST_READY_TARGET=${test_root}/tailnet/workspace-tailnet-ready" \
  bash "${subject}" "${test_root}/proxy.py" "${test_root}/proxy.pid" "${test_root}/volume" "${test_root}/tailnet"
[[ "$(<"${test_root}/runtime/workspace-paseo-ready")" == boot-current ]]
wait_line="$(grep -n -m1 -Fx wait "${events}" | cut -d: -f1)"
start_line="$(grep -n -m1 'paseo daemon start' "${events}" | cut -d: -f1)"
[[ -n ${wait_line} ]]
((wait_line < start_line))
[[ -e "${test_root}/reconciled" ]]
if grep -F 'paseo plugin ' "${events}" >/dev/null; then
  printf '%s\n' 'plugin seeding ran again after the first boot' >&2
  exit 1
fi

: >"${events}"
rm -f "${test_root}/reconciled"
rm -f "${test_root}/status-used"
env "${common_env[@]}" \
  'TEST_PASEO_STATUS={"localDaemon":"running","connectedDaemon":"reachable","listen":"0.0.0.0:6767","relay":"wss://relay.paseo.sh:443"}' \
  bash "${subject}" "${test_root}/proxy.py" "${test_root}/proxy.pid" "${test_root}/volume" "${test_root}/tailnet"
[[ -e "${test_root}/reconciled" ]]
stop_line="$(grep -n -m1 'paseo daemon stop' "${events}" | cut -d: -f1)"
start_line="$(grep -n -m1 'paseo daemon start' "${events}" | cut -d: -f1)"
((stop_line < start_line))

: >"${events}"
rm -f "${test_root}/reconciled"
rm -f "${test_root}/status-used"
env "${common_env[@]}" \
  'TEST_PASEO_STATUS={"localDaemon":"running","connectedDaemon":"reachable","listen":"127.0.0.1:6767","relay":"disabled"}' \
  bash "${subject}" "${test_root}/proxy.py" "${test_root}/proxy.pid" "${test_root}/volume" "${test_root}/tailnet"
[[ -e "${test_root}/reconciled" ]]
stop_line="$(grep -n -m1 'paseo daemon stop' "${events}" | cut -d: -f1)"
start_line="$(grep -n -m1 'paseo daemon start' "${events}" | cut -d: -f1)"
((stop_line < start_line))

: >"${events}"
rm -f "${test_root}/reconciled"
printf '%s\n' boot-stale >"${test_root}/runtime/workspace-restore-ready"
env "${common_env[@]}" KOPIA_RESTORE_SELECTOR=manifest-1 \
  bash "${subject}" "${test_root}/proxy.py" "${test_root}/proxy.pid" "${test_root}/volume" "${test_root}/tailnet"
[[ -e "${test_root}/reconciled" ]]
[[ "$(sed -n '1p' "${events}")" == wait ]]
[[ "$(<"${test_root}/runtime/workspace-restore-ready")" == boot-current ]]
grep -F 'paseo daemon start --home' "${events}" >/dev/null

: >"${events}"
rm -f "${test_root}/reconciled" "${test_root}/runtime/workspace-paseo-ready"
rm -f "${test_root}/tailnet/workspace-tailnet-ready" "${test_root}/tailnet/workspace-tailnet-ipv4"
env "${common_env[@]}" \
  "TEST_READY_TARGET=${test_root}/unused-ready" \
  bash "${subject}" "${test_root}/proxy.py" "${test_root}/proxy.pid" "${test_root}/volume" "${test_root}/tailnet"
[[ "$(<"${test_root}/runtime/workspace-paseo-ready")" == boot-current ]]
[[ ! -e "${test_root}/reconciled" ]]
jq -e '
  .daemon.hostnames | any(. == "100.64.12.34") | not
' "${test_root}/home/.paseo/config.json" >/dev/null
