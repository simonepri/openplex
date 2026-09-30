#!/usr/bin/env bash
# shellcheck disable=SC2310
# Installs and launches the pinned code-server web IDE after workspace volume restore has completed.

set -euo pipefail

: "${WORKSPACE_CHECKOUT_PATH:?workspace checkout path was not injected}"
: "${WORKSPACE_BOOT_TOKEN:?workspace boot token was not injected}"
: "${HOME:?workspace home was not injected}"

code_server_port=13337
code_server_version=4.139.1
case "$(uname -m)" in
  x86_64)
    workspace_arch=amd64
    code_server_sha256=53029be6c5781b7bca49b815fcc9a2a3fc111813ad8c9965b2c0f0d2985a0674
    ;;
  aarch64 | arm64)
    workspace_arch=arm64
    code_server_sha256=0edb4b60d9c4744b2dd14b0911e3c2e6dd8c6f3c13bd58bda23ae744e59e7df1
    ;;
  *)
    printf '%s\n' 'code-server supports only amd64 and arm64 workspaces.' >&2
    exit 1
    ;;
esac
default_settings='{"extensions.autoCheckUpdates":false,"extensions.autoUpdate":false,"files.watcherExclude":{"**/s3/**":true},"security.workspace.trust.emptyWindow":true,"security.workspace.trust.enabled":false,"security.workspace.trust.startupPrompt":"never","workbench.activityBar.location":"top","workbench.colorTheme":"Default Dark Modern","workbench.startupEditor":"none"}'
default_extensions='[{"id":"BazelBuild.vscode-bazel","sha256":"03877ad9de60d080ec30f7880d90387bb2e26c88a7d194374173d15f49a7741d","url":"https://open-vsx.org/api/BazelBuild/vscode-bazel/0.15.0/file/BazelBuild.vscode-bazel-0.15.0.vsix","version":"0.15.0"}]'

wait_for_boot_marker() {
  local file=$1

  until [[ -f ${file} ]] \
    && [[ "$(<"${file}")" == "${WORKSPACE_BOOT_TOKEN}" ]]; do
    sleep 1
  done
}

runtime_dir="${XDG_RUNTIME_DIR:-/tmp}"
mounts_ready_file="${runtime_dir}/workspace-mounts-ready"
restore_ready_file="${runtime_dir}/workspace-restore-ready"
setup_ready_file="${runtime_dir}/workspace-setup-ready"
ready_file="${runtime_dir}/workspace-code-server-ready"
if [[ -f ${mounts_ready_file} ]]; then
  wait_for_boot_marker "${mounts_ready_file}"
fi
if [[ -n ${KOPIA_RESTORE_SELECTOR:-} && ${KOPIA_RESTORE_SELECTOR} != "__start-fresh__" ]]; then
  wait_for_boot_marker "${restore_ready_file}"
fi
if [[ -f ${setup_ready_file} ]]; then
  wait_for_boot_marker "${setup_ready_file}"
fi
rm -f -- "${ready_file}"

write_ready_marker() {
  local temporary

  temporary="$(mktemp "${ready_file}.XXXXXX")"
  printf '%s\n' "${WORKSPACE_BOOT_TOKEN}" >"${temporary}"
  mv -f -- "${temporary}" "${ready_file}"
}

data_dir="${XDG_DATA_HOME:-${HOME}/.local/share}"
user_data_dir="${data_dir}/code-server/user-data"
extensions_dir="${data_dir}/code-server/extensions"
artifact="code-server-${code_server_version}-linux-${workspace_arch}"
install_prefix="${runtime_dir}/${artifact}"
server_binary="${install_prefix}/bin/code-server"
state_file="${runtime_dir}/code-server-launch.state"
log_file="${runtime_dir}/code-server.log"
session_socket="${runtime_dir}/code-server-ipc.sock"
if [[ -L ${HOME} && ! -d ${HOME} ]]; then
  target_home=""
  target_home="$(readlink -f "${HOME}" 2>/dev/null || readlink "${HOME}" || true)"
  if [[ -n ${target_home} ]]; then
    mkdir -p "${target_home}"
  fi
