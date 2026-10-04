#!/usr/bin/env bash
# Generates the embedded AWS eStargz bootstrap userData block in EC2NodeClass manifests
# and cells ApplicationSet templates from atomic bootstrap source files.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly script_dir
bootstrap_dir="${script_dir}/bootstrap"
readonly bootstrap_dir

indent() {
  local spaces=$1
  local prefix
  prefix="$(printf '%*s' "${spaces}" '')"
  sed "s/^\(.\)/${prefix}\1/"
}

render_raw_user_data() {
  cat <<'EOF'
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="//"

--//
Content-Type: text/cloud-config; charset="us-ascii"

#cloud-config
write_files:
  - path: /etc/containerd-stargz-grpc/config.toml
    owner: root:root
    permissions: '0644'
    content: |
EOF
  indent 6 <"${bootstrap_dir}/stargz-config.toml"
  cat <<'EOF'
  - path: /etc/systemd/system/stargz-snapshotter.service
    owner: root:root
    permissions: '0644'
    content: |
EOF
  indent 6 <"${bootstrap_dir}/stargz-snapshotter.service"
  cat <<'EOF'
  - path: /etc/systemd/system/containerd.service.d/10-stargz.conf
    owner: root:root
    permissions: '0644'
    content: |
EOF
  indent 6 <"${bootstrap_dir}/containerd-stargz.conf"
  cat <<'EOF'
  - path: /etc/systemd/system/dragonfly-prepull.service
    owner: root:root
    permissions: '0644'
    content: |
EOF
  indent 6 <"${bootstrap_dir}/dragonfly-prepull.service"
  cat <<'EOF'
--//
Content-Type: text/x-shellscript; charset="us-ascii"

EOF
  indent 0 <"${bootstrap_dir}/stargz-bootstrap.sh"
  cat <<'EOF'
--//
Content-Type: text/x-shellscript; charset="us-ascii"

EOF
  indent 0 <"${bootstrap_dir}/setup-dragonfly-prepull.sh"
  cat <<'EOF'
--//
Content-Type: application/node.eks.aws

EOF
  indent 0 <"${bootstrap_dir}/stargz-node-config.yaml"
  cat <<'EOF'
--//--
EOF
}

render_user_data() {
  cat <<'EOF'
  # BEGIN GENERATED AWS ESTARGZ
  userData: |
EOF
  render_raw_user_data | indent 4
  cat <<'EOF'
  # END GENERATED AWS ESTARGZ
EOF
}

render_cells_user_data() {
  cat <<'EOF'
    # BEGIN GENERATED AWS ESTARGZ CELLS
EOF
  render_raw_user_data | awk '
    {
      gsub(/\\/, "\\\\")
      gsub(/"/, "\\\"")
      gsub(/000000000000\.dkr\.ecr\.region\.amazonaws\.com/, "%s")
      s = (s == "" ? "" : s "\\n") $0
    }
    END {
      printf "    {{- $nodeUserData := printf \"%s\\n\" $ecrRegistry $ecrRegistry }}\n", s
    }
  '
  cat <<'EOF'
    # END GENERATED AWS ESTARGZ CELLS
EOF
}

check_only=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --check)
      check_only=true
      shift
      ;;
    *)
      printf 'Unknown argument: %s\n' "$1" >&2
      exit 2
      ;;
  esac
done
readonly check_only

gen_file="$(mktemp)"
cells_gen_file="$(mktemp)"
readonly gen_file cells_gen_file
# shellcheck disable=SC2329 # Invoked by the EXIT trap.
cleanup() {
  rm -f "${gen_file}" "${cells_gen_file}"
}
trap cleanup EXIT

render_user_data >"${gen_file}"
render_cells_user_data >"${cells_gen_file}"

update_file() {
  local target_file=$1
  local content_file=$2
  local start_marker=${3:-"# BEGIN GENERATED AWS ESTARGZ"}
  local end_marker=${4:-"# END GENERATED AWS ESTARGZ"}
  local tmp_file
  tmp_file="$(mktemp)"

  awk -v start="${start_marker}" -v end="${end_marker}" '
    NR == FNR {
      gen = (gen == "" ? "" : gen "\n") $0
      next
    }
    $0 ~ start {
      print gen
      in_gen = 1
      next
    }
    $0 ~ end {
      in_gen = 0
      next
    }
    !in_gen { print }
  ' "${content_file}" "${target_file}" >"${tmp_file}"

  if [[ ${check_only} == true ]]; then
    if ! diff -u "${target_file}" "${tmp_file}"; then
      printf '%s is out of date. Run src/infra/argocd/components/karpenter_aws/generate.sh to regenerate.\n' "${target_file}" >&2
      rm -f "${tmp_file}"
      exit 1
    fi
    rm -f "${tmp_file}"
  else
    mv "${tmp_file}" "${target_file}"
  fi
}

update_file "${script_dir}/kustomize/base/ec2-node-class.yaml" "${gen_file}"
update_file "${script_dir}/kustomize/components/gpu/ec2-node-class.yaml" "${gen_file}"
update_file "${script_dir}/../../apps/cells.yaml" "${cells_gen_file}" "BEGIN GENERATED AWS ESTARGZ CELLS" "END GENERATED AWS ESTARGZ CELLS"
