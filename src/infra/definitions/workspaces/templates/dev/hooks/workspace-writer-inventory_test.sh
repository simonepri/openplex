#!/usr/bin/env bash
# Tests writer inventory querying, active pod status filtering, and volume lock conflict detection workflows.

set -euo pipefail

subject="${1:-$(cd -- "$(dirname -- "$0")" && pwd)/workspace-writer-inventory.sh}"
jq_bin="${2:-$(command -v jq || true)}"
test_dir="$(mktemp -d)"
trap 'rm -rf "$test_dir"' EXIT
mkdir -p "${test_dir}/bin"
if [[ -n ${jq_bin} && -x ${jq_bin} ]]; then
  cp "${jq_bin}" "${test_dir}/bin/jq"
elif host_jq=$(command -v jq 2>/dev/null); then
  cp "${host_jq}" "${test_dir}/bin/jq"
else
  printf 'Error: jq executable was not found\n' >&2
  exit 1
fi
export PATH="${test_dir}/bin:${PATH}"
curl_log="${test_dir}/curl.log"

cat >"${test_dir}/bin/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '<%s>\n' "$@" >>"${WRITER_INVENTORY_CURL_LOG:?}"
if [[ "${WRITER_INVENTORY_CURL_FAIL:-false}" == true ]]; then
  exit 22
fi
case " $* " in
  *'/apis/apps/v1/namespaces/team-examples-workspaces/deployments'*)
    printf '%s\n' '{"apiVersion":"apps/v1","kind":"DeploymentList","items":[{"metadata":{"name":"foreign-deployment","labels":{"com.coder.workspace.id":"bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb","com.coder.workspace.name":"research"}},"spec":{"replicas":1}}]}'
    ;;
  *'/api/v1/namespaces/team-examples-workspaces/pods'*)
    printf '%s\n' '{"apiVersion":"meta.k8s.io/v1","kind":"PartialObjectMetadataList","items":[{"metadata":{"name":"foreign-pod","labels":{"com.coder.workspace.id":"bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb","com.coder.workspace.name":"research"}}}]}'
    ;;
  *) exit 22 ;;
esac
EOF
chmod +x "${test_dir}/bin/curl"

kubeconfig="${test_dir}/kubeconfig"
cat >"${kubeconfig}" <<'EOF'
"apiVersion": "v1"
"clusters":
- "cluster":
    "certificate-authority-data": "Y2E="
    "server": "https://172.19.255.11:6443"
  "name": "cell-eaws-lh1"
"contexts":
- "context":
    "cluster": "cell-eaws-lh1"
    "user": "cluster:coder-provisioner:cell-eaws-lh1"
  "name": "cell-eaws-lh1"
"current-context": "cell-eaws-lh1"
"users":
- "name": "cluster:coder-provisioner:cell-eaws-lh1"
  "user":
    "client-certificate-data": "Y2VydA=="
    "client-key-data": "a2V5"
EOF

output="${test_dir}/output.json"
env \
  "PATH=${test_dir}/bin:${PATH}" \
  "WRITER_INVENTORY_CURL_LOG=${curl_log}" \
  "${subject}" \
  "${kubeconfig}" \
  team-examples-workspaces \
  123e4567-e89b-42d3-a456-426614174000 \
  cell-eaws-lh1 >"${output}"

jq -e 'keys == ["deployments", "pods"]' "${output}" >/dev/null
jq -r '.deployments' "${output}" | openssl base64 -d -A \
  | jq -e '.kind == "DeploymentList" and .items[0].metadata.name == "foreign-deployment"' >/dev/null
jq -r '.pods' "${output}" | openssl base64 -d -A \
  | jq -e '.kind == "PartialObjectMetadataList" and .items[0].metadata.name == "foreign-pod"' >/dev/null
grep -Fx '<Accept: application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1>' "${curl_log}" >/dev/null
grep -Fx '<fieldSelector=status.phase!=Succeeded,status.phase!=Failed>' "${curl_log}" >/dev/null
grep -Fx '<labelSelector=app.kubernetes.io/name=coder-workspace,com.coder.user.id=123e4567-e89b-42d3-a456-426614174000>' "${curl_log}" >/dev/null
if grep -Eq 'Y2E=|Y2VydA==|a2V5' "${output}"; then
  printf '%s\n' 'The compact inventory exposed kubeconfig credentials.' >&2
  exit 1
fi

if env \
  "PATH=${test_dir}/bin:${PATH}" \
  "WRITER_INVENTORY_CURL_FAIL=true" \
  "WRITER_INVENTORY_CURL_LOG=${curl_log}" \
  "${subject}" \
  "${kubeconfig}" \
  team-examples-workspaces \
  123e4567-e89b-42d3-a456-426614174000 \
  cell-eaws-lh1 >"${test_dir}/failure.json" 2>/dev/null; then
  printf '%s\n' 'The compact inventory accepted a failed Kubernetes request.' >&2
  exit 1
