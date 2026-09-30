#!/usr/bin/env bash
# shellcheck disable=SC2312
# Tests workspace startup sequencing, volume restore gating, shell seeding, and toolchain repair routines.

set -euo pipefail

subject="${1:?workspace start script path was not supplied}"
shell_subject="${2:?workspace shell script path was not supplied}"
jq_bin="${3:?jq path was not supplied}"
test_root="$(mktemp -d)"
readonly subject test_root
trap 'rm -rf -- "$test_root"' EXIT

bin="${test_root}/bin"
events="${test_root}/events"
zshrc="${test_root}/workspace.zshrc"
zsh_plugins="${test_root}/workspace.zsh_plugins.txt"
zellij_config="${test_root}/workspace-zellij.kdl"
herdr_config="${test_root}/workspace-herdr.toml"
snazzy_theme="${test_root}/workspace-snazzy.zsh"
mise_data_dir="${test_root}/mise"
rustup="${mise_data_dir}/cargo/bin/rustup"
mkdir -p \
  "${bin}" \
  "${mise_data_dir}/cargo/bin" \
  "${mise_data_dir}/rustup/toolchains" \
  "${test_root}/checkout" \
  "${test_root}/runtime" \
  "${test_root}/tailnet" \
  "${test_root}/volume"
cp "${jq_bin}" "${bin}/jq"
printf '%s\n' 'PROMPT="workspace> "' >"${zshrc}"
printf '%s\n' 'zsh-users/zsh-autosuggestions pin:fixture' >"${zsh_plugins}"
printf '%s\n' 'theme "snazzy"' >"${zellij_config}"
printf '%s\n' 'onboarding = false' >"${herdr_config}"
printf '%s\n' '# snazzy fixture' >"${snazzy_theme}"

cat >"${bin}/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
url="${*: -1}"
if [[ "$url" == http://127.0.0.1:19847/ ]]; then
	printf '%s\n' proxy-ready >>"$TEST_EVENTS"
	exit 0
fi
printf '%s\n' repository-password >>"$TEST_EVENTS"
printf '{"repositoryPassword":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}'
EOF

cat >"${bin}/hostname" <<'EOF'
#!/bin/sh
printf '%s\n' ldap-dev
EOF

cat >"${bin}/git" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "$*" == 'clone -- https://git.example/cluster-config.git '* ]]; then
	mkdir -p "${*: -1}/.git"
	printf '%s\n' clone >>"$TEST_EVENTS"
	exit 0
fi
if [[ "$*" == 'clone --branch v2.2.0 --depth 1 https://github.com/mattmc3/antidote.git '* ]]; then
	mkdir -p "${*: -1}/.git"
	touch "${*: -1}/antidote.zsh"
	printf '%s\n' antidote >>"$TEST_EVENTS"
	exit 0
fi
if [[ "$1" == -C && "$3 $4" == 'rev-parse HEAD' ]]; then
	printf '%s\n' 762857af1fb89ae482f755c26212c0a9a3b68d4c
	exit 0
fi
if [[ "$1" == -C && "$3 $4" == 'rev-parse --is-inside-work-tree' ]]; then
	[[ ! -e "$2/.git/incomplete" ]] || exit 2
	printf '%s\n' true
	exit 0
fi
if [[ "$1" == -C && "$3 $4 $5" == 'remote get-url origin' ]]; then
	[[ ! -e "$2/.git/incomplete" ]] || exit 2
	printf '%s\n' https://git.example/cluster-config.git
	exit 0
fi
if [[ "$1" == -C && "$3 $4 $5" == 'rev-parse --verify HEAD' ]]; then
	[[ ! -e "$2/.git/incomplete" ]] || exit 2
	printf '%s\n' 0123456789abcdef0123456789abcdef01234567
	exit 0
fi
if [[ "$1" == -C && "$3 $4" == 'status --porcelain=v1' ]]; then
	[[ ! -e "$2/.git/incomplete" ]] || exit 2
	exit 0
fi
exit 2
EOF

cat >"${bin}/mise" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'mise %s\n' "$*" >>"$TEST_EVENTS"
case "$*" in
	'exec kopia@0.23.1 -- kopia repository connect s3 '*) exit 0 ;;
	'exec kopia@0.23.1 -- kopia policy set '*)
		[[ "$*" == *"--keep-latest 3 --keep-hourly 12 --keep-daily 7 --keep-weekly 4 --keep-monthly 0 --keep-annual 0"* ]]
		exit 0
		;;
	'ls --json rust')
		printf '%s\n' '[{"requested_version":"1.98.0"}]'
		;;
	'ls --json zig')
		printf '%s\n' '[{"requested_version":"0.15.2"}]'
		;;
	'install zig')
		[[ ! -e "$MISE_DATA_DIR/installs/zig/0.15.2" ]]
		printf '%s\n' zig-reinstalled >>"$TEST_EVENTS"
		;;
	trust\ * | install*) exit 0 ;;
	*) exit 2 ;;
