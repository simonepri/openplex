#!/usr/bin/env bash
# Tests that AWS EC2NodeClasses receive the pre-kubelet eStargz contract and fail closed before nodeadm.

# shellcheck disable=SC2016,SC2312
set -euo pipefail

if (($# != 7)); then
  printf 'usage: %s RENDER BOOTSTRAP CONFIG UNIT DROP_IN NODE_CONFIG YQ\n' "$0" >&2
  exit 2
fi

readonly render=$1
readonly bootstrap=$2
readonly config=$3
readonly unit=$4
readonly drop_in=$5
readonly node_config=$6
readonly yq_bin=$7
test_dir="$(mktemp -d)"
readonly test_dir
readonly cpu_user_data="${test_dir}/cpu-user-data"
readonly gpu_user_data="${test_dir}/gpu-user-data"
readonly cloud_config="${test_dir}/cloud-config.yaml"
readonly rendered_node_config="${test_dir}/node-config.yaml"
readonly fake_bin="${test_dir}/bin"
readonly trace="${test_dir}/trace"

# shellcheck disable=SC2329 # Invoked by the EXIT trap.
cleanup() {
  rm -rf "${test_dir}"
}
trap cleanup EXIT

yq() {
  "${yq_bin}" "$@"
}

for name in dynamic-cpu dynamic-gpu; do
  [[ "$(NAME="${name}" yq -r '
    select(.kind == "EC2NodeClass") | select(.metadata.name == strenv(NAME)) |
    .spec.amiSelectorTerms[0].alias
  ' "${render}")" == "al2023@v20260520" ]]
  NAME="${name}" yq -r '
    select(.kind == "EC2NodeClass") | select(.metadata.name == strenv(NAME)) |
    .spec.userData
  ' "${render}" | grep -Fq 'MIME-Version: 1.0'
done

NAME=dynamic-cpu yq -r '
  select(.kind == "EC2NodeClass") | select(.metadata.name == strenv(NAME)) |
  .spec.userData
' "${render}" >"${cpu_user_data}"
NAME=dynamic-gpu yq -r '
  select(.kind == "EC2NodeClass") | select(.metadata.name == strenv(NAME)) |
  .spec.userData
' "${render}" >"${gpu_user_data}"
cmp "${cpu_user_data}" "${gpu_user_data}"
if (($(wc -c <"${cpu_user_data}") > 16384)); then
  printf 'AWS eStargz user data exceeds the EC2 16 KiB limit\n' >&2
  exit 1
fi

for name in cpu-on-demand cpu-spot; do
  [[ "$(NAME="${name}" yq -r '
    select(.kind == "NodePool") | select(.metadata.name == strenv(NAME)) |
    .spec.template.spec.requirements[] |
    select(.key == "kubernetes.io/arch") | .values[]
  ' "${render}")" == amd64 ]]
  [[ "$(NAME="${name}" yq -r '
    select(.kind == "NodePool") | select(.metadata.name == strenv(NAME)) |
    .spec.template.metadata.labels."cpu-capability.avx2"
  ' "${render}")" == "true" ]]
  [[ "$(NAME="${name}" yq -r '
    select(.kind == "NodePool") | select(.metadata.name == strenv(NAME)) |
    (.spec.template.spec.startupTaints // [] | length)
  ' "${render}")" == 0 ]]
done

assert_embedded() {
  local source=$1 line
  while IFS= read -r line; do
    [[ -z ${line} || ${line} == \#* ]] && continue
    if ! grep -Fq -- "${line}" "${cpu_user_data}"; then
      printf '%s is missing generated source line: %s\n' "${cpu_user_data}" "${line}" >&2
      exit 1
    fi
  done <"${source}"
}

for source in "${bootstrap}" "${config}" "${unit}" "${drop_in}" "${node_config}"; do
  assert_embedded "${source}"
done

line_number() {
  grep -nF -- "$1" "${cpu_user_data}" | awk -F: 'NR == 1 { print $1 }'
}

cloud_config_line="$(line_number 'Content-Type: text/cloud-config; charset="us-ascii"')"
readonly cloud_config_line
shell_line="$(line_number 'Content-Type: text/x-shellscript; charset="us-ascii"')"
readonly shell_line
node_config_line="$(line_number 'Content-Type: application/node.eks.aws')"
readonly node_config_line
if ! ((cloud_config_line < shell_line && shell_line < node_config_line)); then
  printf 'AWS eStargz MIME parts must order cloud-config, bootstrap, then NodeConfig\n' >&2
  exit 1
fi
[[ "$(awk 'NF { line = $0 } END { print line }' "${cpu_user_data}")" == '--//--' ]]

awk '
  /^#cloud-config$/ { body = 1 }
  body && /^--\/\/$/ { exit }
  body { print }
' "${cpu_user_data}" >"${cloud_config}"
[[ "$(yq -r '.write_files | length' "${cloud_config}")" == 3 ]]
[[ "$(yq -r '.write_files[0].path' "${cloud_config}")" == "/etc/containerd-stargz-grpc/config.toml" ]]
[[ "$(yq -r '.write_files[1].path' "${cloud_config}")" == "/etc/systemd/system/stargz-snapshotter.service" ]]
[[ "$(yq -r '.write_files[2].path' "${cloud_config}")" == "/etc/systemd/system/containerd.service.d/10-stargz.conf" ]]

awk '
  /^Content-Type: application\/node[.]eks[.]aws$/ { header = 1; next }
  header && !body && /^$/ { body = 1; next }
  body && /^--\/\/--$/ { exit }
  body { print }
' "${cpu_user_data}" >"${rendered_node_config}"
[[ "$(yq -r '.apiVersion' "${rendered_node_config}")" == "node.eks.aws/v1alpha1" ]]
[[ "$(yq -r '.kind' "${rendered_node_config}")" == "NodeConfig" ]]
grep -Fq 'snapshotter = "stargz"' <(yq -r '.spec.containerd.config' "${rendered_node_config}")
[[ "$(yq -r '
  .spec.kubelet.flags[] |
  select(. == "--image-service-endpoint=unix:///run/containerd-stargz-grpc/containerd-stargz-grpc.sock")
' "${rendered_node_config}")" == "--image-service-endpoint=unix:///run/containerd-stargz-grpc/containerd-stargz-grpc.sock" ]]
[[ "$(yq -r '.spec.kubelet.config.memorySwap.swapBehavior' "${rendered_node_config}")" == "LimitedSwap" ]]
[[ "$(yq -r '.spec.kubelet.config.failSwapOn' "${rendered_node_config}")" == "false" ]]

for contract in \
  'path: /etc/containerd-stargz-grpc/config.toml' \
  'path: /etc/systemd/system/stargz-snapshotter.service' \
  'path: /etc/systemd/system/containerd.service.d/10-stargz.conf' \
  'readonly stargz_image="ghcr.io/containerd/stargz-snapshotter:0.18.2-kind@sha256:74cd59bda98d35bcd2e0067ccb8304e7d4ae25c9a1c8906c8d5322a3d806e441"' \
  'ctr images pull "${stargz_image}"' \
  'ctr images mount "${stargz_image}"' \
  'enable_keychain = true' \
  'image_service_path = "/run/containerd/containerd.sock"' \
  'snapshotter = "stargz"' \
  'disable_snapshot_annotations = false' \
  '[proxy_plugins.stargz]' \
  '--image-service-endpoint=unix:///run/containerd-stargz-grpc/containerd-stargz-grpc.sock' \
  'failSwapOn: false' \
  'swapBehavior: LimitedSwap' \
  'Requires=stargz-snapshotter.service'; do
  grep -Fq -- "${contract}" "${cpu_user_data}"
done

if grep -Eqi '127[.]0[.]0[.]1:4001|dragonfly|NoVerifier|skip_verify|insecure|http://|\[resolver|mirrors' "${cpu_user_data}"; then
  printf 'AWS eStargz user data must use authenticated, TLS-verified direct ECR fallback only\n' >&2
  exit 1
fi

pull_line="$(line_number 'ctr images pull')"
readonly pull_line
mount_line="$(line_number 'ctr images mount')"
readonly mount_line
start_line="$(line_number 'systemctl enable --now stargz-snapshotter.service')"
readonly start_line
if ! ((pull_line < mount_line && mount_line < start_line)); then
  printf 'AWS eStargz bootstrap must pull before mounting and start only afterward\n' >&2
  exit 1
fi

mkdir -p "${fake_bin}"
cat >"${fake_bin}/ctr" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'ctr %s\n' "$*" >>"$TRACE"
if [[ "$1" == "images" && "$2" == "pull" && "${FAIL_IMAGE_PULL:-false}" == true ]]; then
  exit 1
fi
if [[ "$1" == "images" && "$2" == "mount" ]]; then
  mount_dir=$4
  mkdir -p "$mount_dir/usr/local/bin"
  printf '#!/usr/bin/env bash\nexit 0\n' >"$mount_dir/usr/local/bin/containerd-stargz-grpc"
  chmod 0755 "$mount_dir/usr/local/bin/containerd-stargz-grpc"
fi
EOF
cat >"${fake_bin}/install" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'install\n' >>"$TRACE"
EOF
cat >"${fake_bin}/systemctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'systemctl %s\n' "$*" >>"$TRACE"
if [[ "$*" == 'enable --now stargz-snapshotter.service' && "${FAIL_SERVICE_START:-false}" == true ]]; then
  exit 1
fi
EOF
cat >"${fake_bin}/sleep" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod 0755 "${fake_bin}"/*

if PATH="${fake_bin}:${PATH}" TRACE="${trace}" FAIL_IMAGE_PULL=true bash "${bootstrap}"; then
  printf 'Image pull failure must abort AWS eStargz bootstrap\n' >&2
  exit 1
fi
grep -Fq 'ctr images pull' "${trace}"
if grep -Eq '^(install|systemctl enable)' "${trace}"; then
  printf 'Image pull failure reached AWS eStargz installation or startup\n' >&2
  exit 1
fi

: >"${trace}"
if PATH="${fake_bin}:${PATH}" TRACE="${trace}" FAIL_SERVICE_START=true bash "${bootstrap}"; then
  printf 'Snapshotter service failure must abort AWS eStargz bootstrap\n' >&2
  exit 1
fi
grep -Fxq 'systemctl enable --now stargz-snapshotter.service' "${trace}"

: >"${trace}"
if PATH="${fake_bin}:${PATH}" TRACE="${trace}" bash "${bootstrap}"; then
  printf 'Missing snapshotter socket must abort AWS eStargz bootstrap\n' >&2
  exit 1
fi
grep -Fxq 'systemctl status --no-pager stargz-snapshotter.service' "${trace}"
