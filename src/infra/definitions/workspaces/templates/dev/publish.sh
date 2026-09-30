#!/usr/bin/env bash
# Packages and publishes versioned developer workspace templates to the Coder platform API with pinned images.

set -euo pipefail

template_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
template_name=dev
if [[ ${1:-} == "--check" ]]; then
  test -f "${template_dir}/.terraform.lock.hcl"
  test -f "${template_dir}/accelerators.tf"
  test -f "${template_dir}/agent.tf"
  test -f "${template_dir}/apps.tf"
  test -f "${template_dir}/deployment.tf"
  test -f "${template_dir}/identity.tf"
  test -f "${template_dir}/kubernetes_config.tf"
  test -f "${template_dir}/locals.tf"
  test -f "${template_dir}/parameters.tf"
  test -f "${template_dir}/storage.tf"
  test -f "${template_dir}/variables.tf"
  test -f "${template_dir}/writer_inventory.tftest.hcl"
  test -f "${template_dir}/hooks/attest-owner.sh"
  test -f "${template_dir}/hooks/register-workspace-agent.sh"
  test -f "${template_dir}/hooks/wait-for-admission.sh"
  test -f "${template_dir}/hooks/workspace-build-context.sh"
  test -f "${template_dir}/hooks/workspace-writer-inventory.sh"
  test -f "${template_dir}/container/config/workspace.zshrc"
  test -f "${template_dir}/container/config/workspace-snazzy.zsh"
  test -f "${template_dir}/container/config/workspace.zsh_plugins.txt"
  test -f "${template_dir}/container/config/zig-cc.sh"
  test -f "${template_dir}/container/config/workspace-herdr.toml"
  test -f "${template_dir}/container/config/workspace-mise.toml"
  test -f "${template_dir}/container/config/workspace-zellij.kdl"
  test -f "${template_dir}/container/init/workspace-identity.sh"
  test -f "${template_dir}/container/init/workspace-mounts.sh"
  test -f "${template_dir}/container/init/workspace-shell.sh"
  test -f "${template_dir}/container/init/workspace-snapshots.sh"
  test -f "${template_dir}/container/init/workspace-start.sh"
  test -f "${template_dir}/container/sidecars/workspace-ssh.sh"
  test -f "${template_dir}/container/sidecars/workspace-tailnet.sh"
  test -f "${template_dir}/container/apps/code-server.sh"
  test -f "${template_dir}/container/apps/coder-cli.sh"
  test -f "${template_dir}/container/apps/filebrowser.sh"
  test -f "${template_dir}/container/apps/reboot.sh"
  test -f "${template_dir}/container/apps/s3i-cli.sh"
  test -f "${template_dir}/container/apps/workspace-herdr.sh"
  test -f "${template_dir}/container/apps/workspace-zasper.sh"
  test -f "${template_dir}/container/apps/workspace-nohang.sh"
  test -f "${template_dir}/container/apps/workspace_nohang.py"
  test -f "${template_dir}/container/apps/workspace-paseo.sh"
  test -f "${template_dir}/container/apps/paseo_coder_proxy.py"
  test -f "${template_dir}/container/apps/paseo_instructions.py"
  test -f "${template_dir}/container/apps/workspace-zellij.sh"
  printf '%s template publication inputs are present.\n' "${template_name}"
  exit 0
fi

prepare_dir=
publish_dir=
version_arguments=()
while (($# > 0)); do
  case "$1" in
    --prepare)
      prepare_dir="${2:?--prepare requires a directory}"
      shift 2
      ;;
    --directory)
      publish_dir="${2:?--directory requires a directory}"
      shift 2
      ;;
    --message)
      version_arguments+=(--message "${2:?--message requires a value}")
      shift 2
      ;;
    --name)
      version_arguments+=(--name "${2:?--name requires a value}")
      shift 2
      ;;
    *)
      printf 'unknown argument: %s\n' "$1" >&2
      exit 64
      ;;
  esac
done

