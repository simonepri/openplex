#!/usr/bin/env bash
# Reconciles and publishes Coder workspace templates against declared Git revisions.

set -euo pipefail

: "${CODER_URL:?}"
: "${CODER_SESSION_TOKEN_FILE:?}"
: "${CODER_TEMPLATE_ACCESS_ALIAS_DOMAIN:?}"
: "${CODER_TEMPLATE_DEPLOYMENT_DOMAIN:?}"
: "${CODER_TEMPLATE_HEADSCALE_URL:=}"
: "${CODER_TEMPLATE_ARCH:?}"
: "${CODER_TEMPLATE_CA_CONFIG_MAP:=}"
: "${CODER_TEMPLATE_CELL:=}"
: "${CODER_TEMPLATE_CONTROL_PLANE_CA_FILE:?}"
: "${CODER_TEMPLATE_REPOSITORY_URL:?}"
: "${CODER_TEMPLATE_SERVICE_ACCOUNT:?}"
: "${CODER_TEMPLATE_STORAGE_CLASS:?}"
: "${CODER_TEMPLATE_TEAM:?}"
: "${CODER_TEMPLATE_WORKLOAD_REGISTRY:?}"
: "${CODER_TEMPLATE_WORKLOAD_REGISTRY_INSECURE:?}"
: "${CODER_TEMPLATE_WORKLOAD_ORIGIN_AUTH_MODE:?}"
: "${CODER_TEMPLATE_WORKLOAD_ORIGIN_PROVIDER:?}"
: "${CODER_TEMPLATE_WORKLOAD_ORIGIN_REGION:?}"
: "${CODER_TEMPLATE_WORKLOAD_ORIGIN_ROLE_ARN:=}"
: "${CODER_TEMPLATE_WORKLOAD_ORIGIN_TOKEN_AUDIENCE:=}"
: "${CODER_TEMPLATE_WORKLOAD_ORIGIN_TOKEN_FILE:=}"
: "${CODER_TEMPLATE_WORKSPACE_BACKUP_PROXY_IMAGE:?}"
: "${CODER_TEMPLATE_WORKSPACE_INCARNATION_INVENTORY:?}"
: "${CODER_TEMPLATE_WORKSPACE_NAMESPACE:?}"
: "${CODER_TEMPLATE_WORKSPACE_PLACEMENT_INVENTORY:?}"
: "${CODER_TEMPLATE_WORKSPACE_VIRTUAL_NAME_INVENTORY:?}"

if [[ ! -s ${CODER_TEMPLATE_CONTROL_PLANE_CA_FILE} ]]; then
  printf '%s\n' 'control-plane CA file is missing or empty' >&2
  exit 1
fi
if ! grep -Fxq -- '-----BEGIN CERTIFICATE-----' "${CODER_TEMPLATE_CONTROL_PLANE_CA_FILE}" \
  || ! grep -Fxq -- '-----END CERTIFICATE-----' "${CODER_TEMPLATE_CONTROL_PLANE_CA_FILE}"; then
  printf '%s\n' 'control-plane CA file does not contain a PEM certificate' >&2
  exit 1
fi
CODER_TEMPLATE_CONTROL_PLANE_CA_BASE64="$({
  base64 <"${CODER_TEMPLATE_CONTROL_PLANE_CA_FILE}" || exit 1
} | tr -d '\r\n')"
if [[ -z ${CODER_TEMPLATE_CONTROL_PLANE_CA_BASE64} ]]; then
  printf '%s\n' 'control-plane CA encoding is empty' >&2
  exit 1
fi
export CODER_TEMPLATE_CONTROL_PLANE_CA_BASE64

if [[ ! -f ${CODER_SESSION_TOKEN_FILE} ]]; then
  printf '%s\n' 'scoped Coder token file is missing or invalid' >&2
  exit 1
fi
CODER_SESSION_TOKEN="$(<"${CODER_SESSION_TOKEN_FILE}")"
if [[ -z ${PRESERVE_CODER_SESSION_TOKEN:-} ]]; then
  rm -- "${CODER_SESSION_TOKEN_FILE}"
fi
if [[ -z ${CODER_SESSION_TOKEN} || ${CODER_SESSION_TOKEN} == *$'\n'* ]]; then
  printf '%s\n' 'scoped Coder token is empty or invalid' >&2
  exit 1
fi
export CODER_SESSION_TOKEN

