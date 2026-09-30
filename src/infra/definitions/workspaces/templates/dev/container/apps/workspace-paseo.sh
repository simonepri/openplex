#!/usr/bin/env bash
# shellcheck disable=SC2310
# Starts the Paseo service daemon on loopback and configures Tailscale Serve and Coder proxy integration.

set -euo pipefail

: "${PASEO_APP_HOSTNAME:?Paseo Coder app hostname was not injected}"
: "${WORKSPACE_CHECKOUT_PATH:?workspace checkout path was not injected}"
: "${WORKSPACE_BOOT_TOKEN:?workspace boot token was not injected}"

paseo_home="${HOME}/.paseo"
plugins_seeded_file="${paseo_home}/plugins-seeded"
runtime_dir="${XDG_RUNTIME_DIR:-/tmp}"
mounts_ready_file="${runtime_dir}/workspace-mounts-ready"
restore_ready_file="${runtime_dir}/workspace-restore-ready"
setup_ready_file="${runtime_dir}/workspace-setup-ready"
paseo_ready_file="${runtime_dir}/workspace-paseo-ready"
wait_limit=3600
tailnet_wait_limit=120
paseo_port=6767
paseo_coder_proxy_port=6768
paseo_coder_proxy_script="${1:-/etc/workspace/access/paseo_coder_proxy.py}"
paseo_coder_proxy_pid_file="${2:-/tmp/workspace-paseo-coder-proxy.pid}"
workspace_volume="${3:-/var/lib/workspace}"
tailnet_state_dir="${4:-/var/run/workspace/tailnet}"
tailnet_ready_file="${tailnet_state_dir}/workspace-tailnet-ready"
tailnet_ipv4_file="${tailnet_state_dir}/workspace-tailnet-ipv4"

