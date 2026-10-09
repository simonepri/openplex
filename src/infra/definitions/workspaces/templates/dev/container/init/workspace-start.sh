#!/usr/bin/env bash
# shellcheck disable=SC2312
# Orchestrates workspace startup tasks including toolchain validation, restore gating, and background services.

set -euo pipefail

: "${WORKSPACE_CELL:?workspace cell was not injected}"
: "${WORKSPACE_CHECKOUT_PATH:?workspace checkout path was not injected}"
: "${WORKSPACE_CELL_INCARNATION:?cell incarnation was not injected}"
: "${WORKSPACE_MACHINE:?workspace machine name was not injected}"
: "${WORKSPACE_REPOSITORY_URL:?workspace repository URL was not injected}"
: "${WORKSPACE_USERNAME:?workspace user was not injected}"
: "${CODER_WORKSPACE_OWNER_ID:?workspace user ID was not injected}"
: "${CODER_WORKSPACE_NAME:?workspace name was not injected}"
: "${WORKSPACE_BOOT_TOKEN:?workspace boot token was not injected}"
: "${CODER_AGENT_TOKEN:?Coder agent token was not injected}"
: "${SSH_ACCESS:=disable}"

# shellcheck disable=SC2034
restore_script="${1:-/etc/workspace/config/kopia-restore.sh}"
zshrc_source="${2:-/etc/workspace/config/workspace.zshrc}"
zsh_plugins_source="${3:-/etc/workspace/config/workspace.zsh_plugins.txt}"
workspace_volume="${4:-/var/lib/workspace}"
tailnet_state_dir="${5:-/var/run/workspace/tailnet}"
shell_script="${6:-/etc/workspace/access/workspace-shell.sh}"
zellij_config_source="${7:-/etc/workspace/config/workspace-zellij.kdl}"
herdr_config_source="${8:-/etc/workspace/config/workspace-herdr.toml}"
snazzy_theme_source="${9:-/etc/workspace/config/workspace-snazzy.zsh}"
runtime_dir="${XDG_RUNTIME_DIR:-/tmp}"
mkdir -p "${runtime_dir}"
chmod 0700 "${runtime_dir}"
mkdir -p "${XDG_CACHE_HOME:-/tmp/cache}" "${BAZEL_OUTPUT_ROOT:-/tmp/bazel}" "${CARGO_TARGET_DIR:-/tmp/cargo-target}"
local_bin="${HOME}/.local/bin"
mkdir -p "${local_bin}"
case ":${PATH}:" in
  *":${local_bin}:"*) ;;
  *) export PATH="${local_bin}:${PATH}" ;;
esac
mounts_ready_file="${runtime_dir}/workspace-mounts-ready"
restore_ready_file="${runtime_dir}/workspace-restore-ready"
setup_ready_file="${runtime_dir}/workspace-setup-ready"
ssh_ready_file="${tailnet_state_dir}/workspace-ssh-ready"
antidote_revision=762857af1fb89ae482f755c26212c0a9a3b68d4c

write_ready_marker() {
  local file=$1 temporary

  temporary="$(mktemp "${file}.XXXXXX")"
  printf '%s\n' "${WORKSPACE_BOOT_TOKEN}" >"${temporary}"
  mv -f -- "${temporary}" "${file}"
}

wait_for_ready_marker() {
  local file=$1

  until [[ -f ${file} ]] && [[ "$(<"${file}")" == "${WORKSPACE_BOOT_TOKEN}" ]]; do
    sleep 1
  done
}

wait_for_file() {
  local file=$1

  until [[ -f ${file} ]]; do
    sleep 1
  done
}

case "${SSH_ACCESS}" in
  enable | disable) ;;
  *)
    printf 'Unsupported SSH access mode: %s\n' "${SSH_ACCESS}" >&2
    exit 1
    ;;
esac

if [[ "$(hostname)" != "${WORKSPACE_MACHINE}" ]]; then
  printf 'workspace hostname must equal machine name %s, found %s\n' \
    "${WORKSPACE_MACHINE}" "$(hostname)" >&2
  exit 1
fi

