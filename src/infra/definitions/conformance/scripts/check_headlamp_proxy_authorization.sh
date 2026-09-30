#!/bin/sh
# Validates kube-oidc-proxy ServiceAccount impersonation boundaries to defend cell Kubernetes APIs against unauthorized privilege escalation.

set -eu

ca_certificate="$(mktemp)"
response="$(mktemp)"
trap 'rm -f "${ca_certificate}" "${response}"' EXIT

cluster_config="$(kubectl config view --raw --flatten --minify --output=json)"
server="$(printf '%s' "${cluster_config}" | jq --raw-output '.clusters[0].cluster.server')"
printf '%s' "${cluster_config}" \
  | jq --raw-output '.clusters[0].cluster["certificate-authority-data"]' \
  | openssl base64 -d -A >"${ca_certificate}"
proxy_token="$(
  kubectl create token kube-oidc-proxy --namespace=kube-system --duration=10m
)"

api_request() {
  method="${1:?HTTP method is required}"
  path="${2:?API path is required}"
  shift 2
  curl --cacert "${ca_certificate}" --noproxy '*' \
    --request "${method}" --silent --show-error \
    --output "${response}" --write-out '%{http_code}' \
    --header "Authorization: Bearer ${proxy_token}" \
    "$@" "${server}${path}"
}

operator_request() {
  method="${1:?HTTP method is required}"
  path="${2:?API path is required}"
  shift 2
  api_request "${method}" "${path}" \
    --header 'Impersonate-User: cluster:user:headlamp-conformance' \
    --header 'Impersonate-Group: system:authenticated' \
    --header 'Impersonate-Group: cluster:group:operators' \
    --header 'Impersonate-Group: cluster:group:team:examples' \
    --header 'Impersonate-Extra-authentication.kubernetes.io%2Fcredential-id: JTI=headlamp-conformance' \
    "$@"
}

expect_code() {
  expected="${1:?Expected HTTP status is required}"
  shift
  actual="$("$@")"
  if test "${actual}" != "${expected}"; then
    cat "${response}" >&2
    printf 'Expected HTTP %s, got %s.\n' "${expected}" "${actual}" >&2
    exit 1
  fi
}

expect_code 200 operator_request GET \
  '/apis/apiextensions.k8s.io/v1/customresourcedefinitions?limit=1'
expect_code 201 operator_request POST \
  /apis/authorization.k8s.io/v1/selfsubjectrulesreviews \
  --header 'Content-Type: application/json' \
  --data '{"apiVersion":"authorization.k8s.io/v1","kind":"SelfSubjectRulesReview","spec":{"namespace":"team-examples-workloads"}}'
expect_code 200 operator_request GET /healthz
expect_code 403 operator_request GET \
  '/api/v1/namespaces/kube-system/secrets?limit=1'

expect_code 403 api_request GET \
  '/apis/apiextensions.k8s.io/v1/customresourcedefinitions?limit=1' \
  --header 'Impersonate-User: cluster:user:headlamp-no-operator' \
  --header 'Impersonate-Group: system:authenticated' \
  --header 'Impersonate-Group: cluster:group:team:examples'
expect_code 403 api_request GET '/api/v1/namespaces?limit=1' \
  --header 'Impersonate-User: cluster:user:headlamp-conformance' \
  --header 'Impersonate-Group: system:masters'
