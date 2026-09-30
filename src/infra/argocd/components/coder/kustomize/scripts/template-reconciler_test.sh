#!/usr/bin/env bash
# Tests Coder template reconciliation, duplicate detection, and error handling.

# shellcheck disable=SC2310
set -euo pipefail

subject="${1:?template reconciler path is required}"
unset VERSION_AFTER_PUBLISH
test_root="$(mktemp -d)"
trap 'rm -rf "${test_root}"' EXIT
mkdir -p "${test_root}/bin" "${test_root}/source" "${test_root}/state" "${test_root}/work"
printf '%s\n' 'registry.invalid/dev@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' >"${test_root}/state/workspace-image"
printf '%s\n' \
  '-----BEGIN CERTIFICATE-----' \
  'Y29udHJvbC1wbGFuZS1jYQ==' \
  '-----END CERTIFICATE-----' >"${test_root}/control-plane-ca.crt"
control_plane_ca_base64="$(base64 <"${test_root}/control-plane-ca.crt" | tr -d '\r\n')"

cat >"${test_root}/source/publish.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "--prepare" ]]; then
  mkdir -p "$2"
  printf '%s\n' 'template payload' >"$2/main.tf"
  exit 0
fi
: "${CODER_TEMPLATE_PUBLISHER_TOKEN:?}"
: "${CODER_TEMPLATE_DEPLOYMENT_DOMAIN:?}"
: "${CODER_TEMPLATE_REPOSITORY_URL:?}"
: "${CODER_TEMPLATE_WORKLOAD_REGISTRY:?}"
: "${CODER_TEMPLATE_WORKLOAD_REGISTRY_INSECURE:?}"
: "${CODER_TEMPLATE_WORKLOAD_ORIGIN_AUTH_MODE:?}"
: "${CODER_TEMPLATE_WORKLOAD_ORIGIN_PROVIDER:?}"
: "${CODER_TEMPLATE_WORKLOAD_ORIGIN_REGION:?}"
: "${CODER_TEMPLATE_WORKSPACE_BACKUP_PROXY_IMAGE:?}"
: "${CODER_TEMPLATE_WORKSPACE_INCARNATION_INVENTORY:?}"
: "${CODER_TEMPLATE_WORKSPACE_NAMESPACE:?}"
: "${CODER_TEMPLATE_WORKSPACE_PLACEMENT_INVENTORY:?}"
: "${CODER_TEMPLATE_WORKSPACE_VIRTUAL_NAME_INVENTORY:?}"
[[ "$CODER_TEMPLATE_CA_CONFIG_MAP" == managed-cluster-ca ]]
[[ "$CODER_TEMPLATE_CELL" == cell-test-1 ]]
[[ "$CODER_TEMPLATE_CONTROL_PLANE_CA_BASE64" == "${EXPECTED_CONTROL_PLANE_CA_BASE64:?}" ]]
[[ "$CODER_TEMPLATE_SERVICE_ACCOUNT" == coder-workspace ]]
[[ "$CODER_TEMPLATE_STORAGE_CLASS" == gp3 ]]
[[ "$CODER_TEMPLATE_TEAM" == examples ]]
[[ "$CODER_TEMPLATE_DEPLOYMENT_DOMAIN" == "${EXPECTED_DEPLOYMENT_DOMAIN:?}" ]]
[[ "$CODER_TEMPLATE_REPOSITORY_URL" == "${EXPECTED_REPOSITORY_URL:?}" ]]
[[ "$CODER_TEMPLATE_WORKLOAD_REGISTRY" == "${EXPECTED_WORKLOAD_REGISTRY:?}" ]]
[[ "$CODER_TEMPLATE_WORKLOAD_REGISTRY_INSECURE" == "${EXPECTED_WORKLOAD_REGISTRY_INSECURE:?}" ]]
[[ "$CODER_TEMPLATE_WORKLOAD_ORIGIN_AUTH_MODE" == web-identity ]]
[[ "$CODER_TEMPLATE_WORKLOAD_ORIGIN_PROVIDER" == aws ]]
[[ "$CODER_TEMPLATE_WORKLOAD_ORIGIN_REGION" == us-west-2 ]]
[[ "$CODER_TEMPLATE_WORKLOAD_ORIGIN_ROLE_ARN" == arn:aws:iam::999988887777:role/examples-workspace-origin-writer ]]
[[ "$CODER_TEMPLATE_WORKLOAD_ORIGIN_TOKEN_AUDIENCE" == sts.amazonaws.com ]]
[[ "$CODER_TEMPLATE_WORKLOAD_ORIGIN_TOKEN_FILE" == /var/run/secrets/workload-origin/token ]]
[[ "$CODER_TEMPLATE_WORKSPACE_BACKUP_PROXY_IMAGE" == "${EXPECTED_WORKSPACE_BACKUP_PROXY_IMAGE:?}" ]]
[[ "$CODER_TEMPLATE_WORKSPACE_INCARNATION_INVENTORY" == "${EXPECTED_WORKSPACE_INCARNATION_INVENTORY:?}" ]]
[[ "$CODER_TEMPLATE_WORKSPACE_NAMESPACE" == "${EXPECTED_WORKSPACE_NAMESPACE:?}" ]]
[[ "$CODER_TEMPLATE_WORKSPACE_PLACEMENT_INVENTORY" == "${EXPECTED_WORKSPACE_PLACEMENT_INVENTORY:?}" ]]
[[ "$CODER_TEMPLATE_WORKSPACE_VIRTUAL_NAME_INVENTORY" == "${EXPECTED_WORKSPACE_VIRTUAL_NAME_INVENTORY:?}" ]]
printf 'publish %s\n' "$*" >>"$CALL_LOG"
[[ "${PUBLISH_FAIL:-false}" == false ]]
EOF
chmod +x "${test_root}/source/publish.sh"