prepare_repository() {
  local entry incomplete_root origin_url staged_repository

  if [[ -e "${WORKSPACE_CHECKOUT_PATH}/.git" ]]; then
    origin_url="$(git -C "${WORKSPACE_CHECKOUT_PATH}" remote get-url origin 2>/dev/null || true)"
    if [[ -n ${origin_url} && ${origin_url} != "${WORKSPACE_REPOSITORY_URL}" ]]; then
      printf 'repository origin must be %s, found %s\n' \
        "${WORKSPACE_REPOSITORY_URL}" "${origin_url}" >&2
      exit 1
    fi
    if [[ ${origin_url} == "${WORKSPACE_REPOSITORY_URL}" ]] \
      && [[ "$(git -C "${WORKSPACE_CHECKOUT_PATH}" rev-parse --is-inside-work-tree 2>/dev/null)" == true ]] \
      && git -C "${WORKSPACE_CHECKOUT_PATH}" status --porcelain=v1 >/dev/null 2>&1 \
      && {
        git -C "${WORKSPACE_CHECKOUT_PATH}" rev-parse --verify HEAD >/dev/null 2>&1 \
          || [[ -z "$(git ls-remote --heads --tags -- "${WORKSPACE_REPOSITORY_URL}")" ]]
      }; then
      return
    fi
    if [[ -n "$(find "${WORKSPACE_CHECKOUT_PATH}" -mindepth 1 -maxdepth 1 ! -name .git -print -quit)" ]]; then
      printf 'incomplete Git metadata accompanies workspace files at %s; refusing to replace it\n' \
        "${WORKSPACE_CHECKOUT_PATH}" >&2
      exit 1
    fi
  elif [[ -n "$(find "${WORKSPACE_CHECKOUT_PATH}" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    printf 'checkout path is not empty and does not contain a Git repository: %s\n' \
      "${WORKSPACE_CHECKOUT_PATH}" >&2
    exit 1
  fi

  staged_repository="$(mktemp -d "${workspace_volume}/.repository.XXXXXX")"
  rmdir -- "${staged_repository}"
  if ! git clone -- "${WORKSPACE_REPOSITORY_URL}" "${staged_repository}"; then
    rm -rf -- "${staged_repository}"
    return 1
  fi
  if [[ -e "${WORKSPACE_CHECKOUT_PATH}/.git" ]]; then
    incomplete_root="${workspace_volume}/.workspace/incomplete-repositories"
    mkdir -p "${incomplete_root}"
    mv -- "${WORKSPACE_CHECKOUT_PATH}/.git" "${incomplete_root}/$(basename "${staged_repository}").git"
  fi
  shopt -s dotglob nullglob
  for entry in "${staged_repository}"/*; do
    [[ "$(basename "${entry}")" == .git ]] || mv -- "${entry}" "${WORKSPACE_CHECKOUT_PATH}/"
  done
  mv -- "${staged_repository}/.git" "${WORKSPACE_CHECKOUT_PATH}/.git"
  rmdir -- "${staged_repository}"
  shopt -u dotglob nullglob
}

install_shell_plugins() {
  local antidote_dir antidote_root staging

  antidote_root="${HOME}/.local/share/workspace/antidote"
  antidote_dir="${antidote_root}/${antidote_revision}"
  if [[ ! -r "${antidote_dir}/antidote.zsh" ]]; then
    mkdir -p "${antidote_root}"
    staging="$(mktemp -d "${antidote_root}/.install.XXXXXX")"
    rmdir -- "${staging}"
    if ! git clone --branch v2.2.0 --depth 1 \
      https://github.com/mattmc3/antidote.git "${staging}"; then
      rm -rf -- "${staging}"
      return 1
    fi
    if [[ "$(git -C "${staging}" rev-parse HEAD)" != "${antidote_revision}" ]]; then
      rm -rf -- "${staging}"
      printf '%s\n' 'downloaded Antidote revision did not match the pinned release' >&2
      return 1
    fi
    rm -rf -- "${antidote_dir}"
    mv -- "${staging}" "${antidote_dir}"
  fi
  ln -sfn -- "${antidote_dir}" "${antidote_root}/current"
}

repair_interrupted_rust() {
  local data_dir rust_version rustup toolchain toolchain_path

  data_dir="${MISE_DATA_DIR:-${XDG_DATA_HOME:-${HOME}/.local/share}/mise}"
  rustup="${data_dir}/cargo/bin/rustup"
  [[ -x ${rustup} ]] || return 0
  rust_version="$(
    mise ls --json rust \
      | jq -er '
        map(.requested_version | strings) | unique |
        select(length == 1) | .[0] | strings |
        select(test("^[0-9]+\\.[0-9]+\\.[0-9]+$"))
      '
  )"
  while read -r toolchain _; do
    [[ ${toolchain} == "${rust_version}"-* ]] || continue
    toolchain_path="${data_dir}/rustup/toolchains/${toolchain}"
    if ! CARGO_HOME="${data_dir}/cargo" RUSTUP_HOME="${data_dir}/rustup" \
      "${rustup}" run "${toolchain}" rustc --version >/dev/null 2>&1; then
      rm -rf -- "${toolchain_path}"
    fi
  done < <(
    CARGO_HOME="${data_dir}/cargo" RUSTUP_HOME="${data_dir}/rustup" \
      "${rustup}" toolchain list
  )
}

repair_interrupted_zig() {
  local data_dir zig_path zig_version

  data_dir="${MISE_DATA_DIR:-${XDG_DATA_HOME:-${HOME}/.local/share}/mise}"
  zig_version="$(
    mise ls --json zig \
      | jq -er '
        map(.requested_version | strings) | unique |
        select(length == 1) | .[0] | strings |
        select(test("^[0-9]+\\.[0-9]+\\.[0-9]+$"))
      '
  )"
  zig_path="${data_dir}/installs/zig/${zig_version}"
  [[ -d ${zig_path} ]] || return 0
  if [[ ! -x "${zig_path}/zig" ]] || ! "${zig_path}/zig" version >/dev/null 2>&1; then
    rm -rf -- "${zig_path}"
  fi
}

configure_codex_sandbox() {
  local bwrap_bin=""
  local mise_data_dir="${MISE_DATA_DIR:-${XDG_DATA_HOME:-${HOME}/.local/share}/mise}"
  local codex_dir="${HOME}/.codex"
  local codex_config="${codex_dir}/config.toml"
  local tmp_config=""

  if [[ ! -x "${mise_data_dir}/shims/bwrap" ]]; then
    bwrap_bin="$(find "${mise_data_dir}/installs/codex" -name bwrap -type f -perm -111 2>/dev/null | head -n 1 || true)"
    if [[ -n ${bwrap_bin} ]]; then
      mkdir -p "${mise_data_dir}/shims"
      ln -sf "${bwrap_bin}" "${mise_data_dir}/shims/bwrap"
    fi
  fi

  mkdir -p "${codex_dir}"
  if [[ -f ${codex_config} ]]; then
    if ! grep -q "^sandbox_mode" "${codex_config}"; then
      tmp_config="${codex_config}.tmp.$$"
      printf '%s\n' 'sandbox_mode = "danger-full-access"' >"${tmp_config}"
      cat "${codex_config}" >>"${tmp_config}"
      mv -f -- "${tmp_config}" "${codex_config}"
    fi
  else
    printf '%s\n' 'sandbox_mode = "danger-full-access"' >"${codex_config}"
  fi
}

configure_agy_telemetry() {
  /usr/bin/python3 /usr/local/lib/agy-otel/hook.py install || true
}


install_tools() {
  cd "${WORKSPACE_CHECKOUT_PATH}"
  local repo_mise_file="${WORKSPACE_CHECKOUT_PATH}/mise.toml"
  local global_mise_file="/etc/workspace/config/config.toml"
  local hash_file="${workspace_volume}/.mise.sha256"
  local current_hash=""
  local github_token=""

  configure_codex_sandbox
  configure_agy_telemetry

  if [[ -z ${GITHUB_TOKEN:-} ]] && command -v coder >/dev/null 2>&1; then
    github_token="$(coder external-auth access-token github 2>/dev/null || true)"
    if [[ -n ${github_token} ]]; then
      export GITHUB_TOKEN="${github_token}"
    fi
  fi

  local hash_inputs=()
  if [[ -f ${global_mise_file} ]]; then
    hash_inputs+=("${global_mise_file}")
  fi
  if [[ -f ${repo_mise_file} ]]; then
    hash_inputs+=("${repo_mise_file}")
  fi

  if [[ -f ${repo_mise_file} ]]; then
    mise trust "${repo_mise_file}" >/dev/null 2>&1 || true
  fi

  local mise_reinstalled=true
  if [[ ${#hash_inputs[@]} -gt 0 ]]; then
    current_hash="$(sha256sum "${hash_inputs[@]}" | sha256sum | cut -d' ' -f1)"
    if [[ -f ${hash_file} ]] && [[ "$(<"${hash_file}")" == "${current_hash}" ]]; then
      printf '[setup] mise configs unchanged; skipping tool reinstall.\n'
      mise_reinstalled=false
    fi
  fi

  if [[ ${mise_reinstalled} == true ]]; then
    repair_interrupted_zig
    CI=1 mise install zig
    repair_interrupted_rust
    CI=1 mise install rust
    CI=1 mise install

    if [[ -n ${current_hash} ]]; then
      printf '%s\n' "${current_hash}" >"${hash_file}"
    fi
  fi

}

wait_for_ready_marker "${mounts_ready_file}"
wait_for_ready_marker "${restore_ready_file}"

if [[ ${SSH_ACCESS} == enable ]]; then
  wait_for_file "${ssh_ready_file}"
fi

printf 'Starting workspace environment setup...\n'
(
  "${shell_script}" "${zshrc_source}" "${zsh_plugins_source}" "${zellij_config_source}" "${herdr_config_source}" "${snazzy_theme_source}"
  install_shell_plugins
) &
pid_shell=$!

(
  prepare_repository
) &
pid_repo=$!

wait "${pid_repo}"

(
  install_tools
) &
pid_tools=$!

wait "${pid_shell}"
wait "${pid_tools}"

write_ready_marker "${setup_ready_file}"
printf 'Workspace setup completed successfully.\n'
