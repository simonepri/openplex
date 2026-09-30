#!/usr/bin/env bash
# shellcheck disable=SC2016,SC2310,SC2312
# Tests template packaging, variable validation, and Coder CLI publication commands in publish.sh.

set -euo pipefail

subject="${1:?template publisher path is required}"
variables="${2:?template variables path is required}"
test_root="$(mktemp -d)"
trap 'rm -rf "${test_root}"' EXIT
mkdir -p "${test_root}/bin" "${test_root}/payload"
control_plane_ca_base64="$(printf '%s\n' \
  '-----BEGIN CERTIFICATE-----' \
  'Y29udHJvbC1wbGFuZS1jYQ==' \
  '-----END CERTIFICATE-----' \
  | base64 | tr -d '\r\n')"

cat >"${test_root}/bin/coder" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
: "${CODER_SESSION_TOKEN:?}"
printf '%s\n' "$@" >"${PUBLISH_TEST_ARGUMENTS:?}"
EOF
chmod +x "${test_root}/bin/coder"

run_publish() {
  env \
    CODER_TEMPLATE_ARCH=arm64 \
    CODER_TEMPLATE_CA_CONFIG_MAP=cluster-local-ca \
    CODER_TEMPLATE_CELL=cell-eaws-lh1 \
    CODER_TEMPLATE_CONTROL_PLANE_CA_BASE64="${CODER_TEMPLATE_CONTROL_PLANE_CA_BASE64-${control_plane_ca_base64}}" \
    CODER_TEMPLATE_DEPLOYMENT_DOMAIN="${CODER_TEMPLATE_DEPLOYMENT_DOMAIN-unit.test}" \
    CODER_TEMPLATE_IMAGE=registry.invalid/dev@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
    CODER_TEMPLATE_WORKSPACE_BACKUP_PROXY_IMAGE="${CODER_TEMPLATE_WORKSPACE_BACKUP_PROXY_IMAGE-registry.invalid/cluster/workspace-backup-proxy@sha256:8a37fbafb559d495b7b07d38f0365d247e32d82bd34bcf1e907b5611ddf0b5c1}" \
    CODER_TEMPLATE_PUBLISHER_TOKEN=publisher-token \
    CODER_TEMPLATE_REPOSITORY_URL="${CODER_TEMPLATE_REPOSITORY_URL-git://172.19.255.21:9418/cluster-config.git}" \
    CODER_TEMPLATE_SERVICE_ACCOUNT=coder-workspace \
    CODER_TEMPLATE_STORAGE_CLASS=workspace-expandable \
    CODER_TEMPLATE_TEAM=examples \
    CODER_TEMPLATE_WORKLOAD_REGISTRY=origin-registry:5000/000000000000/us-east-1 \
    CODER_TEMPLATE_WORKLOAD_REGISTRY_INSECURE=false \
    CODER_TEMPLATE_WORKLOAD_ORIGIN_AUTH_MODE=floci \
    CODER_TEMPLATE_WORKLOAD_ORIGIN_PROVIDER=floci \
    CODER_TEMPLATE_WORKLOAD_ORIGIN_REGION=us-east-1 \
    CODER_TEMPLATE_WORKLOAD_ORIGIN_ROLE_ARN= \
    CODER_TEMPLATE_WORKLOAD_ORIGIN_TOKEN_AUDIENCE= \
    CODER_TEMPLATE_WORKLOAD_ORIGIN_TOKEN_FILE= \
    CODER_TEMPLATE_WORKSPACE_INCARNATION_INVENTORY='{"cell-eaws-lh1":"abcdef123456"}' \
    CODER_TEMPLATE_WORKSPACE_NAMESPACE=team-examples-workspaces \
    CODER_TEMPLATE_WORKSPACE_PLACEMENT_INVENTORY='{"cell-eaws-lh1":{"cpu":{"default":1,"max":3,"min":1},"gpu_offers":{},"memory_gib":{"default":2,"max":8,"min":1},"storage_gib":{"default":16,"max":32,"min":16}}}' \
    CODER_TEMPLATE_WORKSPACE_VIRTUAL_NAME_INVENTORY='{"cell-eaws-lh1":"eaws-lh1"}' \
    CODER_URL=http://coder.invalid \
    CODER_TEMPLATE_ACCESS_ALIAS_DOMAIN=c.example.invalid \
    CODER_TEMPLATE_HEADSCALE_URL=https://headscale.ctrl-eaws-lh1.c.example.invalid \
    PATH="${test_root}/bin:${PATH}" \
    PUBLISH_TEST_ARGUMENTS="${test_root}/arguments" \
    bash "${subject}" --directory "${test_root}/payload" --name first-install
}

bash "${subject}" --prepare "${test_root}/payload"
cmp -- "$(dirname "${subject}")/container/init/workspace-shell.sh" \
  "${test_root}/payload/container/init/workspace-shell.sh"
while IFS= read -r referenced_file; do
  if [[ ! -f "${test_root}/payload/${referenced_file}" ]]; then
    printf 'template publication omits referenced file: %s\n' "${referenced_file}" >&2
    exit 1
  fi