cat >"${test_root}/bin/coder" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
: "${CODER_SESSION_TOKEN:?}"
[[ "$CODER_SESSION_TOKEN" == "${EXPECTED_SESSION_TOKEN:?}" ]]
printf 'coder %s\n' "$*" >>"$CALL_LOG"
if [[ "$*" == 'templates list --output json' ]]; then
  if [[ "${TEMPLATE_LIST_FAIL:-false}" == true ]]; then
    exit 1
  fi
  printf '%s\n' "${TEMPLATE_LIST:-[]}"
elif [[ "$*" == 'templates versions list dev --column name,status --output table' ]]; then
  if [[ "${VERSION_LIST_FAIL:-false}" == true ]]; then
    exit 1
  fi
  if [[ "${VERSION_AFTER_PUBLISH:-false}" == true ]] && grep -q '^publish ' "$CALL_LOG"; then
    version_name="$(sed -n 's/^publish .*--name \([^ ]*\).*/\1/p' "$CALL_LOG")"
    printf '%s succeeded\n' "$version_name"
  else
    printf '%s\n' "${VERSION_LIST:-}"
  fi
elif [[ "$*" == templates\ versions\ promote* ]]; then
  [[ "${PROMOTE_FAIL:-false}" == false ]]
elif [[ "$*" == 'list --all --search template:dev --output json' ]]; then
  printf '%s\n' "${WORKSPACE_LIST:-[]}"
fi
EOF
chmod +x "${test_root}/bin/coder"

default_workspace_placement_inventory='{"cell-test-1":{"cpu":{"default":10,"max":16,"min":1},"gpu_offers":{"a10g-on-demand":{"capacity_type":"on-demand","max_count":8,"model":"a10g","workspace_max":{"cpu":16,"memory_gib":64}}},"memory_gib":{"default":32,"max":64,"min":1},"storage_gib":{"default":256,"max":1024,"min":16}}}'
default_workspace_incarnation_inventory='{"cell-test-1":"aaaaaaaaaaaa"}'
default_workspace_virtual_name_inventory='{"cell-test-1":"test-1"}'