fi
test ! -s "${test_dir}/failure.json"

cp "${kubeconfig}" "${test_dir}/duplicate-kubeconfig"
printf '%s\n' '    "server": "https://192.0.2.1:6443"' >>"${test_dir}/duplicate-kubeconfig"
if env \
  "PATH=${test_dir}/bin:${PATH}" \
  "WRITER_INVENTORY_CURL_LOG=${curl_log}" \
  "${subject}" \
  "${test_dir}/duplicate-kubeconfig" \
  team-examples-workspaces \
  123e4567-e89b-42d3-a456-426614174000 \
  cell-eaws-lh1 >/dev/null 2>&1; then
  printf '%s\n' 'The compact inventory accepted an ambiguous kubeconfig.' >&2
  exit 1
fi

if env \
  "PATH=${test_dir}/bin:${PATH}" \
  "WRITER_INVENTORY_CURL_LOG=${curl_log}" \
  "${subject}" \
  "${kubeconfig}" \
  team-examples-workspaces \
  not-a-uuid \
  cell-eaws-lh1 >/dev/null 2>&1; then
  printf '%s\n' 'The compact inventory accepted an invalid owner UUID.' >&2
  exit 1
fi

mkdir -p "${test_dir}/contexts"
auth_log="${test_dir}/auth.log"
cat >"${test_dir}/bin/auth" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '<%s>\n' "$@" >"${WRITER_INVENTORY_AUTH_LOG:?}"
printf '<GOOGLE_APPLICATION_CREDENTIALS=%s>\n' "${GOOGLE_APPLICATION_CREDENTIALS:-}" >>"$WRITER_INVENTORY_AUTH_LOG"
printf '%s\n' '{"apiVersion":"client.authentication.k8s.io/v1beta1","kind":"ExecCredential","status":{"token":"fixture.exec-token_1"}}'
EOF
chmod +x "${test_dir}/bin/auth"
cat >"${test_dir}/contexts/cell-gcp-euw4" <<EOF
"apiVersion": "v1"
"clusters":
- "cluster":
    "certificate-authority-data": "Y2E="
    "server": "https://172.19.255.12:6443"
  "name": "cell-gcp-euw4"
"contexts":
- "context":
    "cluster": "cell-gcp-euw4"
    "user": "coder-provisioner@example.invalid"
  "name": "cell-gcp-euw4"
"current-context": "cell-gcp-euw4"
"users":
- "name": "coder-provisioner@example.invalid"
  "user":
    "exec":
      "apiVersion": "client.authentication.k8s.io/v1beta1"
      "args":
      - "gcp"
      "command": "${test_dir}/bin/auth"
      "env":
      - "name": "GOOGLE_APPLICATION_CREDENTIALS"
        "value": "${test_dir}/gcp-credential-configuration.json"
EOF

: >"${curl_log}"
env \
  "PATH=${test_dir}/bin:${PATH}" \
  "WRITER_INVENTORY_AUTH_LOG=${auth_log}" \
  "WRITER_INVENTORY_CURL_LOG=${curl_log}" \
  "${subject}" \
  "${kubeconfig}" \
  team-examples-workspaces \
  123e4567-e89b-42d3-a456-426614174000 \
  cell-gcp-euw4 >"${test_dir}/exec-output.json"

jq -e 'keys == ["deployments", "pods"]' "${test_dir}/exec-output.json" >/dev/null
grep -Fx '<gcp>' "${auth_log}" >/dev/null
grep -Fx "<GOOGLE_APPLICATION_CREDENTIALS=${test_dir}/gcp-credential-configuration.json>" "${auth_log}" >/dev/null
grep -Fx '<Authorization: Bearer fixture.exec-token_1>' "${curl_log}" >/dev/null
grep -Fx '<https://172.19.255.12:6443/apis/apps/v1/namespaces/team-examples-workspaces/deployments>' "${curl_log}" >/dev/null
if grep -Fq 'fixture.exec-token_1' "${test_dir}/exec-output.json"; then
  printf '%s\n' 'The compact exec-auth inventory exposed its bearer token.' >&2
  exit 1
fi

cp "${test_dir}/contexts/cell-gcp-euw4" "${test_dir}/contexts/cell-gcp-usw1"
if env \
  "PATH=${test_dir}/bin:${PATH}" \
  "WRITER_INVENTORY_AUTH_LOG=${auth_log}" \
  "WRITER_INVENTORY_CURL_LOG=${curl_log}" \
  "${subject}" \
  "${kubeconfig}" \
  team-examples-workspaces \
  123e4567-e89b-42d3-a456-426614174000 \
  cell-gcp-usw1 >/dev/null 2>&1; then
  printf '%s\n' 'The compact exec-auth inventory accepted a mismatched selected context.' >&2
  exit 1
fi