fi
mkdir -p "${HOME}"
mkdir -p "${runtime_dir}" "${user_data_dir}/User" "${extensions_dir}"
wait_limit=120

printf 'Starting VS Code (code-server v%s)...\n' "${code_server_version}"

server_is_ready() {
  curl --connect-timeout 1 --max-time 1 --noproxy '*' --fail --silent \
    "http://127.0.0.1:${code_server_port}/healthz" >/dev/null
}

if command -v code-server >/dev/null 2>&1; then
  server_binary="$(command -v code-server)"
elif [[ -x "${HOME}/.local/share/mise/installs/github-coder-code-server/${code_server_version}/bin/code-server" ]]; then
  server_binary="${HOME}/.local/share/mise/installs/github-coder-code-server/${code_server_version}/bin/code-server"
fi

old_token=
old_pid=
if [[ -f ${state_file} ]]; then
  read -r old_token old_pid <"${state_file}" || true
fi
if [[ ${old_token} == "${WORKSPACE_BOOT_TOKEN}" ]] \
  && [[ ${old_pid} =~ ^[1-9][0-9]*$ ]] && kill -0 "${old_pid}" 2>/dev/null; then
  if server_is_ready; then
    write_ready_marker
    printf 'VS Code is already running (listening on http://127.0.0.1:%s).\n' "${code_server_port}"
    exit 0
  fi
  if [[ -r "/proc/${old_pid}/cmdline" ]] \
    && grep -Fq "${server_binary}" "/proc/${old_pid}/cmdline"; then
    kill "${old_pid}"
    wait "${old_pid}" 2>/dev/null || true
  else
    printf '%s\n' 'Refusing to stop a process not owned by this code-server launcher.' >&2
    exit 1
  fi
fi

if [[ ! -x ${server_binary} ]]; then
  archive="$(mktemp "${runtime_dir}/${artifact}.tar.gz.XXXXXX")"
  extraction="$(mktemp -d "${runtime_dir}/.code-server-install.XXXXXX")"
  cleanup_install() {
    rm -f -- "${archive}"
    rm -rf -- "${extraction}"
  }
  trap cleanup_install EXIT
  curl --silent --show-error --fail --location --retry 3 --retry-all-errors \
    --output "${archive}" \
    "https://github.com/coder/code-server/releases/download/v${code_server_version}/${artifact}.tar.gz"
  archive_sha256="$(sha256sum "${archive}" | awk '{print $1}')"
  if [[ ${archive_sha256} != "${code_server_sha256}" ]]; then
    printf '%s\n' 'Downloaded code-server archive did not match its pinned SHA-256.' >&2
    exit 1
  fi
  tar -xzf "${archive}" -C "${extraction}"
  if [[ ! -x "${extraction}/${artifact}/bin/code-server" ]]; then
    printf '%s\n' 'Downloaded code-server archive did not contain the expected binary.' >&2
    exit 1
  fi
  rm -rf -- "${install_prefix}"
  mv -- "${extraction}/${artifact}" "${install_prefix}"
  cleanup_install
  trap - EXIT
fi

installed_version="$(
  "${server_binary}" --version 2>&1 \
    | awk '$1 ~ /^[0-9]+\.[0-9]+\.[0-9]+$/ { print $1 }'
)"
if [[ ${installed_version} != "${code_server_version}" ]]; then
  printf 'Installed code-server version must be %s, found %s.\n' \
    "${code_server_version}" "${installed_version}" >&2
  exit 1
fi

settings_file="${user_data_dir}/User/settings.json"
temporary_settings="$(mktemp "${user_data_dir}/User/.settings.json.XXXXXX")"
if [[ ! -e ${settings_file} ]]; then
  printf '%s\n' "${default_settings}" | jq -S . >"${temporary_settings}"
else
  jq -S '
    .["security.workspace.trust.emptyWindow"] = true |
    .["security.workspace.trust.enabled"] = false |
    .["security.workspace.trust.startupPrompt"] = "never"
  ' "${settings_file}" >"${temporary_settings}"