esac
EOF

cat >"${rustup}" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
case "$*" in
	'toolchain list')
		find "$RUSTUP_HOME/toolchains" -mindepth 1 -maxdepth 1 -type d \
			-exec basename {} \; | sort
		;;
	'run '*' rustc --version')
		toolchain="$(printf '%s\n' "$*" | cut -d' ' -f2)"
		[[ -f "$RUSTUP_HOME/toolchains/$toolchain/.rustup-valid" ]] || exit 1
		"$RUSTUP_HOME/toolchains/$toolchain/bin/rustc" --version
		;;
	*) exit 2 ;;
esac
EOF

cat >"${bin}/restore" <<'EOF'
#!/bin/sh
set -eu
printf '%s\n' restore >>"$TEST_EVENTS"
[ "${TEST_RESTORE_FAIL:-false}" != true ]
rm -f -- "$1/.kopiaignore"
if [ "${TEST_RESTORE_SHELL:-false}" = true ]; then
  printf '%s\n' 'PROMPT="restored> "' >"$HOME/.zshrc"
  printf '%s\n' 'owner/restored-plugin' >"$HOME/.zsh_plugins.txt"
fi
printf '%s\n' restored
EOF
cat >"${bin}/touch" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
/usr/bin/touch "$@"
EOF
cat >"${bin}/sleep" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
for marker in "$XDG_RUNTIME_DIR/workspace-code-server-ready" "$XDG_RUNTIME_DIR/workspace-paseo-ready"; do
  if [[ -f "$marker" ]] && [[ "$(<"$marker")" != "$WORKSPACE_BOOT_TOKEN" ]]; then
    [[ -r "$HOME/.zshrc" && -r "$HOME/.zsh_plugins.txt" ]]
    printf '%s-ready\n' "$(basename "$marker")" >>"$TEST_EVENTS"
    printf '%s\n' "$WORKSPACE_BOOT_TOKEN" >"$marker"
  fi
done
if [[ ! -e "$TEST_SSH_READY_FILE" ]]; then
  printf '%s\n' ssh-ready >>"$TEST_EVENTS"
  /usr/bin/touch "$TEST_SSH_READY_FILE"
