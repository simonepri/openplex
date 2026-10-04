#!/usr/bin/env bash
# Tests that AWS EC2NodeClasses receive the pre-kubelet eStargz contract and fail closed before nodeadm.

# shellcheck disable=SC2016,SC2310,SC2312
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
  df_mirror="${script_dir}/dragonfly-mirror.toml"
  df_prepull_service="${script_dir}/dragonfly-prepull.service"
  df_prepull_script="${script_dir}/setup-dragonfly-prepull.sh"
elif (($# == 10)); then
  render=$1
  bootstrap=$2
  config=$3
  unit=$4
  drop_in=$5
  node_config=$6
  yq_bin=$7
  df_mirror=$8
  df_prepull_service=$9
  df_prepull_script=${10}
else
  printf 'usage: %s RENDER BOOTSTRAP CONFIG UNIT DROP_IN NODE_CONFIG YQ DF_MIRROR DF_PREPULL_SERVICE DF_PREPULL_SCRIPT\n' "$0" >&2
  exit 2
fi
readonly render bootstrap config unit drop_in node_config yq_bin df_mirror df_prepull_service df_prepull_script

for required_input in "${render}" "${bootstrap}" "${config}" "${unit}" "${drop_in}" "${node_config}" "${yq_bin}" "${df_mirror}" "${df_prepull_service}" "${df_prepull_script}"; do
  if [[ ! -f ${required_input} ]]; then
    printf 'stargz-bootstrap_test: required input file not found: %s\n' "${required_input}" >&2
    exit 1
  fi
done
if [[ ! -x ${yq_bin} ]]; then
  printf 'stargz-bootstrap_test: required yq binary not executable: %s\n' "${yq_bin}" >&2
  exit 1
fi

readonly cpu_user_data="${test_dir}/cpu-user-data"
readonly gpu_user_data="${test_dir}/gpu-user-data"
readonly cloud_config="${test_dir}/cloud-config.yaml"
readonly rendered_node_config="${test_dir}/node-config.yaml"
readonly fake_bin="${test_dir}/bin"
readonly trace="${test_dir}/trace"

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

for source in "${bootstrap}" "${config}" "${unit}" "${drop_in}" "${node_config}" "${df_mirror}" "${df_prepull_service}" "${df_prepull_script}"; do
  if [[ ! -f ${source} ]]; then
    printf 'stargz-bootstrap_test: required source file not found: %s\n' "${source}" >&2
    exit 1
  fi
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
prepull_shell_line="$(grep -nF 'systemctl start --no-block dragonfly-prepull.service' "${cpu_user_data}" | awk -F: 'NR == 1 { print $1 }')"
readonly prepull_shell_line
if ! ((cloud_config_line < shell_line && shell_line < prepull_shell_line && prepull_shell_line < node_config_line)); then
  printf 'AWS eStargz MIME parts must order cloud-config, bootstrap, prepull, then NodeConfig\n' >&2
  exit 1
fi
[[ "$(awk 'NF { line = $0 } END { print line }' "${cpu_user_data}")" == '--//--' ]]

awk '
  /^#cloud-config$/ { body = 1 }
  body && /^--\/\/$/ { exit }
  body { print }
' "${cpu_user_data}" >"${cloud_config}"
[[ "$(yq -r '.write_files | length' "${cloud_config}")" == 4 ]]
[[ "$(yq -r '.write_files[0].path' "${cloud_config}")" == "/etc/containerd-stargz-grpc/config.toml" ]]
[[ "$(yq -r '.write_files[1].path' "${cloud_config}")" == "/etc/systemd/system/stargz-snapshotter.service" ]]
[[ "$(yq -r '.write_files[2].path' "${cloud_config}")" == "/etc/systemd/system/containerd.service.d/10-stargz.conf" ]]
[[ "$(yq -r '.write_files[3].path' "${cloud_config}")" == "/etc/systemd/system/dragonfly-prepull.service" ]]

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
  'path: /etc/systemd/system/dragonfly-prepull.service' \
  'readonly stargz_version="v0.18.2"' \
  'readonly stargz_sha256="515a3c3af0012f192ace31fb79e910597977c77227e976680aeaaef6e9ae50a9"' \
  'readonly install_dir="${INSTALL_DIR:-/usr/local/bin}"' \
  'readonly tmp_dir="${TMP_DIR:-/tmp}"' \
  'curl -fsSL --retry 5 --retry-delay 2 --retry-connrefused' \
  'echo "${stargz_sha256}  ${stargz_tarball}" | sha256sum -c -' \
  'tar -C "${install_dir}" -xzf "${stargz_tarball}" containerd-stargz-grpc ctr-remote' \
  'enable_keychain = true' \
  'image_service_path = "/run/containerd/containerd.sock"' \
  'snapshotter = "stargz"' \
  'disable_snapshot_annotations = false' \
  '[proxy_plugins.stargz]' \
  '--image-service-endpoint=unix:///run/containerd-stargz-grpc/containerd-stargz-grpc.sock' \
  'failSwapOn: false' \
  'swapBehavior: LimitedSwap' \
  'Wants=stargz-snapshotter.service' \
  'command -v aws' \
  'systemctl enable dragonfly-prepull.service' \
  'systemctl start --no-block dragonfly-prepull.service'; do
  grep -Fq -- "${contract}" "${cpu_user_data}"
done

grep -Fq 'Wants=stargz-snapshotter.service' "${drop_in}"
if grep -Fq 'Requires=stargz-snapshotter.service' "${cpu_user_data}" || grep -Fq 'Requires=stargz-snapshotter.service' "${drop_in}"; then
  printf 'AWS eStargz containerd drop-in must use Wants= instead of Requires=\n' >&2
  exit 1
fi

# Ensure bootstrap script does not invoke container runtime tools
if grep -Eq '(^|[[:space:];|&(])(ctr|crictl|nerdctl)([[:space:];|&)]|$)' "${bootstrap}"; then
  printf 'AWS eStargz bootstrap must not call ctr, crictl, or nerdctl\n' >&2
  exit 1
fi

# ==============================================================================
# Parse generated stargz configuration and verify Dragonfly resolver mirror
# ==============================================================================
stargz_config_raw="$(yq -r '
  .write_files[] | select(.path == "/etc/containerd-stargz-grpc/config.toml") | .content
' "${cloud_config}")"
parsed_stargz_config="${test_dir}/stargz-config.json"
printf '%s\n' "${stargz_config_raw}" | yq -p toml -o json '.' >"${parsed_stargz_config}"

# 1. Mirror is limited to ECR host:
# Resolver host mirror must exist, point to 127.0.0.1:4001, and match ECR domain pattern.
mirror_count="$(yq -r '.resolver.host | length' "${parsed_stargz_config}")"
if [[ ${mirror_count} -lt 1 ]]; then
  printf 'Generated stargz config must declare at least one resolver.host entry\n' >&2
  exit 1
fi

host_keys="$(yq -r '.resolver.host | keys | .[]' "${parsed_stargz_config}")"
for h in ${host_keys}; do
  if ! [[ ${h} =~ ^[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com$ ]]; then
    printf 'Stargz mirror host %s is not an ECR host; mirror must be strictly scoped to ECR\n' "${h}" >&2
    exit 1
  fi
  mirror_host="$(HOST="${h}" yq -r '.resolver.host[strenv(HOST)].mirrors[0].host' "${parsed_stargz_config}")"
  if [[ ${mirror_host} != "127.0.0.1:4001" ]]; then
    printf 'Stargz mirror host %s must route to 127.0.0.1:4001 (got %s)\n' "${h}" "${mirror_host}" >&2
    exit 1
  fi
  insecure="$(HOST="${h}" yq -r '.resolver.host[strenv(HOST)].mirrors[0].insecure' "${parsed_stargz_config}")"
  if [[ ${insecure} != "true" ]]; then
    printf 'Stargz mirror host %s must set insecure = true for local HTTP proxy\n' "${h}" >&2
    exit 1
  fi
done

# 2. No server override, origin fallback preserved:
server_override="$(yq -r '[.resolver.host[].server] | map(select(. != null)) | join(",")' "${parsed_stargz_config}")"
if [[ -n ${server_override} ]]; then
  printf 'Stargz resolver must not set server override (%s); ECR origin fallback must be preserved\n' "${server_override}" >&2
  exit 1
fi

# 3. Docker Hub is untouched:
if yq -e '.resolver.host["docker.io"] or .resolver.host["registry-1.docker.io"] or .resolver.host["_default"]' "${parsed_stargz_config}" >/dev/null 2>&1; then
  printf 'Stargz resolver must not configure mirrors or override for Docker Hub or _default\n' >&2
  exit 1
fi

# Assert old broken hosts.toml and registry-1.docker.io are completely absent
if grep -Fq '/etc/containerd/certs.d' "${cpu_user_data}"; then
  printf 'Containerd certs.d hosts.toml must not be configured in user data\n' >&2
  exit 1
fi
if grep -Fq 'registry-1.docker.io' "${cpu_user_data}"; then
  printf 'registry-1.docker.io must not be configured in user data\n' >&2
  exit 1
fi

# Verify ported dragonfly bootstrap files contract
if [[ ! -f ${df_mirror} ]]; then
  printf 'stargz-bootstrap_test: dragonfly mirror file not found: %s\n' "${df_mirror}" >&2
  exit 1
fi
grep -Fq '127.0.0.1:4001' "${df_mirror}"
grep -Fq 'insecure = true' "${df_mirror}"

if [[ ! -f ${df_prepull_service} ]]; then
  printf 'stargz-bootstrap_test: dragonfly prepull service file not found: %s\n' "${df_prepull_service}" >&2
  exit 1
fi
grep -Fq 'Wants=network-online.target' "${df_prepull_service}"
grep -Fq 'dragonflyoss/client' "${df_prepull_service}"
grep -Fq 'busybox' "${df_prepull_service}"
grep -Fq 'ctr -n k8s.io image pull --snapshotter stargz' "${df_prepull_service}"

if [[ ! -f ${df_prepull_script} ]]; then
  printf 'stargz-bootstrap_test: dragonfly prepull script file not found: %s\n' "${df_prepull_script}" >&2
  exit 1
fi
grep -Fq 'command -v aws' "${df_prepull_script}"
grep -Fq 'systemctl start --no-block dragonfly-prepull.service' "${df_prepull_script}"

# Validate integrated dragonfly prepull unit in user data
prepull_unit_content="$(yq -r '
  .write_files[] | select(.path == "/etc/systemd/system/dragonfly-prepull.service") | .content
' "${cloud_config}")"
[[ -n ${prepull_unit_content} ]]
grep -Fq 'Description=Pre-pull Dragonfly images so dfdaemon is up before the first model pull' <<<"${prepull_unit_content}"
grep -Fq 'Wants=network-online.target' <<<"${prepull_unit_content}"
grep -Fq 'After=network-online.target' <<<"${prepull_unit_content}"
grep -Fq 'Type=oneshot' <<<"${prepull_unit_content}"
grep -Fq 'RemainAfterExit=yes' <<<"${prepull_unit_content}"
grep -Fq 'TimeoutStartSec=600' <<<"${prepull_unit_content}"
grep -Fq 'ctr -n k8s.io image pull --snapshotter stargz' <<<"${prepull_unit_content}"
grep -Fq '000000000000.dkr.ecr.region.amazonaws.com' <<<"${prepull_unit_content}"
grep -Fq '$$REGISTRY/mirror/dragonflyoss/client:v1.5.7' <<<"${prepull_unit_content}"
grep -Fq '$$REGISTRY/mirror/busybox:1.37.0' <<<"${prepull_unit_content}"

if grep -Fq 'systemctl start dragonfly-prepull.service' "${cpu_user_data}" && ! grep -Fq 'systemctl start --no-block dragonfly-prepull.service' "${cpu_user_data}"; then
  printf 'Dragonfly prepull service start must be non-blocking (--no-block)\n' >&2
  exit 1
fi

# Order of execution checks in bootstrap
download_line="$(line_number 'curl -fsSL')"
readonly download_line
checksum_line="$(line_number 'sha256sum')"
readonly checksum_line
extract_line="$(line_number 'tar -C "${install_dir}"')"
readonly extract_line
start_line="$(line_number 'systemctl enable --now stargz-snapshotter.service')"
readonly start_line
if ! ((download_line < checksum_line && checksum_line < extract_line && extract_line < start_line)); then
  printf 'AWS eStargz bootstrap must download, verify checksum, extract, then start service\n' >&2
  exit 1
fi

# ==============================================================================
# Test stargz-bootstrap.sh under a temporary sandbox root (Item 12 overridability)
# ==============================================================================
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
target_dir=""
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "-C" ]]; then
    target_dir="$2"
    shift 2
  else
    shift
  fi
done
if [[ -n "${target_dir}" ]]; then
  touch "${target_dir}/containerd-stargz-grpc" "${target_dir}/ctr-remote"
fi
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

sandbox_bin="${test_dir}/usr/local/bin"
sandbox_tmp="${test_dir}/tmp"
sandbox_root="${test_dir}/var/lib/containerd-stargz-grpc"
sandbox_socket="${test_dir}/run/containerd-stargz-grpc.sock"
mkdir -p "${sandbox_bin}" "${sandbox_tmp}" "$(dirname "${sandbox_socket}")"

# 1. Download failure test
: >"${trace}"
if PATH="${fake_bin}:${PATH}" TRACE="${trace}" \
  INSTALL_DIR="${sandbox_bin}" TMP_DIR="${sandbox_tmp}" STARGZ_ROOT="${sandbox_root}" STARGZ_SOCKET="${sandbox_socket}" \
  FAIL_DOWNLOAD=true bash "${bootstrap}"; then
  printf 'Download failure must abort AWS eStargz bootstrap\n' >&2
  exit 1
fi
grep -Fq 'curl' "${trace}"
if grep -Eq '^(sha256sum|tar|systemctl enable)' "${trace}"; then
  printf 'Download failure reached AWS eStargz verification, extraction, or startup\n' >&2
  exit 1
fi
if [[ -f "${sandbox_tmp}/stargz.tgz" ]]; then
  printf 'Temporary tarball must be removed when download fails\n' >&2
  exit 1
fi

# 2. Checksum failure test
: >"${trace}"
if PATH="${fake_bin}:${PATH}" TRACE="${trace}" \
  INSTALL_DIR="${sandbox_bin}" TMP_DIR="${sandbox_tmp}" STARGZ_ROOT="${sandbox_root}" STARGZ_SOCKET="${sandbox_socket}" \
  FAIL_CHECKSUM=true bash "${bootstrap}"; then
  printf 'Checksum failure must abort AWS eStargz bootstrap\n' >&2
  exit 1
fi
grep -Fq 'sha256sum' "${trace}"
if grep -Eq '^(tar|systemctl enable)' "${trace}"; then
  printf 'Checksum failure reached AWS eStargz extraction or startup\n' >&2
  exit 1
fi
if [[ -f "${sandbox_tmp}/stargz.tgz" ]]; then
  printf 'Temporary tarball must be removed when checksum verification fails\n' >&2
  exit 1
fi

# 3. Service start failure test
: >"${trace}"
if PATH="${fake_bin}:${PATH}" TRACE="${trace}" \
  INSTALL_DIR="${sandbox_bin}" TMP_DIR="${sandbox_tmp}" STARGZ_ROOT="${sandbox_root}" STARGZ_SOCKET="${sandbox_socket}" \
  FAIL_SERVICE_START=true bash "${bootstrap}"; then
  printf 'Snapshotter service failure must abort AWS eStargz bootstrap\n' >&2
  exit 1
fi
grep -Fxq 'systemctl enable --now stargz-snapshotter.service' "${trace}"

# 4. Missing socket failure test
: >"${trace}"
rm -f "${sandbox_socket}"
if PATH="${fake_bin}:${PATH}" TRACE="${trace}" \
  INSTALL_DIR="${sandbox_bin}" TMP_DIR="${sandbox_tmp}" STARGZ_ROOT="${sandbox_root}" STARGZ_SOCKET="${sandbox_socket}" \
  bash "${bootstrap}"; then
  printf 'Missing snapshotter socket must abort AWS eStargz bootstrap\n' >&2
  exit 1
fi
grep -Fxq 'systemctl status --no-pager stargz-snapshotter.service' "${trace}"

# 5. Successful startup test in sandbox root
: >"${trace}"
python3 -c "import socket; s = socket.socket(socket.AF_UNIX); s.bind('${sandbox_socket}')"
if [[ ! -S ${sandbox_socket} ]]; then
  printf 'Failed to create mock unix domain socket at %s\n' "${sandbox_socket}" >&2
  exit 1
fi
if ! PATH="${fake_bin}:${PATH}" TRACE="${trace}" \
  INSTALL_DIR="${sandbox_bin}" TMP_DIR="${sandbox_tmp}" STARGZ_ROOT="${sandbox_root}" STARGZ_SOCKET="${sandbox_socket}" \
  bash "${bootstrap}"; then
  printf 'AWS eStargz bootstrap failed under clean sandbox root\n' >&2
  exit 1
fi
if [[ ! -d ${sandbox_root} ]]; then
  printf 'Stargz data root directory %s was not created\n' "${sandbox_root}" >&2
  exit 1
fi
if [[ -f "${sandbox_tmp}/stargz.tgz" ]]; then
  printf 'Temporary tarball must be removed upon successful bootstrap\n' >&2
  exit 1
fi

# 6. Test dual binary gating logic in a sandbox root directory
test_bin_dir="${test_dir}/sandbox-gate-bin"
mkdir -p "${test_bin_dir}"

# 6.1 Neither binary exists: gating condition must trigger download branch
if ! [[ ! -x "${test_bin_dir}/containerd-stargz-grpc" || ! -x "${test_bin_dir}/ctr-remote" ]]; then
  printf 'AWS eStargz dual binary gating failed when neither binary exists\n' >&2
  exit 1
fi

# 6.2 Only containerd-stargz-grpc exists: gating condition must trigger download branch
touch "${test_bin_dir}/containerd-stargz-grpc"
chmod +x "${test_bin_dir}/containerd-stargz-grpc"
if ! [[ ! -x "${test_bin_dir}/containerd-stargz-grpc" || ! -x "${test_bin_dir}/ctr-remote" ]]; then
  printf 'AWS eStargz dual binary gating failed when only containerd-stargz-grpc exists\n' >&2
  exit 1
fi

# 6.3 Only ctr-remote exists: gating condition must trigger download branch
rm -f "${test_bin_dir}/containerd-stargz-grpc"
touch "${test_bin_dir}/ctr-remote"
chmod +x "${test_bin_dir}/ctr-remote"
if ! [[ ! -x "${test_bin_dir}/containerd-stargz-grpc" || ! -x "${test_bin_dir}/ctr-remote" ]]; then
  printf 'AWS eStargz dual binary gating failed when only ctr-remote exists\n' >&2
  exit 1
fi

# 6.4 Both binaries exist and are executable: gating condition must skip download branch
touch "${test_bin_dir}/containerd-stargz-grpc"
chmod +x "${test_bin_dir}/containerd-stargz-grpc"
if [[ ! -x "${test_bin_dir}/containerd-stargz-grpc" || ! -x "${test_bin_dir}/ctr-remote" ]]; then
  printf 'AWS eStargz dual binary gating failed when both binaries are present and executable\n' >&2
  exit 1
fi