materialize_template() {
  local destination="$1"
  mkdir -p \
    "${destination}/container/config" \
    "${destination}/container/init" \
    "${destination}/container/sidecars" \
    "${destination}/container/apps" \
    "${destination}/hooks" \
    "${destination}/modules/coder_snapshots/scripts"

  cp \
    "${template_dir}"/.terraform.lock.hcl \
    "${template_dir}"/*.tf \
    "${destination}/"

  cp \
    "${template_dir}"/container/config/* \
    "${destination}/container/config/"

  cp \
    "${template_dir}"/container/init/workspace-identity.sh \
    "${template_dir}"/container/init/workspace-mounts.sh \
    "${template_dir}"/container/init/workspace-shell.sh \
    "${template_dir}"/container/init/workspace-snapshots.sh \
    "${template_dir}"/container/init/workspace-start.sh \
    "${destination}/container/init/"

  cp \
    "${template_dir}"/container/sidecars/workspace-ssh.sh \
    "${template_dir}"/container/sidecars/workspace-tailnet.sh \
    "${destination}/container/sidecars/"

  cp \
    "${template_dir}"/container/apps/code-server.sh \
    "${template_dir}"/container/apps/coder-cli.sh \
    "${template_dir}"/container/apps/filebrowser.sh \
    "${template_dir}/container/apps/reboot.sh" \
    "${template_dir}/container/apps/s3i-cli.sh" \
    "${template_dir}/container/apps/workspace-herdr.sh" \
    "${template_dir}"/container/apps/workspace-zasper.sh \
    "${template_dir}"/container/apps/workspace-nohang.sh \
    "${template_dir}"/container/apps/workspace_nohang.py \
    "${template_dir}"/container/apps/workspace-paseo.sh \
    "${template_dir}"/container/apps/paseo_coder_proxy.py \
    "${template_dir}"/container/apps/paseo_instructions.py \
    "${template_dir}"/container/apps/workspace-zellij.sh \
    "${destination}/container/apps/"

  cp \
    "${template_dir}"/hooks/attest-owner.sh \
    "${template_dir}"/hooks/register-workspace-agent.sh \
    "${template_dir}"/hooks/wait-for-admission.sh \
    "${template_dir}"/hooks/workspace-build-context.sh \
    "${template_dir}"/hooks/workspace-writer-inventory.sh \
    "${destination}/hooks/"

  cp \
    "${template_dir}"/../../modules/coder_snapshots/*.tf \
    "${destination}/modules/coder_snapshots/"

  cp \
    "${template_dir}"/../../modules/coder_snapshots/scripts/*.sh \
    "${destination}/modules/coder_snapshots/scripts/"

  sed -i.bak 's|source = "../../modules/coder_snapshots"|source = "./modules/coder_snapshots"|g' "${destination}/storage.tf" && rm -f "${destination}/storage.tf.bak"
}

if [[ -n ${prepare_dir} ]]; then
  [[ -z ${publish_dir} && ${#version_arguments[@]} -eq 0 ]] || {
    printf '%s\n' '--prepare cannot publish' >&2
    exit 64
  }
  materialize_template "${prepare_dir}"
  exit 0
fi

: "${CODER_URL:?set CODER_URL to the team Coder endpoint}"
: "${CODER_TEMPLATE_PUBLISHER_TOKEN:?set CODER_TEMPLATE_PUBLISHER_TOKEN to the dedicated Coder automation-user token}"
: "${CODER_TEMPLATE_IMAGE:?set CODER_TEMPLATE_IMAGE to the digest-pinned image}"
: "${CODER_TEMPLATE_WORKSPACE_BACKUP_PROXY_IMAGE:?set CODER_TEMPLATE_WORKSPACE_BACKUP_PROXY_IMAGE to the digest-pinned backup proxy image}"
: "${CODER_TEMPLATE_CELL:?set CODER_TEMPLATE_CELL to the target cell}"
: "${CODER_TEMPLATE_STORAGE_CLASS:?set CODER_TEMPLATE_STORAGE_CLASS to the RWO home class}"
: "${CODER_TEMPLATE_TEAM:?set CODER_TEMPLATE_TEAM to the owning team}"
: "${CODER_TEMPLATE_WORKLOAD_REGISTRY:?set CODER_TEMPLATE_WORKLOAD_REGISTRY to the workload OCI origin root}"
: "${CODER_TEMPLATE_WORKLOAD_REGISTRY_INSECURE:?set CODER_TEMPLATE_WORKLOAD_REGISTRY_INSECURE to false or true}"
: "${CODER_TEMPLATE_WORKLOAD_ORIGIN_AUTH_MODE:?set CODER_TEMPLATE_WORKLOAD_ORIGIN_AUTH_MODE to the cell-owned credential mode}"
: "${CODER_TEMPLATE_WORKLOAD_ORIGIN_PROVIDER:?set CODER_TEMPLATE_WORKLOAD_ORIGIN_PROVIDER to the cell provider}"
: "${CODER_TEMPLATE_WORKLOAD_ORIGIN_REGION:?set CODER_TEMPLATE_WORKLOAD_ORIGIN_REGION to the origin AWS region}"
: "${CODER_TEMPLATE_WORKLOAD_ORIGIN_ROLE_ARN:=}"
: "${CODER_TEMPLATE_WORKLOAD_ORIGIN_TOKEN_AUDIENCE:=}"
: "${CODER_TEMPLATE_WORKLOAD_ORIGIN_TOKEN_FILE:=}"
: "${CODER_TEMPLATE_ARCH:?set CODER_TEMPLATE_ARCH to amd64 or arm64}"
: "${CODER_TEMPLATE_CA_CONFIG_MAP:=}"
: "${CODER_TEMPLATE_CONTROL_PLANE_CA_BASE64:?set CODER_TEMPLATE_CONTROL_PLANE_CA_BASE64 to the base64-encoded control-plane CA}"
: "${CODER_TEMPLATE_REPOSITORY_URL:?set CODER_TEMPLATE_REPOSITORY_URL to the administrator-owned Git repository}"
: "${CODER_TEMPLATE_SERVICE_ACCOUNT:?set CODER_TEMPLATE_SERVICE_ACCOUNT to the team service account}"
: "${CODER_TEMPLATE_WORKSPACE_NAMESPACE:?set CODER_TEMPLATE_WORKSPACE_NAMESPACE to the team workspaces namespace}"
: "${CODER_TEMPLATE_WORKSPACE_INCARNATION_INVENTORY:?set CODER_TEMPLATE_WORKSPACE_INCARNATION_INVENTORY to the canonical cluster incarnation JSON}"
: "${CODER_TEMPLATE_WORKSPACE_PLACEMENT_INVENTORY:?set CODER_TEMPLATE_WORKSPACE_PLACEMENT_INVENTORY to the canonical cluster resource JSON}"
: "${CODER_TEMPLATE_WORKSPACE_VIRTUAL_NAME_INVENTORY:?set CODER_TEMPLATE_WORKSPACE_VIRTUAL_NAME_INVENTORY to the canonical cluster virtual-name JSON}"
: "${CODER_TEMPLATE_ACCESS_ALIAS_DOMAIN:?set CODER_TEMPLATE_ACCESS_ALIAS_DOMAIN to the k8s access domain}"
: "${CODER_TEMPLATE_DEPLOYMENT_DOMAIN:?set CODER_TEMPLATE_DEPLOYMENT_DOMAIN to the public deployment domain}"
: "${CODER_TEMPLATE_HEADSCALE_URL:?set CODER_TEMPLATE_HEADSCALE_URL to the canonical Headscale HTTPS URL}"

tmp_dir=
if [[ -z ${publish_dir} ]]; then
  tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/coder-template.XXXXXX")"
  trap 'rm -rf "$tmp_dir"' EXIT
  publish_dir="${tmp_dir}"
  materialize_template "${publish_dir}"
fi
export CODER_SESSION_TOKEN="${CODER_TEMPLATE_PUBLISHER_TOKEN}"
workspace_incarnation_inventory_csv="${CODER_TEMPLATE_WORKSPACE_INCARNATION_INVENTORY//\"/\"\"}"
workspace_placement_inventory_csv="${CODER_TEMPLATE_WORKSPACE_PLACEMENT_INVENTORY//\"/\"\"}"
workspace_virtual_name_inventory_csv="${CODER_TEMPLATE_WORKSPACE_VIRTUAL_NAME_INVENTORY//\"/\"\"}"
coder templates push "${template_name}" \
  --directory "${publish_dir}" \
  --variable "access_alias_domain=${CODER_TEMPLATE_ACCESS_ALIAS_DOMAIN}" \
  --variable "deployment_domain=${CODER_TEMPLATE_DEPLOYMENT_DOMAIN}" \
  --variable "workspace_image=${CODER_TEMPLATE_IMAGE}" \
  --variable "workspace_backup_proxy_image=${CODER_TEMPLATE_WORKSPACE_BACKUP_PROXY_IMAGE}" \
  --variable "ca_config_map_name=${CODER_TEMPLATE_CA_CONFIG_MAP}" \
  --variable "cell=${CODER_TEMPLATE_CELL}" \
  --variable "control_plane_ca_base64=${CODER_TEMPLATE_CONTROL_PLANE_CA_BASE64}" \
  --variable "headscale_url=${CODER_TEMPLATE_HEADSCALE_URL}" \
  --variable "repository_url=${CODER_TEMPLATE_REPOSITORY_URL}" \
  --variable "storage_class_name=${CODER_TEMPLATE_STORAGE_CLASS}" \
  --variable "team=${CODER_TEMPLATE_TEAM}" \
  --variable "workload_registry=${CODER_TEMPLATE_WORKLOAD_REGISTRY}" \
  --variable "workload_registry_insecure=${CODER_TEMPLATE_WORKLOAD_REGISTRY_INSECURE}" \
  --variable "workload_origin_auth_mode=${CODER_TEMPLATE_WORKLOAD_ORIGIN_AUTH_MODE}" \
  --variable "workload_origin_provider=${CODER_TEMPLATE_WORKLOAD_ORIGIN_PROVIDER}" \
  --variable "workload_origin_region=${CODER_TEMPLATE_WORKLOAD_ORIGIN_REGION}" \
  --variable "workload_origin_role_arn=${CODER_TEMPLATE_WORKLOAD_ORIGIN_ROLE_ARN}" \
  --variable "workload_origin_token_audience=${CODER_TEMPLATE_WORKLOAD_ORIGIN_TOKEN_AUDIENCE}" \
  --variable "workload_origin_token_file=${CODER_TEMPLATE_WORKLOAD_ORIGIN_TOKEN_FILE}" \
  --variable "workspace_arch=${CODER_TEMPLATE_ARCH}" \
  --variable "workspace_namespace=${CODER_TEMPLATE_WORKSPACE_NAMESPACE}" \
  --variable "\"workspace_incarnation_inventory=${workspace_incarnation_inventory_csv}\"" \
  --variable "\"workspace_placement_inventory=${workspace_placement_inventory_csv}\"" \
  --variable "\"workspace_virtual_name_inventory=${workspace_virtual_name_inventory_csv}\"" \
  --variable "workspace_service_account=${CODER_TEMPLATE_SERVICE_ACCOUNT}" \
  --provisioner-tag "-" \
  "${version_arguments[@]}" \
  --yes