done < <(
  grep -hoE '\$\{path\.module\}/[A-Za-z0-9._/-]+' "$(dirname "${subject}")"/*.tf \
    | sed 's#^${path.module}/##' \
    | LC_ALL=C sort -u
)
run_publish

awk '
  /^variable "[^"]+" \{$/ {
    name = $2
    gsub(/"/, "", name)
    has_default = 0
    next
  }
  name != "" && /^  default[[:space:]]*=/ { has_default = 1 }
  name != "" && /^}$/ {
    if (!has_default) print name
    name = ""
  }
' "${variables}" | LC_ALL=C sort >"${test_root}/required"
sed -n 's/^variable "\([^"]*\)" {$/\1/p' "${variables}" | LC_ALL=C sort >"${test_root}/declared"

awk '
  previous == "--variable" {
    assignment = $0
    if (assignment ~ /^".*"$/) {
      assignment = substr(assignment, 2, length(assignment) - 2)
    }
    split(assignment, parts, "=")
    print parts[1]
  }
  { previous = $0 }
' "${test_root}/arguments" | LC_ALL=C sort >"${test_root}/supplied"

if [[ -s "${test_root}/supplied" ]] && [[ -n "$(uniq -d "${test_root}/supplied")" ]]; then
  printf '%s\n' 'template publication supplies a variable more than once' >&2
  exit 1
fi
if [[ -n "$(comm -23 "${test_root}/required" "${test_root}/supplied")" ]]; then
  printf '%s\n' 'template publication omits a required variable' >&2
  exit 1
fi
if [[ -n "$(comm -13 "${test_root}/declared" "${test_root}/supplied")" ]]; then
  printf '%s\n' 'template publication supplies an undeclared variable' >&2
  exit 1
fi

grep -Fxq 'access_alias_domain=c.example.invalid' "${test_root}/arguments"
grep -Fxq 'deployment_domain=unit.test' "${test_root}/arguments"
grep -Fxq "control_plane_ca_base64=${control_plane_ca_base64}" "${test_root}/arguments"
grep -Fxq 'repository_url=git://172.19.255.21:9418/cluster-config.git' "${test_root}/arguments"
grep -Fxq 'workload_registry=origin-registry:5000/000000000000/us-east-1' "${test_root}/arguments"
grep -Fxq 'workload_registry_insecure=false' "${test_root}/arguments"
grep -Fxq 'workspace_backup_proxy_image=registry.invalid/cluster/workspace-backup-proxy@sha256:8a37fbafb559d495b7b07d38f0365d247e32d82bd34bcf1e907b5611ddf0b5c1' "${test_root}/arguments"
grep -Fxq 'workspace_namespace=team-examples-workspaces' "${test_root}/arguments"
grep -Fxq '"workspace_incarnation_inventory={""cell-eaws-lh1"":""abcdef123456""}"' "${test_root}/arguments"
grep -Fxq '"workspace_placement_inventory={""cell-eaws-lh1"":{""cpu"":{""default"":1,""max"":3,""min"":1},""gpu_offers"":{},""memory_gib"":{""default"":2,""max"":8,""min"":1},""storage_gib"":{""default"":16,""max"":32,""min"":16}}}"' "${test_root}/arguments"
grep -Fxq '"workspace_virtual_name_inventory={""cell-eaws-lh1"":""eaws-lh1""}"' "${test_root}/arguments"

if CODER_TEMPLATE_REPOSITORY_URL='' run_publish 2>"${test_root}/missing-repository"; then
  printf '%s\n' 'template publication accepted a missing repository URL' >&2
  exit 1
fi
grep -Fq 'CODER_TEMPLATE_REPOSITORY_URL' "${test_root}/missing-repository"

if CODER_TEMPLATE_DEPLOYMENT_DOMAIN='' run_publish 2>"${test_root}/missing-deployment-domain"; then
  printf '%s\n' 'template publication accepted a missing deployment domain' >&2
  exit 1
fi
grep -Fq 'CODER_TEMPLATE_DEPLOYMENT_DOMAIN' "${test_root}/missing-deployment-domain"

if CODER_TEMPLATE_CONTROL_PLANE_CA_BASE64='' run_publish 2>"${test_root}/missing-control-plane-ca"; then
  printf '%s\n' 'template publication accepted a missing control-plane CA' >&2
  exit 1
fi
grep -Fq 'CODER_TEMPLATE_CONTROL_PLANE_CA_BASE64' "${test_root}/missing-control-plane-ca"

if CODER_TEMPLATE_WORKSPACE_BACKUP_PROXY_IMAGE='' run_publish 2>"${test_root}/missing-backup-proxy-image"; then
  printf '%s\n' 'template publication accepted a missing backup proxy image' >&2
  exit 1
fi
grep -Fq 'CODER_TEMPLATE_WORKSPACE_BACKUP_PROXY_IMAGE' "${test_root}/missing-backup-proxy-image"