if [[ ! ${PASEO_APP_HOSTNAME} =~ ^paseo--([a-z0-9-]+)--([a-z0-9-]+)\.[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]]; then
  printf '%s\n' 'PASEO_APP_HOSTNAME must be a lowercase Paseo Coder app hostname.' >&2
  exit 1
fi
workspace_name="${BASH_REMATCH[1]}"
owner_name="${BASH_REMATCH[2]}"
coder_app_host_suffix=".${PASEO_APP_HOSTNAME#*.}"
if [[ ! -r ${paseo_coder_proxy_script} ]]; then
  printf 'Paseo Coder proxy script is not readable: %s\n' "${paseo_coder_proxy_script}" >&2
  exit 1
fi

wait_for_token() {
  local file=$1 description=$2
  local attempt=0

  while [[ ! -f ${file} ]] || [[ "$(<"${file}")" != "${WORKSPACE_BOOT_TOKEN}" ]]; do
    ((attempt += 1))
    if ((attempt >= wait_limit)); then
      printf '%s did not become ready before the access deadline.\n' "${description}" >&2
      exit 1
    fi
    sleep 1
  done
}

valid_tailnet_ipv4() {
  [[ $1 =~ ^100\.([0-9]{1,3}\.){2}[0-9]{1,3}$ ]]
}

write_ready_marker() {
  local file=$1 temporary

  temporary="$(mktemp "${file}.XXXXXX")"
  printf '%s\n' "${WORKSPACE_BOOT_TOKEN}" >"${temporary}"
  mv -f -- "${temporary}" "${file}"
}

stop_coder_proxy() {
  local proxy_command proxy_pid proxy_matches=false

  if [[ -r ${paseo_coder_proxy_pid_file} ]]; then
    proxy_pid="$(sed -n '1p' "${paseo_coder_proxy_pid_file}")"
    if [[ ${proxy_pid} =~ ^[1-9][0-9]*$ ]] && [[ -r "/proc/${proxy_pid}/cmdline" ]]; then
      if tr '\0' '\n' <"/proc/${proxy_pid}/cmdline" \
        | grep -Fx -- "${paseo_coder_proxy_script}" >/dev/null; then
        proxy_matches=true
      fi
    elif [[ ${proxy_pid} =~ ^[1-9][0-9]*$ ]]; then
      proxy_command="$(ps -p "${proxy_pid}" -o command= 2>/dev/null || true)"
      if [[ ${proxy_command} == *"${paseo_coder_proxy_script}"* ]]; then
        proxy_matches=true
      fi
    fi
    if [[ ${proxy_matches} == true ]]; then
      kill "${proxy_pid}" 2>/dev/null || true
      wait "${proxy_pid}" 2>/dev/null || true
    fi
  fi
  rm -f -- "${paseo_coder_proxy_pid_file}"
}

start_coder_proxy() {
  local attempt proxy_pid temporary_pid_file

  stop_coder_proxy
  nohup python3 "${paseo_coder_proxy_script}" \
    --external-hostname "${PASEO_APP_HOSTNAME}" \
    --listen-port "${paseo_coder_proxy_port}" \
    --upstream-port "${paseo_port}" \
    </dev/null >>"${paseo_home}/coder-proxy.log" 2>&1 &
  proxy_pid=$!
  temporary_pid_file="$(mktemp "${paseo_coder_proxy_pid_file}.XXXXXX")"
  printf '%s\n' "${proxy_pid}" >"${temporary_pid_file}"
  mv -f -- "${temporary_pid_file}" "${paseo_coder_proxy_pid_file}"

  for ((attempt = 0; attempt < wait_limit; attempt++)); do
    if ! kill -0 "${proxy_pid}" 2>/dev/null; then
      printf '%s\n' 'Paseo Coder proxy exited before becoming ready.' >&2
      return 1
    fi
    if curl --connect-timeout 1 --max-time 2 --noproxy '*' \
      --fail --silent --output /dev/null \
      --header "Host: ${PASEO_APP_HOSTNAME}" \
      "http://127.0.0.1:${paseo_coder_proxy_port}/"; then
      return
    fi
    sleep 1
  done
  printf '%s\n' 'Paseo Coder proxy did not become ready before the access deadline.' >&2
  return 1
}

rm -f -- "${paseo_ready_file}"
printf 'Starting Paseo service daemon...\n'
if [[ -f ${mounts_ready_file} ]]; then
  wait_for_token "${mounts_ready_file}" 'Storage mounts'
fi
if [[ -n ${KOPIA_RESTORE_SELECTOR:-} && ${KOPIA_RESTORE_SELECTOR} != "__start-fresh__" ]]; then
  wait_for_token "${restore_ready_file}" 'Workspace restore'
fi
if [[ -f ${setup_ready_file} ]]; then
  wait_for_token "${setup_ready_file}" 'Workspace setup'
fi
# Workspace login blocks on this script, so a broken tailnet must degrade
# direct access rather than hold up the workspace: give enrollment a bounded
# head start and start the daemon without the address when it misses.
for ((attempt = 0; attempt < tailnet_wait_limit; attempt++)); do
  [[ -f ${tailnet_ready_file} ]] && break
  sleep 1
done
tailnet_ipv4="$(sed -n '1p' "${tailnet_ipv4_file}" 2>/dev/null || true)"
allowed_hostnames="localhost,127.0.0.1,${coder_app_host_suffix}"
if [[ -n ${tailnet_ipv4:-} ]] && valid_tailnet_ipv4 "${tailnet_ipv4}"; then
  allowed_hostnames+=",${tailnet_ipv4}"
else
  printf '%s\n' 'Workspace tailnet address is unavailable; Paseo direct access stays off until the next workspace start.'
fi
allowed_origins="https://${PASEO_APP_HOSTNAME},http://${PASEO_APP_HOSTNAME},https://app.paseo.sh"

ignore_file="${workspace_volume}/.kopiaignore"
touch "${ignore_file}"
if ! grep -Fqx -- 'home/.paseo' "${ignore_file}"; then
  printf '%s\n' 'home/.paseo' >>"${ignore_file}"
fi
if [[ -L ${HOME} && ! -d ${HOME} ]]; then
  target_home=""
  target_home="$(readlink -f "${HOME}" 2>/dev/null || readlink "${HOME}" || true)"
  if [[ -n ${target_home} ]]; then
    mkdir -p "${target_home}"
  fi
fi
mkdir -p "${HOME}"
mkdir -p "${paseo_home}"
config_file="${paseo_home}/config.json"
if [[ -e ${config_file} ]]; then
  normalized_config="$(mktemp "${paseo_home}/.config.json.XXXXXX")"
  if ! jq \
    --arg listen "127.0.0.1:${paseo_port}" \
    --arg allowed_hostnames "${allowed_hostnames}" \
    --arg allowed_origins "${allowed_origins}" '
      del(
        .daemon.allowedHosts,
        .daemon.auth.password,
        .daemon.serviceProxy,
        .features.webUi.distDir,
        .daemon.pluginsEnabled,
        .tools,
        .plugins["agy-provider"],
        .plugins["paseo-cafe"]
      ) |
      .pluginsEnabled = true |
      .daemon.listen = $listen |
      .daemon.hostnames = ($allowed_hostnames | split(",")) |
      .daemon.cors.allowedOrigins = ($allowed_origins | split(",")) |
      .daemon.relay.enabled = false |
      .daemon.browserTools.enabled = (.daemon.browserTools.enabled // true) |
      .features.webUi.enabled = true |
      .agents.skills.selection.mode = (.agents.skills.selection.mode // "all") |
      .agents.providers["opencode"].enabled = false |
      .agents.providers["pi"].enabled = false |
      .agents.providers["copilot"].enabled = false |
      .agents.providers["refined-antigravity-acp"] = {
        extends: "acp",
        label: "Antigravity",
        command: ["refined-antigravity-acp"],
        enabled: true
      }
    ' "${config_file}" >"${normalized_config}"; then
    rm -f -- "${normalized_config}"
    printf '%s\n' 'Paseo configuration must contain valid JSON.' >&2
    exit 1
  fi
  chmod 0600 "${normalized_config}"
  mv -f -- "${normalized_config}" "${config_file}"
else
  jq -n \
    --arg listen "127.0.0.1:${paseo_port}" \
    --arg allowed_hostnames "${allowed_hostnames}" \
    --arg allowed_origins "${allowed_origins}" \
    '{
      version: 1,
      pluginsEnabled: true,
      daemon: {
        listen: $listen,
        hostnames: ($allowed_hostnames | split(",")),
        cors: {
          allowedOrigins: ($allowed_origins | split(","))
        },
        relay: { enabled: false },
        browserTools: { enabled: true }
      },
      features: {
        webUi: { enabled: true }
      },
      agents: {
        skills: {
          selection: { mode: "all" }
        },
        providers: {
          copilot: { enabled: false },
          opencode: { enabled: false },
          pi: { enabled: false },
          "refined-antigravity-acp": {
            extends: "acp",
            label: "Antigravity",
            command: ["refined-antigravity-acp"],
            enabled: true
          }
        }
      }
    }' >"${config_file}"
  chmod 0600 "${config_file}"
fi
rm -rf -- "${paseo_home}/plugins/agy-provider" "${paseo_home}/plugins/paseo-cafe"
unset PASEO_ALLOWED_HOSTS PASEO_CORS_ALLOWED_ORIGINS
unset PASEO_HOSTNAMES PASEO_PASSWORD PASEO_WEB_UI_DIST_DIR
unset PASEO_SERVICE_PROXY_LISTEN PASEO_SERVICE_PROXY_PUBLIC_BASE_URL
export PASEO_CORS_ORIGINS="https://${PASEO_APP_HOSTNAME}"
export PASEO_SERVER_ID="${owner_name}-${workspace_name}"
export PASEO_SERVICE_PROXY_ENABLED=false
export PASEO_TRUSTED_PROXIES=loopback
export PASEO_WEB_UI_ENABLED=true
export PASEO_AGY_ACP_BIN="${PASEO_AGY_ACP_BIN:-${HOME}/.local/share/antigravity-acp/agy_acp_server.par}"
paseo=(mise exec -- paseo)

ensure_antigravity_acp() {
  local acp_dir="${HOME}/.local/share/antigravity-acp"
  local acp_bin="${acp_dir}/agy_acp_server.par"
  local bin_dir="${HOME}/.local/bin"
  local arch
  local url
  if [[ -x ${acp_bin} || -n ${TEST_EVENTS:-} ]]; then
    mkdir -p "${bin_dir}"
    ln -sf "${acp_bin}" "${bin_dir}/agy_acp_server.par" 2>/dev/null || true
    return 0
  fi
  arch="$(uname -m)"
  case "${arch}" in
    x86_64)
      url="https://dl.google.com/agy-extensions/releases/linux/agy-acp-server-agy_acp_server_1.1.1-linux-x86_64.zip"
      ;;
    aarch64 | arm64)
      url="https://dl.google.com/agy-extensions/releases/linux/agy-acp-server-agy_acp_server_1.1.1-linux-arm64.zip"
      ;;
    *)
      return 0
      ;;
  esac
  mkdir -p "${acp_dir}" "${bin_dir}"
  local tmp_zip
  tmp_zip="$(mktemp "${acp_dir}/acp.XXXXXX.zip" 2>/dev/null || mktemp /tmp/acp.XXXXXX.zip)"
  if curl --connect-timeout 2 --max-time 10 -fsSL "${url}" -o "${tmp_zip}" 2>/dev/null; then
    python3 -m zipfile -e "${tmp_zip}" "${acp_dir}" 2>/dev/null || unzip -qo "${tmp_zip}" -d "${acp_dir}" 2>/dev/null || true
    chmod +x "${acp_bin}" "${acp_dir}/localharness_external" 2>/dev/null || true
    ln -sf "${acp_bin}" "${bin_dir}/agy_acp_server.par" 2>/dev/null || true
  fi
  rm -f -- "${tmp_zip}"
}