run_reconciler() {
  local deployment_domain="${TEST_DEPLOYMENT_DOMAIN:-example.com}"
  local repository_url="${TEST_REPOSITORY_URL:-git://172.19.255.21:9418/cluster-config.git}"
  local workspace_backup_proxy_image="${TEST_WORKSPACE_BACKUP_PROXY_IMAGE:-registry.invalid/cluster/workspace-backup-proxy@sha256:8a37fbafb559d495b7b07d38f0365d247e32d82bd34bcf1e907b5611ddf0b5c1}"
  local workspace_incarnation_inventory="${TEST_WORKSPACE_INCARNATION_INVENTORY:-${default_workspace_incarnation_inventory}}"
  local workspace_namespace="${TEST_WORKSPACE_NAMESPACE:-team-examples-workspaces}"
  local workspace_placement_inventory="${TEST_WORKSPACE_PLACEMENT_INVENTORY:-${default_workspace_placement_inventory}}"
  local workspace_virtual_name_inventory="${TEST_WORKSPACE_VIRTUAL_NAME_INVENTORY:-${default_workspace_virtual_name_inventory}}"
  if [[ ${OMIT_TOKEN_FILE:-false} == false ]]; then
    printf %s 'secret-token' >"${test_root}/state/coder-session-token"
  fi
  CALL_LOG="${test_root}/calls" \
    CODER_URL=http://coder.invalid \
    CODER_SESSION_TOKEN_FILE="${test_root}/state/coder-session-token" \
    CODER_TEMPLATE_ARCH=arm64 \
    CODER_TEMPLATE_CA_CONFIG_MAP=managed-cluster-ca \
    CODER_TEMPLATE_CELL="${TEST_CELL-cell-test-1}" \
    CODER_TEMPLATE_CONTROL_PLANE_CA_FILE="${TEST_CONTROL_PLANE_CA_FILE:-${test_root}/control-plane-ca.crt}" \
    CODER_TEMPLATE_DEPLOYMENT_DOMAIN="${deployment_domain}" \
    CODER_TEMPLATE_REPOSITORY_URL="${repository_url}" \
    CODER_TEMPLATE_SERVICE_ACCOUNT=coder-workspace \
    CODER_TEMPLATE_STORAGE_CLASS=gp3 \
    CODER_TEMPLATE_TEAM=examples \
    CODER_TEMPLATE_WORKLOAD_REGISTRY=999988887777.dkr.ecr.us-west-2.amazonaws.com \
    CODER_TEMPLATE_WORKLOAD_REGISTRY_INSECURE=false \
    CODER_TEMPLATE_WORKLOAD_ORIGIN_AUTH_MODE=web-identity \
    CODER_TEMPLATE_WORKLOAD_ORIGIN_PROVIDER=aws \
    CODER_TEMPLATE_WORKLOAD_ORIGIN_REGION=us-west-2 \
    CODER_TEMPLATE_WORKLOAD_ORIGIN_ROLE_ARN=arn:aws:iam::999988887777:role/examples-workspace-origin-writer \
    CODER_TEMPLATE_WORKLOAD_ORIGIN_TOKEN_AUDIENCE=sts.amazonaws.com \
    CODER_TEMPLATE_WORKLOAD_ORIGIN_TOKEN_FILE=/var/run/secrets/workload-origin/token \
    CODER_TEMPLATE_WORKSPACE_BACKUP_PROXY_IMAGE="${workspace_backup_proxy_image}" \
    CODER_TEMPLATE_WORKSPACE_INCARNATION_INVENTORY="${workspace_incarnation_inventory}" \
    CODER_TEMPLATE_WORKSPACE_NAMESPACE="${workspace_namespace}" \
    CODER_TEMPLATE_WORKSPACE_PLACEMENT_INVENTORY="${workspace_placement_inventory}" \
    CODER_TEMPLATE_WORKSPACE_VIRTUAL_NAME_INVENTORY="${workspace_virtual_name_inventory}" \
    EXPECTED_REPOSITORY_URL="${repository_url}" \
    EXPECTED_CONTROL_PLANE_CA_BASE64="${control_plane_ca_base64}" \
    EXPECTED_DEPLOYMENT_DOMAIN="${deployment_domain}" \
    EXPECTED_SESSION_TOKEN=secret-token \
    EXPECTED_WORKLOAD_REGISTRY=999988887777.dkr.ecr.us-west-2.amazonaws.com \
    EXPECTED_WORKLOAD_REGISTRY_INSECURE=false \
    EXPECTED_WORKSPACE_BACKUP_PROXY_IMAGE="${workspace_backup_proxy_image}" \
    EXPECTED_WORKSPACE_INCARNATION_INVENTORY="${workspace_incarnation_inventory}" \
    EXPECTED_WORKSPACE_NAMESPACE="${workspace_namespace}" \
    EXPECTED_WORKSPACE_PLACEMENT_INVENTORY="${workspace_placement_inventory}" \
    EXPECTED_WORKSPACE_VIRTUAL_NAME_INVENTORY="${workspace_virtual_name_inventory}" \
    CODER_TEMPLATE_ACCESS_ALIAS_DOMAIN=k8s.example.invalid \
    CODER_TEMPLATE_HEADSCALE_URL=https://headscale.example.invalid \
    HOSTNAME=template-reconciler-test \
    STATE_DIR="${test_root}/state" \
    TEMPLATE_DIR="${test_root}/source" \
    WORK_DIR="${test_root}/work" \
    PATH="${test_root}/bin:${PATH}" \
    bash "${subject}"
}

