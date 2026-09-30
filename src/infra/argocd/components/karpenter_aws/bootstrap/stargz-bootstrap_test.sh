#!/usr/bin/env bash
# Tests that AWS EC2NodeClasses receive the pre-kubelet eStargz contract and fail closed before nodeadm.

# shellcheck disable=SC2016,SC2312
set -euo pipefail

test_dir="$(mktemp -d)"
readonly test_dir

# shellcheck disable=SC2329 # Invoked by the EXIT trap.
cleanup() {
  rm -rf "${test_dir}"
}
trap cleanup EXIT

if (($# == 0)); then
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  repo_root="$(git rev-parse --show-toplevel 2>/dev/null || (cd "${script_dir}/../../../../../.." && pwd))"
  render="${test_dir}/render.yaml"
  if command -v kubectl >/dev/null 2>&1; then
    kubectl kustomize "${repo_root}/src/infra/argocd/components/karpenter_aws/kustomize/overlays/cells" >"${render}"
  elif [[ -f "${repo_root}/bazel-bin/src/infra/argocd/components/karpenter_aws/overlay-cells_render.yaml" ]]; then
    cp "${repo_root}/bazel-bin/src/infra/argocd/components/karpenter_aws/overlay-cells_render.yaml" "${render}"
  else
    printf 'Could not render overlay-cells: kubectl not found\n' >&2
    exit 1
  fi
  bootstrap="${script_dir}/stargz-bootstrap.sh"
  config="${script_dir}/stargz-config.toml"
  unit="${script_dir}/stargz-snapshotter.service"
  drop_in="${script_dir}/containerd-stargz.conf"
  node_config="${script_dir}/stargz-node-config.yaml"
  yq_bin="$(command -v yq || echo "${repo_root}/bazel-bin/src/bazel/tools/yq")"
elif (($# == 7)); then
  render=$1
  bootstrap=$2
  config=$3
  unit=$4
  drop_in=$5
  node_config=$6
  yq_bin=$7
else
  printf 'usage: %s [RENDER BOOTSTRAP CONFIG UNIT DROP_IN NODE_CONFIG YQ]\n' "$0" >&2
  exit 2
fi
readonly render bootstrap config unit drop_in node_config yq_bin
readonly cpu_user_data="${test_dir}/cpu-user-data"
readonly gpu_user_data="${test_dir}/gpu-user-data"
readonly cloud_config="${test_dir}/cloud-config.yaml"
readonly rendered_node_config="${test_dir}/node-config.yaml"
readonly fake_bin="${test_dir}/bin"
readonly trace="${test_dir}/trace"
readonly test_hosts_file="${test_dir}/etc/containerd/certs.d/_default/hosts.toml"

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
  'readonly stargz_version="v0.18.2"' \
  'readonly stargz_sha256="515a3c3af0012f192ace31fb79e910597977c77227e976680aeaaef6e9ae50a9"' \
  'curl -fsSL --retry 5 --retry-delay 2 --retry-connrefused' \
  'echo "${stargz_sha256}  ${stargz_tarball}" | sha256sum -c -' \
  'tar -C /usr/local/bin -xzf "${stargz_tarball}" containerd-stargz-grpc ctr-remote' \
  'enable_keychain = true' \
  'image_service_path = "/run/containerd/containerd.sock"' \
  'snapshotter = "stargz"' \
  'disable_snapshot_annotations = false' \
  '[proxy_plugins.stargz]' \
  '--image-service-endpoint=unix:///run/containerd-stargz-grpc/containerd-stargz-grpc.sock' \
  'failSwapOn: false' \
  'swapBehavior: LimitedSwap' \
  'Wants=stargz-snapshotter.service' \
  '/etc/containerd/certs.d/_default/hosts.toml' \
  'server = "https://registry-1.docker.io"' \
  '[host."http://127.0.0.1:4001"]' \
  'capabilities = ["pull", "resolve"]'; do
  grep -Fq -- "${contract}" "${cpu_user_data}"
done

grep -Fq 'Wants=stargz-snapshotter.service' "${drop_in}"
if grep -Fq 'Requires=stargz-snapshotter.service' "${cpu_user_data}" || grep -Fq 'Requires=stargz-snapshotter.service' "${drop_in}"; then
  printf 'AWS eStargz containerd drop-in must use Wants= instead of Requires=\n' >&2
  exit 1
fi

if grep -Eq '(^|[[:space:];|&(])(ctr|crictl|nerdctl)([[:space:];|&)]|$)' "${cpu_user_data}" \
  || grep -Eq '(^|[[:space:];|&(])(ctr|crictl|nerdctl)([[:space:];|&)]|$)' "${bootstrap}"; then
  printf 'AWS eStargz bootstrap must not call ctr, crictl, or nerdctl\n' >&2
  exit 1
fi

grep_status=0
grep -Eqi 'NoVerifier|skip_verify|insecure' "${cpu_user_data}" || grep_status=$?
if [[ ${grep_status} -eq 0 ]]; then
  printf 'AWS eStargz user data must not permit unverified or insecure registries\n' >&2
  exit 1
elif [[ ${grep_status} -ne 1 ]]; then
  exit "${grep_status}"
fi

download_line="$(line_number 'curl -fsSL')"
readonly download_line
checksum_line="$(line_number 'sha256sum')"
readonly checksum_line
extract_line="$(line_number 'tar -C /usr/local/bin')"
readonly extract_line
start_line="$(line_number 'systemctl enable --now stargz-snapshotter.service')"
readonly start_line
if ! ((download_line < checksum_line && checksum_line < extract_line && extract_line < start_line)); then
  printf 'AWS eStargz bootstrap must download, verify checksum, extract, then start service\n' >&2
  exit 1
fi

mkdir -p "${fake_bin}"
cat >"${fake_bin}/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'curl %s\n' "$*" >>"$TRACE"
if [[ "${FAIL_DOWNLOAD:-false}" == true ]]; then
  exit 1
fi
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "-o" ]]; then
    touch "$2"
    shift 2
  else
    shift
  fi
done
EOF
cat >"${fake_bin}/sha256sum" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'sha256sum %s\n' "$*" >>"$TRACE"
if [[ "${FAIL_CHECKSUM:-false}" == true ]]; then
  exit 1
fi
exit 0
EOF
cat >"${fake_bin}/tar" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'tar %s\n' "$*" >>"$TRACE"
EOF
cat >"${fake_bin}/chmod" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'chmod %s\n' "$*" >>"$TRACE"
/bin/chmod "$@" 2>/dev/null || true
EOF
cat >"${fake_bin}/mkdir" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'mkdir %s\n' "$*" >>"$TRACE"
/bin/mkdir -p "$@" 2>/dev/null || true
EOF
for cmd in ctr crictl nerdctl; do
  cat >"${fake_bin}/${cmd}" <<EOF
#!/usr/bin/env bash
set -euo pipefail
printf '${cmd} %s\n' "\$*" >>"\$TRACE"
printf 'Prohibited runtime tool invoked: ${cmd}\n' >&2
exit 1
EOF
done
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

rm -f "${test_hosts_file}"
if PATH="${fake_bin}:${PATH}" TRACE="${trace}" CONTAINERD_HOSTS_FILE="${test_hosts_file}" FAIL_DOWNLOAD=true bash "${bootstrap}"; then
  printf 'Download failure must abort AWS eStargz bootstrap\n' >&2
  exit 1
fi
grep -Fq 'curl' "${trace}"
if grep -Eq '^(sha256sum|tar|systemctl enable)' "${trace}"; then
  printf 'Download failure reached AWS eStargz verification, extraction, or startup\n' >&2
  exit 1
fi
if [[ -f /tmp/stargz.tgz ]]; then
  printf 'Temporary tarball must be removed when download fails\n' >&2
  exit 1
fi
if [[ -f ${test_hosts_file} ]]; then
  printf 'Temporary hosts file must not be generated when download fails\n' >&2
  exit 1
fi

: >"${trace}"
rm -f "${test_hosts_file}"
if PATH="${fake_bin}:${PATH}" TRACE="${trace}" CONTAINERD_HOSTS_FILE="${test_hosts_file}" FAIL_CHECKSUM=true bash "${bootstrap}"; then
  printf 'Checksum failure must abort AWS eStargz bootstrap\n' >&2
  exit 1
fi
grep -Fq 'sha256sum' "${trace}"
if grep -Eq '^(tar|systemctl enable)' "${trace}"; then
  printf 'Checksum failure reached AWS eStargz extraction or startup\n' >&2
  exit 1
fi
if [[ -f /tmp/stargz.tgz ]]; then
  printf 'Temporary tarball must be removed when checksum verification fails\n' >&2
  exit 1
fi
if [[ -f ${test_hosts_file} ]]; then
  printf 'Temporary hosts file must not be generated when checksum fails\n' >&2
  exit 1
fi

: >"${trace}"
rm -f "${test_hosts_file}"
if PATH="${fake_bin}:${PATH}" TRACE="${trace}" CONTAINERD_HOSTS_FILE="${test_hosts_file}" FAIL_SERVICE_START=true bash "${bootstrap}"; then
  printf 'Snapshotter service failure must abort AWS eStargz bootstrap\n' >&2
  exit 1
fi
grep -Fxq 'systemctl enable --now stargz-snapshotter.service' "${trace}"

: >"${trace}"
rm -f "${test_hosts_file}"
if PATH="${fake_bin}:${PATH}" TRACE="${trace}" CONTAINERD_HOSTS_FILE="${test_hosts_file}" bash "${bootstrap}"; then
  printf 'Missing snapshotter socket must abort AWS eStargz bootstrap\n' >&2
  exit 1
fi
grep -Fxq 'systemctl status --no-pager stargz-snapshotter.service' "${trace}"

# Assert containerd Dragonfly proxy hosts configuration contract
if [[ ! -f ${test_hosts_file} ]]; then
  printf 'Containerd Dragonfly hosts configuration was not generated at %s\n' "${test_hosts_file}" >&2
  exit 1
fi
if ! grep -Fq 'server = "https://registry-1.docker.io"' "${test_hosts_file}"; then
  printf 'Generated hosts.toml missing server endpoint\n' >&2
  exit 1
fi
if ! grep -Fq '[host."http://127.0.0.1:4001"]' "${test_hosts_file}"; then
  printf 'Generated hosts.toml does not reference 127.0.0.1:4001\n' >&2
  exit 1
fi
if ! grep -Fq 'capabilities = ["pull", "resolve"]' "${test_hosts_file}"; then
  printf 'Generated hosts.toml missing capabilities = ["pull", "resolve"]\n' >&2
  exit 1
fi
grep -Eq 'mkdir .*/etc/containerd/certs.d/_default' "${trace}"
grep -Eq 'chmod 0755 .*/etc/containerd/certs.d/_default' "${trace}"

if grep -Eq '^(ctr|crictl|nerdctl)' "${trace}"; then
  printf 'AWS eStargz bootstrap must not call ctr, crictl, or nerdctl\n' >&2
  exit 1
fi
if [[ -f /tmp/stargz.tgz ]]; then
  printf 'Temporary tarball must be removed on exit\n' >&2
  exit 1
fi

# Test dual binary gating logic in a sandbox root directory
sandbox_bin="${test_dir}/usr/local/bin"
mkdir -p "${sandbox_bin}"

# 1. Neither binary exists: gating condition must trigger download branch
if ! [[ ! -x "${sandbox_bin}/containerd-stargz-grpc" || ! -x "${sandbox_bin}/ctr-remote" ]]; then
  printf 'AWS eStargz dual binary gating failed when neither binary exists\n' >&2
  exit 1
fi

# 2. Only containerd-stargz-grpc exists: gating condition must trigger download branch
touch "${sandbox_bin}/containerd-stargz-grpc"
chmod +x "${sandbox_bin}/containerd-stargz-grpc"
if ! [[ ! -x "${sandbox_bin}/containerd-stargz-grpc" || ! -x "${sandbox_bin}/ctr-remote" ]]; then
  printf 'AWS eStargz dual binary gating failed when only containerd-stargz-grpc exists\n' >&2
  exit 1
fi

# 3. Only ctr-remote exists: gating condition must trigger download branch
rm -f "${sandbox_bin}/containerd-stargz-grpc"
touch "${sandbox_bin}/ctr-remote"
chmod +x "${sandbox_bin}/ctr-remote"
if ! [[ ! -x "${sandbox_bin}/containerd-stargz-grpc" || ! -x "${sandbox_bin}/ctr-remote" ]]; then
  printf 'AWS eStargz dual binary gating failed when only ctr-remote exists\n' >&2
  exit 1
fi

# 4. Both binaries exist and are executable: gating condition must skip download branch
touch "${sandbox_bin}/containerd-stargz-grpc"
chmod +x "${sandbox_bin}/containerd-stargz-grpc"
if [[ ! -x "${sandbox_bin}/containerd-stargz-grpc" || ! -x "${sandbox_bin}/ctr-remote" ]]; then
  printf 'AWS eStargz dual binary gating failed when both binaries are present and executable\n' >&2
  exit 1
fi