# A template version is only valid for the cells baked into it, so with no registered cells the template is removed
# instead of leaving a stale version that offers clusters which do not exist.
if [[ -z ${CODER_TEMPLATE_CELL} || ${CODER_TEMPLATE_WORKSPACE_PLACEMENT_INVENTORY} == "{}" || ${CODER_TEMPLATE_WORKSPACE_PLACEMENT_INVENTORY} == "" ]]; then
  template_list_json="$(coder templates list --output json)"
  template_list_compact="$(printf '%s' "${template_list_json}" | tr -d '[:space:]')"
  if [[ ${template_list_compact} != *'"name":"dev"'* ]]; then
    printf 'No registered cells and no dev template; nothing to reconcile.\n'
    exit 0
  fi
  workspace_list_json="$(coder list --all --search template:dev --output json)"
  workspace_list_compact="$(printf '%s' "${workspace_list_json}" | tr -d '[:space:]')"
  if [[ ${workspace_list_compact} != "[]" ]]; then
    printf '%s\n' 'No registered cells, but workspaces still use the dev template; refusing to delete it.' >&2
    exit 1
  fi
  coder templates delete dev --yes
  exit 0
fi

template_dir="${TEMPLATE_DIR:-/source/repository/src/infra/definitions/workspaces/templates/dev}"
work_dir="${WORK_DIR:-/tmp}"
state_dir="${STATE_DIR:-/state}"
payload_dir="${work_dir}/template"
if [[ -z ${CODER_TEMPLATE_IMAGE:-} ]]; then
  if [[ -s "${state_dir}/workspace-image" ]]; then
    CODER_TEMPLATE_IMAGE="$(<"${state_dir}/workspace-image")"
  fi
fi
if [[ -z ${CODER_TEMPLATE_IMAGE:-} ]]; then
  printf '%s\n' 'CODER_TEMPLATE_IMAGE is empty or unset' >&2
  exit 1
fi
export CODER_TEMPLATE_IMAGE
workspace_arch="${CODER_TEMPLATE_ARCH}"
"${template_dir}/publish.sh" --prepare "${payload_dir}"
cat >"${work_dir}/target-inputs" <<EOF
access_alias_domain=${CODER_TEMPLATE_ACCESS_ALIAS_DOMAIN}
ca_config_map_name=${CODER_TEMPLATE_CA_CONFIG_MAP}
cell=${CODER_TEMPLATE_CELL}
control_plane_ca_base64=${CODER_TEMPLATE_CONTROL_PLANE_CA_BASE64}
deployment_domain=${CODER_TEMPLATE_DEPLOYMENT_DOMAIN}
headscale_url=${CODER_TEMPLATE_HEADSCALE_URL}
repository_url=${CODER_TEMPLATE_REPOSITORY_URL}
storage_class_name=${CODER_TEMPLATE_STORAGE_CLASS}
team=${CODER_TEMPLATE_TEAM}
workload_registry=${CODER_TEMPLATE_WORKLOAD_REGISTRY}
workload_registry_insecure=${CODER_TEMPLATE_WORKLOAD_REGISTRY_INSECURE}
workload_origin_auth_mode=${CODER_TEMPLATE_WORKLOAD_ORIGIN_AUTH_MODE}
workload_origin_provider=${CODER_TEMPLATE_WORKLOAD_ORIGIN_PROVIDER}
workload_origin_region=${CODER_TEMPLATE_WORKLOAD_ORIGIN_REGION}
workload_origin_role_arn=${CODER_TEMPLATE_WORKLOAD_ORIGIN_ROLE_ARN}
workload_origin_token_audience=${CODER_TEMPLATE_WORKLOAD_ORIGIN_TOKEN_AUDIENCE}
workload_origin_token_file=${CODER_TEMPLATE_WORKLOAD_ORIGIN_TOKEN_FILE}
workspace_arch=${workspace_arch}
workspace_backup_proxy_image=${CODER_TEMPLATE_WORKSPACE_BACKUP_PROXY_IMAGE}
workspace_incarnation_inventory=${CODER_TEMPLATE_WORKSPACE_INCARNATION_INVENTORY}
workspace_image=${CODER_TEMPLATE_IMAGE}
workspace_namespace=${CODER_TEMPLATE_WORKSPACE_NAMESPACE}
workspace_placement_inventory=${CODER_TEMPLATE_WORKSPACE_PLACEMENT_INVENTORY}
workspace_virtual_name_inventory=${CODER_TEMPLATE_WORKSPACE_VIRTUAL_NAME_INVENTORY}
workspace_service_account=${CODER_TEMPLATE_SERVICE_ACCOUNT}
EOF
source_hash="$({
  cd "${payload_dir}"
  find . -type f -print0 \
    | LC_ALL=C sort -z \
    | xargs -0 sha256sum
  sha256sum "${work_dir}/target-inputs"
} | sha256sum | cut -d ' ' -f 1)"
version_message="gitops:${source_hash:0:12}"
version_name="${source_hash}"

template_names="$(coder templates list --output json | tr -d '[:space:]')"
version_status=
if [[ ${template_names} == *'"name":"dev"'* ]]; then
  version_status="$(
    coder templates versions list dev --column name,status --output table \
      | awk -v name="${version_name}" '$1 == name { print tolower($2); exit }'
  )"