: >"${test_root}/calls"
TEMPLATE_LIST='[{"name": "developer"}]' run_reconciler
if [[ -e "${test_root}/state/coder-session-token" ]]; then
  printf '%s\n' 'token was not deleted without PRESERVE_CODER_SESSION_TOKEN' >&2
  exit 1
fi

printf '%s\n' 'secret-token' >"${test_root}/state/coder-session-token"
: >"${test_root}/calls"
PRESERVE_CODER_SESSION_TOKEN=true TEMPLATE_LIST='[{"name": "developer"}]' run_reconciler
if [[ ! -e "${test_root}/state/coder-session-token" ]]; then
  printf '%s\n' 'token was deleted despite PRESERVE_CODER_SESSION_TOKEN' >&2
  exit 1
fi
rm -f "${test_root}/state/coder-session-token"
grep -Fxq 'coder templates list --output json' "${test_root}/calls"
if grep -Fq 'coder templates versions list dev --column name,status --output table' "${test_root}/calls"; then
  printf '%s\n' 'absent template was queried by name' >&2
  exit 1
fi
grep -Eq '^publish .*--name [0-9a-f]{64}( |$)' "${test_root}/calls"
grep -Eq '^coder templates versions promote --template dev --template-version [0-9a-f]{64}$' "${test_root}/calls"
grep -q '^coder update ' "${test_root}/calls" && exit 1
grep -Fxq 'repository_url=git://172.19.255.21:9418/cluster-config.git' "${test_root}/work/target-inputs"
grep -Fxq 'ca_config_map_name=managed-cluster-ca' "${test_root}/work/target-inputs"
grep -Fxq 'cell=cell-test-1' "${test_root}/work/target-inputs"
grep -Fxq "control_plane_ca_base64=${control_plane_ca_base64}" "${test_root}/work/target-inputs"
grep -Fxq 'deployment_domain=example.com' "${test_root}/work/target-inputs"
grep -Fxq 'storage_class_name=gp3' "${test_root}/work/target-inputs"
grep -Fxq 'workload_registry=999988887777.dkr.ecr.us-west-2.amazonaws.com' "${test_root}/work/target-inputs"
grep -Fxq 'workload_registry_insecure=false' "${test_root}/work/target-inputs"
grep -Fxq 'workload_origin_auth_mode=web-identity' "${test_root}/work/target-inputs"
grep -Fxq 'workload_origin_provider=aws' "${test_root}/work/target-inputs"
grep -Fxq 'workload_origin_region=us-west-2' "${test_root}/work/target-inputs"
grep -Fxq 'workload_origin_role_arn=arn:aws:iam::999988887777:role/examples-workspace-origin-writer' "${test_root}/work/target-inputs"
grep -Fxq 'workload_origin_token_audience=sts.amazonaws.com' "${test_root}/work/target-inputs"
grep -Fxq 'workload_origin_token_file=/var/run/secrets/workload-origin/token' "${test_root}/work/target-inputs"
grep -Fxq 'workspace_backup_proxy_image=registry.invalid/cluster/workspace-backup-proxy@sha256:8a37fbafb559d495b7b07d38f0365d247e32d82bd34bcf1e907b5611ddf0b5c1' "${test_root}/work/target-inputs"
grep -Fxq 'workspace_incarnation_inventory={"cell-test-1":"aaaaaaaaaaaa"}' "${test_root}/work/target-inputs"
grep -Fxq 'workspace_namespace=team-examples-workspaces' "${test_root}/work/target-inputs"
grep -Fxq 'workspace_placement_inventory={"cell-test-1":{"cpu":{"default":10,"max":16,"min":1},"gpu_offers":{"a10g-on-demand":{"capacity_type":"on-demand","max_count":8,"model":"a10g","workspace_max":{"cpu":16,"memory_gib":64}}},"memory_gib":{"default":32,"max":64,"min":1},"storage_gib":{"default":256,"max":1024,"min":16}}}' "${test_root}/work/target-inputs"
grep -Fxq 'workspace_virtual_name_inventory={"cell-test-1":"test-1"}' "${test_root}/work/target-inputs"