fi
EOF
chmod +x "${bin}"/* "${rustup}"

common_env=(
  "CODER_AGENT_TOKEN=fixture-agent-token"
  "HOME=${test_root}/home"
  "MISE_DATA_DIR=${mise_data_dir}"
  "KOPIA_REPOSITORY_ACCESS_KEY_ID=LOOPBACKACCESSKEY"
  "KOPIA_REPOSITORY_SECRET_ACCESS_KEY=loopback-secret"
  "WORKSPACE_CELL=cell-eaws-lh1"
  "WORKSPACE_CHECKOUT_PATH=${test_root}/checkout"
  "WORKSPACE_USERNAME=ldap"
  "WORKSPACE_CELL_INCARNATION=incarnation-1"
  "WORKSPACE_MACHINE=ldap-dev"
  "WORKSPACE_REPOSITORY_URL=https://git.example/cluster-config.git"
  "KOPIA_SNAPSHOT_BROKER_URL=https://ctrl-eaws-lh1-runtime-services.tailnet.k8s.example.invalid:8444"
  "SSH_ACCESS=enable"
  "WORKSPACE_TEAM=examples"
  "TEST_EVENTS=${events}"
  "CODER_WORKSPACE_OWNER_ID=123e4567-e89b-42d3-a456-426614174000"
  "CODER_WORKSPACE_NAME=dev"
  "TEST_WORKSPACE_VOLUME=${test_root}/volume"
  "TEST_SSH_READY_FILE=${test_root}/tailnet/workspace-ssh-ready"
  "WORKSPACE_BOOT_TOKEN=boot-current"
  "XDG_RUNTIME_DIR=${test_root}/runtime"
  "PATH=${bin}:${PATH}"
)

subject_args=("${bin}/restore" "${zshrc}" "${zsh_plugins}" "${test_root}/volume" "${test_root}/tailnet" "${shell_subject}" "${zellij_config}" "${herdr_config}" "${snazzy_theme}")
env "${common_env[@]}" bash "${shell_subject}" "${zshrc}" "${zsh_plugins}" "${zellij_config}" "${herdr_config}" "${snazzy_theme}"
cmp "${zshrc}" "${test_root}/home/.zshrc"
cmp "${zsh_plugins}" "${test_root}/home/.zsh_plugins.txt"
cmp "${zellij_config}" "${test_root}/home/.config/zellij/config.kdl"
cmp "${herdr_config}" "${test_root}/home/.config/herdr/config.toml"
cmp "${snazzy_theme}" "${test_root}/home/.config/zsh/themes/snazzy.zsh"
mkdir -p "${test_root}/linked-home"
for name in .zshrc .zsh_plugins.txt; do
  ln -s "${test_root}/owner-${name}" "${test_root}/linked-home/${name}"
done
env HOME="${test_root}/linked-home" bash "${shell_subject}" "${zshrc}" "${zsh_plugins}" "${zellij_config}" "${herdr_config}" "${snazzy_theme}"
for name in .zshrc .zsh_plugins.txt; do
  [[ "$(readlink "${test_root}/linked-home/${name}")" == "${test_root}/owner-${name}" ]]
  [[ ! -e "${test_root}/owner-${name}" ]]
done
ready="${test_root}/runtime/workspace-restore-ready"
mounts_ready="${test_root}/runtime/workspace-mounts-ready"
zig_root="${mise_data_dir}/installs/zig/0.15.2"
mkdir -p "${zig_root}"
codex_bwrap="${mise_data_dir}/installs/codex/0.159.0/codex-resources/bwrap"
mkdir -p "$(dirname "${codex_bwrap}")"
cat >"${codex_bwrap}" <<'EOF'
#!/bin/sh
printf 'bwrap-mock\n'
EOF
chmod +x "${codex_bwrap}"
printf '%s\n' boot-current >"${ready}"
printf '%s\n' boot-current >"${mounts_ready}"
printf '%s\n' boot-stale >"${test_root}/runtime/workspace-code-server-ready"
printf '%s\n' boot-stale >"${test_root}/runtime/workspace-paseo-ready"
env "${common_env[@]}" bash "${subject}" "${subject_args[@]}"
[[ -x "${mise_data_dir}/shims/bwrap" ]]
[[ "$("${mise_data_dir}/shims/bwrap")" == bwrap-mock ]]
[[ -f "${test_root}/home/.codex/config.toml" ]]
grep -q 'sandbox_mode = "danger-full-access"' "${test_root}/home/.codex/config.toml"
[[ ! -e ${zig_root} ]]
grep -Fx ssh-ready "${events}" >/dev/null
[[ "$(<"${test_root}/runtime/workspace-setup-ready")" == boot-current ]]
grep -Fx clone "${events}" >/dev/null
grep -Fx antidote "${events}" >/dev/null
ready_line="$(grep -n -m1 '^ssh-ready$' "${events}" | cut -d: -f1)"
antidote_line="$(grep -n -m1 '^antidote$' "${events}" | cut -d: -f1)"
((ready_line < antidote_line))
grep -Fx zig-reinstalled "${events}" >/dev/null
cmp "${zshrc}" "${test_root}/home/.zshrc"
cmp "${zsh_plugins}" "${test_root}/home/.zsh_plugins.txt"
cmp "${zellij_config}" "${test_root}/home/.config/zellij/config.kdl"
cmp "${herdr_config}" "${test_root}/home/.config/herdr/config.toml"
cmp "${snazzy_theme}" "${test_root}/home/.config/zsh/themes/snazzy.zsh"
if grep -F 'paseo daemon' "${events}" >/dev/null; then
  printf '%s\n' 'slow workspace preparation started Paseo' >&2
  exit 1
fi

touch "${test_root}/checkout/.git/incomplete"
: >"${events}"
printf '%s\n' boot-current >"${ready}"
printf '%s\n' boot-current >"${mounts_ready}"
env "${common_env[@]}" bash "${subject}" "${subject_args[@]}"
[[ ! -e "${test_root}/checkout/.git/incomplete" ]]
find "${test_root}/volume/.workspace/incomplete-repositories" \
  -mindepth 1 -maxdepth 1 -type d -name '*.git' -print -quit | grep . >/dev/null
grep -Fx clone "${events}" >/dev/null

printf '%s\n' 'PROMPT="personal> "' >"${test_root}/home/.zshrc"
printf '%s\n' 'owner/personal-plugin' >"${test_root}/home/.zsh_plugins.txt"
rust_toolchain=1.98.0-aarch64-unknown-linux-gnu
rust_root="${mise_data_dir}/rustup/toolchains/${rust_toolchain}"
mkdir -p "${rust_root}/bin"
cat >"${rust_root}/bin/rustc" <<'EOF'
#!/bin/sh
printf '%s\n' 'rustc 1.98.0'
EOF
chmod +x "${rust_root}/bin/rustc"
: >"${events}"
printf '%s\n' boot-current >"${ready}"
printf '%s\n' boot-current >"${mounts_ready}"
env "${common_env[@]}" bash "${subject}" "${subject_args[@]}"
[[ ! -e ${rust_root} ]]
grep -Fx 'mise install rust' "${events}" >/dev/null
grep -Fx 'PROMPT="personal> "' "${test_root}/home/.zshrc" >/dev/null
grep -Fx 'owner/personal-plugin' "${test_root}/home/.zsh_plugins.txt" >/dev/null