fi
if [[ -n ${version_status} && ${version_status} != succeeded ]]; then
  : "${HOSTNAME:?}"
  attempt_hash="$(printf %s "${HOSTNAME}" | sha256sum | cut -d ' ' -f 1)"
  version_name="${source_hash:0:48}-retry-${attempt_hash:0:8}"
  version_status=
fi
if [[ -z ${version_status} ]]; then
  if ! CODER_TEMPLATE_PUBLISHER_TOKEN="${CODER_SESSION_TOKEN}" \
    CODER_TEMPLATE_CELL="${CODER_TEMPLATE_CELL}" \
    CODER_TEMPLATE_STORAGE_CLASS="${CODER_TEMPLATE_STORAGE_CLASS}" \
    CODER_TEMPLATE_WORKSPACE_BACKUP_PROXY_IMAGE="${CODER_TEMPLATE_WORKSPACE_BACKUP_PROXY_IMAGE}" \
    CODER_TEMPLATE_TEAM="${CODER_TEMPLATE_TEAM}" \
    CODER_TEMPLATE_ARCH="${workspace_arch}" \
    CODER_TEMPLATE_CA_CONFIG_MAP="${CODER_TEMPLATE_CA_CONFIG_MAP}" \
    CODER_TEMPLATE_CONTROL_PLANE_CA_BASE64="${CODER_TEMPLATE_CONTROL_PLANE_CA_BASE64}" \
    CODER_TEMPLATE_DEPLOYMENT_DOMAIN="${CODER_TEMPLATE_DEPLOYMENT_DOMAIN}" \
    CODER_TEMPLATE_REPOSITORY_URL="${CODER_TEMPLATE_REPOSITORY_URL}" \
    CODER_TEMPLATE_SERVICE_ACCOUNT="${CODER_TEMPLATE_SERVICE_ACCOUNT}" \
    CODER_TEMPLATE_WORKLOAD_REGISTRY="${CODER_TEMPLATE_WORKLOAD_REGISTRY}" \
    CODER_TEMPLATE_WORKLOAD_REGISTRY_INSECURE="${CODER_TEMPLATE_WORKLOAD_REGISTRY_INSECURE}" \
    CODER_TEMPLATE_WORKLOAD_ORIGIN_AUTH_MODE="${CODER_TEMPLATE_WORKLOAD_ORIGIN_AUTH_MODE}" \
    CODER_TEMPLATE_WORKLOAD_ORIGIN_PROVIDER="${CODER_TEMPLATE_WORKLOAD_ORIGIN_PROVIDER}" \
    CODER_TEMPLATE_WORKLOAD_ORIGIN_REGION="${CODER_TEMPLATE_WORKLOAD_ORIGIN_REGION}" \
    CODER_TEMPLATE_WORKLOAD_ORIGIN_ROLE_ARN="${CODER_TEMPLATE_WORKLOAD_ORIGIN_ROLE_ARN}" \
    CODER_TEMPLATE_WORKLOAD_ORIGIN_TOKEN_AUDIENCE="${CODER_TEMPLATE_WORKLOAD_ORIGIN_TOKEN_AUDIENCE}" \
    CODER_TEMPLATE_WORKLOAD_ORIGIN_TOKEN_FILE="${CODER_TEMPLATE_WORKLOAD_ORIGIN_TOKEN_FILE}" \
    CODER_TEMPLATE_WORKSPACE_INCARNATION_INVENTORY="${CODER_TEMPLATE_WORKSPACE_INCARNATION_INVENTORY}" \
    CODER_TEMPLATE_WORKSPACE_NAMESPACE="${CODER_TEMPLATE_WORKSPACE_NAMESPACE}" \
    CODER_TEMPLATE_WORKSPACE_PLACEMENT_INVENTORY="${CODER_TEMPLATE_WORKSPACE_PLACEMENT_INVENTORY}" \
    CODER_TEMPLATE_WORKSPACE_VIRTUAL_NAME_INVENTORY="${CODER_TEMPLATE_WORKSPACE_VIRTUAL_NAME_INVENTORY}" \
    "${template_dir}/publish.sh" --directory "${payload_dir}" --name "${version_name}" --message "${version_message}"; then
    version_status="$(
      coder templates versions list dev --column name,status --output table \
        | awk -v name="${version_name}" '$1 == name { print tolower($2); exit }'
    )"
    if [[ -z ${version_status} ]]; then
      printf 'Coder template publication failed before version %s became visible.\n' \
        "${version_name}" >&2
      exit 1
    fi
    if [[ ${version_status} != succeeded ]]; then
      printf 'Coder template publication left version %s in status %s.\n' \
        "${version_name}" "${version_status}" >&2
      exit 1
    fi
  fi
fi

coder templates versions promote --template dev --template-version "${version_name}"
coder templates edit dev --description 'Dev machine template' --yes