version_name="$(sed -n 's/^publish .*--name \([^ ]*\).*/\1/p' "${test_root}/calls")"
: >"${test_root}/calls"
TEMPLATE_LIST='[{"name": "dev"}]' VERSION_LIST="${version_name} succeeded" run_reconciler
grep -q '^publish ' "${test_root}/calls" && exit 1
grep -Fxq "coder templates versions promote --template dev --template-version ${version_name}" "${test_root}/calls"

: >"${test_root}/calls"
TEMPLATE_LIST='[{"name": "dev"}]' VERSION_LIST="${version_name} failed" run_reconciler
retry_version_name="$(sed -n 's/^publish .*--name \([^ ]*\).*/\1/p' "${test_root}/calls")"
[[ ${retry_version_name} =~ ^[0-9a-f]{48}-retry-[0-9a-f]{8}$ ]]
[[ ${retry_version_name} != "${version_name}" ]]
grep -Fxq "coder templates versions promote --template dev --template-version ${retry_version_name}" "${test_root}/calls"

: >"${test_root}/calls"
TEST_WORKSPACE_NAMESPACE=team-examples2-dev run_reconciler
namespace_version_name="$(sed -n 's/^publish .*--name \([^ ]*\).*/\1/p' "${test_root}/calls")"
[[ -n ${namespace_version_name} && ${namespace_version_name} != "${version_name}" ]]
grep -Fxq "coder templates versions promote --template dev --template-version ${namespace_version_name}" "${test_root}/calls"

: >"${test_root}/calls"
TEST_REPOSITORY_URL='git://172.19.255.22:9418/cluster-config.git' run_reconciler
repository_version_name="$(sed -n 's/^publish .*--name \([^ ]*\).*/\1/p' "${test_root}/calls")"
[[ -n ${repository_version_name} && ${repository_version_name} != "${version_name}" ]]
grep -Fxq "coder templates versions promote --template dev --template-version ${repository_version_name}" "${test_root}/calls"

: >"${test_root}/calls"
TEST_DEPLOYMENT_DOMAIN=unit.test run_reconciler
local_domain_version_name="$(sed -n 's/^publish .*--name \([^ ]*\).*/\1/p' "${test_root}/calls")"
[[ -n ${local_domain_version_name} && ${local_domain_version_name} != "${version_name}" ]]
grep -Fxq 'deployment_domain=unit.test' "${test_root}/work/target-inputs"
grep -Fxq "coder templates versions promote --template dev --template-version ${local_domain_version_name}" "${test_root}/calls"