seed_plugins() {
  local installed_plugins
  installed_plugins="$("${paseo[@]}" plugin ls --home "${paseo_home}" --json 2>/dev/null)" || installed_plugins='[]'

  local -a excluded=(
    activity agent-monitor smart-session obol github-integration
    skills fresh-worktrees plugin-updates chat-resume agy-provider
    paseo-cafe
  )
  for plugin in "${excluded[@]}"; do
    if jq -e --arg p "${plugin}" 'any(.[]; .id == $p)' <<<"${installed_plugins}" >/dev/null 2>&1; then
      "${paseo[@]}" plugin remove --home "${paseo_home}" "${plugin}" </dev/null 2>&1 || true
    fi
  done

  local -a plugins_to_ensure=(
    "agent-heartbeats:panrafal/paseo-plugins --path agent-heartbeats"
    "agents-history:panrafal/paseo-plugins --path agents-history"
    "session-usage:panrafal/paseo-plugins --path session-usage"
  )
  for entry in "${plugins_to_ensure[@]}"; do
    local pid="${entry%%:*}"
    local spec="${entry#*:}"
    if ! jq -e --arg p "${pid}" 'any(.[]; .id == $p)' <<<"${installed_plugins}" >/dev/null 2>&1; then
      # shellcheck disable=SC2086
      "${paseo[@]}" plugin add --home "${paseo_home}" ${spec} </dev/null 2>&1 || true
    fi
  done
}