fi
mv -f -- "${temporary_settings}" "${settings_file}"

product_file="${install_prefix}/lib/vscode/product.json"
if [[ -f ${product_file} ]]; then
  temporary_product="$(mktemp "${runtime_dir}/.product.json.XXXXXX")"
  jq '
    .linkProtectionTrustedDomains = (
      ((.linkProtectionTrustedDomains // []) + [
        "https://open-vsx.org",
        "https://github.com",
        "https://*.github.com",
        "https://*.githubusercontent.com"
      ]) | unique
    )
  ' "${product_file}" >"${temporary_product}"
  mv -f -- "${temporary_product}" "${product_file}"
fi

while IFS=$'\t' read -r extension_id extension_version extension_url extension_sha256; do
  expected_extension="$(printf '%s@%s' "${extension_id}" "${extension_version}" | tr '[:upper:]' '[:lower:]')"
  if "${server_binary}" \
    --user-data-dir "${user_data_dir}" \
    --extensions-dir "${extensions_dir}" \
    --list-extensions \
    --show-versions 2>/dev/null \
    | tr '[:upper:]' '[:lower:]' \
    | grep -Fqx -- "${expected_extension}"; then
    continue
  fi
  extension_download="$(mktemp -d "${runtime_dir}/.code-server-extension.XXXXXX")"
  if ! curl --silent --show-error --fail --location --retry 3 --retry-all-errors \
    --output "${extension_download}/extension.vsix" \
    "${extension_url}"; then
    rm -rf -- "${extension_download}"
    exit 1
  fi
  actual_extension_sha256="$(sha256sum "${extension_download}/extension.vsix" | awk '{print $1}')"
  if [[ ${actual_extension_sha256} != "${extension_sha256}" ]]; then
    rm -rf -- "${extension_download}"
    printf 'Default extension %s did not match its pinned SHA-256.\n' "${extension_id}" >&2
    exit 1
  fi
  if ! "${server_binary}" \
    --user-data-dir "${user_data_dir}" \
    --extensions-dir "${extensions_dir}" \
    --install-extension "${extension_download}/extension.vsix" \
    --force; then
    rm -rf -- "${extension_download}"
    exit 1
  fi
  rm -rf -- "${extension_download}"
done < <(jq -r '.[] | [.id, .version, .url, .sha256] | @tsv' <<<"${default_extensions}" || true)

rm -f -- "${session_socket}"
nohup "${server_binary}" \
  --auth none \
  --bind-addr "127.0.0.1:${code_server_port}" \
  --disable-telemetry \
  --disable-update-check \
  --disable-workspace-trust \
  --extensions-dir "${extensions_dir}" \
  --link-protection-trusted-domains "https://open-vsx.org" \
  --link-protection-trusted-domains "https://github.com" \
  --link-protection-trusted-domains "https://*.github.com" \
  --link-protection-trusted-domains "https://*.githubusercontent.com" \
  --session-socket "${session_socket}" \
  --user-data-dir "${user_data_dir}" \
  "${WORKSPACE_CHECKOUT_PATH}" </dev/null >"${log_file}" 2>&1 &
server_pid=$!
printf '%s %s\n' "${WORKSPACE_BOOT_TOKEN}" "${server_pid}" >"${state_file}"

for ((attempt = 0; attempt < wait_limit; attempt++)); do
  if server_is_ready; then
    write_ready_marker
    printf 'VS Code is ready (listening on http://127.0.0.1:%s).\n' "${code_server_port}"
    exit 0
  fi
  if ! kill -0 "${server_pid}" 2>/dev/null; then
    wait "${server_pid}" 2>/dev/null || true
    printf '%s\n' 'code-server exited before its health endpoint became ready.' >&2
    exit 1
  fi
  sleep 1
done
kill "${server_pid}" 2>/dev/null || true
wait "${server_pid}" 2>/dev/null || true
rm -f -- "${ready_file}"
printf '%s\n' 'code-server did not become ready before the startup deadline.' >&2
exit 1