: >"${test_root}/calls"
TEST_WORKSPACE_BACKUP_PROXY_IMAGE='registry.invalid/cluster/workspace-backup-proxy@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc' run_reconciler
backup_proxy_version_name="$(sed -n 's/^publish .*--name \([^ ]*\).*/\1/p' "${test_root}/calls")"
[[ -n ${backup_proxy_version_name} && ${backup_proxy_version_name} != "${version_name}" ]]
grep -Fxq "coder templates versions promote --template dev --template-version ${backup_proxy_version_name}" "${test_root}/calls"

: >"${test_root}/calls"
printf '%s\n' 'registry.invalid/dev@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' >"${test_root}/state/workspace-image"
TEMPLATE_LIST='[{"name": "dev"}]' PUBLISH_FAIL=true VERSION_AFTER_PUBLISH=true run_reconciler
changed_version_name="$(sed -n 's/^publish .*--name \([^ ]*\).*/\1/p' "${test_root}/calls")"
[[ ${changed_version_name} != "${version_name}" ]]
grep -Fxq "coder templates versions promote --template dev --template-version ${changed_version_name}" "${test_root}/calls"

: >"${test_root}/calls"
if TEMPLATE_LIST_FAIL=true run_reconciler; then
  printf '%s\n' 'template authentication/API failure was ignored' >&2
  exit 1
fi
grep -q '^publish ' "${test_root}/calls" && exit 1

: >"${test_root}/calls"
if TEMPLATE_LIST='[{"name": "dev"}]' VERSION_LIST_FAIL=true run_reconciler; then
  printf '%s\n' 'template-version list failure was ignored' >&2
  exit 1
fi
grep -q '^publish ' "${test_root}/calls" && exit 1

: >"${test_root}/calls"
if PUBLISH_FAIL=true run_reconciler; then
  printf '%s\n' 'publication failure was ignored' >&2
  exit 1
fi
grep -q '^coder templates versions promote ' "${test_root}/calls" && exit 1

: >"${test_root}/calls"
if PROMOTE_FAIL=true run_reconciler; then
  printf '%s\n' 'promotion failure was ignored' >&2
  exit 1
fi

: >"${test_root}/calls"
if OMIT_TOKEN_FILE=true run_reconciler; then
  printf '%s\n' 'missing scoped token file was ignored' >&2
  exit 1
fi
[[ ! -e "${test_root}/state/coder-session-token" ]]
[[ ! -s "${test_root}/calls" ]]

: >"${test_root}/calls"
if TEST_CONTROL_PLANE_CA_FILE="${test_root}/missing-ca.crt" run_reconciler; then
  printf '%s\n' 'missing control-plane CA file was ignored' >&2
  exit 1
fi
[[ ! -s "${test_root}/calls" ]]

: >"${test_root}/calls"
TEST_CELL="" run_reconciler
if grep -q -e '^publish ' -e 'templates delete' "${test_root}/calls"; then
  printf '%s\n' 'zero-cell run without a dev template published or deleted a template' >&2
  exit 1
fi

: >"${test_root}/calls"
TEMPLATE_LIST='[{"name":"dev"}]' TEST_CELL="" run_reconciler
grep -qx 'coder templates delete dev --yes' "${test_root}/calls"
if grep -q '^publish ' "${test_root}/calls"; then
  printf '%s\n' 'zero-cell run published a template version' >&2
  exit 1
fi

: >"${test_root}/calls"
if TEMPLATE_LIST='[{"name":"dev"}]' WORKSPACE_LIST='[{"name":"in-use"}]' TEST_CELL="" run_reconciler; then
  printf '%s\n' 'dev template was deleted while workspaces still used it' >&2
  exit 1
fi
if grep -q 'templates delete' "${test_root}/calls"; then
  printf '%s\n' 'dev template delete was attempted while workspaces still used it' >&2
  exit 1
fi