ensure_antigravity_acp || true
paseo_status="$("${paseo[@]}" status --home "${paseo_home}" --json 2>/dev/null)" || paseo_status='{}'
if jq -e '.localDaemon == "running"' <<<"${paseo_status}" >/dev/null; then
  "${paseo[@]}" daemon stop --home "${paseo_home}" \
    </dev/null >>"${paseo_home}/start.log" 2>&1
fi
rm -f -- "${paseo_home}/paseo.pid"
"${paseo[@]}" daemon start \
  --home "${paseo_home}" </dev/null >"${paseo_home}/start.log" 2>&1

for ((attempt = 0; attempt < wait_limit; attempt++)); do
  paseo_status="$("${paseo[@]}" status --home "${paseo_home}" --json 2>/dev/null)" || paseo_status='{}'
  if jq -e --arg listen "127.0.0.1:${paseo_port}" '
      .localDaemon == "running" and
      .connectedDaemon == "reachable" and
      ((.listen // .configuredListen) == $listen) and
      (.relay == "disabled" or .relay.enabled == false or .relay == null)
    ' <<<"${paseo_status}" >/dev/null \
    || (curl --connect-timeout 1 --max-time 1 --noproxy '*' --fail --silent "http://127.0.0.1:${paseo_port}/status" >/dev/null 2>&1 \
      && jq -e '.localDaemon == "running"' <<<"${paseo_status}" >/dev/null 2>&1); then
    mkdir -p "${WORKSPACE_CHECKOUT_PATH}"
    "${paseo[@]}" project create "${WORKSPACE_CHECKOUT_PATH}" \
      --host "127.0.0.1:${paseo_port}" </dev/null >>"${paseo_home}/start.log" 2>&1 || true
    start_coder_proxy
    write_ready_marker "${paseo_ready_file}"
    printf 'Paseo is ready (listening on http://127.0.0.1:%s).\n' "${paseo_port}"
    if [[ ! -e ${plugins_seeded_file} ]]; then
      seed_plugins </dev/null >>"${paseo_home}/start.log" 2>&1 || true
      touch "${plugins_seeded_file}"
    fi
    exit 0
  fi
  sleep 1
done
printf '%s\n' 'Paseo did not become reachable before the access deadline.' >&2
if [[ -s "${paseo_home}/start.log" ]]; then
  printf 'Recent Paseo daemon startup log:\n' >&2
  tail -n 50 "${paseo_home}/start.log" >&2 || true
fi
exit 1
