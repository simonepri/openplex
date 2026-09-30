#!/usr/bin/env bash
# shellcheck disable=SC2312
# Tests owner token attestation workflows, credential caching, and non-identity dry-run handling in attest-owner.sh.

set -euo pipefail

subject=$1
test_dir=$(mktemp -d)
trap 'rm -rf "${test_dir}"' EXIT
mkdir -p "${test_dir}/bin"
curl_calls="${test_dir}/curl-calls"
binding_request='{"binding_url":"https://headscale-workspace-registration.headscale.svc.cluster.local:8443/v1/bind"}'
invalid_binding_request='{"binding_url":"https://headscale.ctrl-eaws-lh1.k8s.example.invalid/v1/bind"}'

cat >"${test_dir}/bin/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
[[ "$*" == '-q --config -' ]]
config=$(cat)
printf '%s\n' called >>"${ATTEST_OWNER_TEST_CALLS:?}"
grep -Fq 'header = "Coder-Session-Token: session-fixture"' <<<"$config"
if grep -Fq '/v1/resolve' <<<"$config"; then
  if [[ ${ATTEST_OWNER_TEST_BIND:-0} == 1 ]]; then
    exit 22
  fi
else
  grep -Fq '/v1/bind' <<<"$config"
  grep -Fq 'header = "Authorization: Bearer oidc-fixture"' <<<"$config"
  grep -Fq 'data = "{\"owner_id\":\"8826ee2e-7933-4665-aef2-2393f84a0d05\"}"' <<<"$config"
fi
printf '%s\n' '{"attested":"true","email":"ldap@example.com","id":"8826ee2e-7933-4665-aef2-2393f84a0d05","preferred_username":"ldap_user","principal_id":"KQPr5PeWs4TXZlXuU4DtAcBcrSmeWHyLtQ_fRwUrShw"}'
EOF
chmod +x "${test_dir}/bin/curl"

assert_nonsecret_result() {
  case "$1" in
    *session-fixture* | *oidc-fixture*)
      echo 'attestation result exposed an owner credential' >&2
      exit 1
      ;;
    *) ;;
  esac
}

run_start() {
  printf '%s\n' "${binding_request}" \
    | env \
      ATTEST_OWNER_TEST_BIND="${1:-0}" \
      ATTEST_OWNER_TEST_CALLS="${curl_calls}" \
      CODER_WORKSPACE_OWNER_ID=8826ee2e-7933-4665-aef2-2393f84a0d05 \
      CODER_WORKSPACE_OWNER_OIDC_ACCESS_TOKEN=oidc-fixture \
      CODER_WORKSPACE_OWNER_SESSION_TOKEN=session-fixture \
      CODER_WORKSPACE_BUILD_ID=087c0102-fd2d-424f-b359-1d438b31cc45 \
      CODER_WORKSPACE_TRANSITION=start \
      PATH="${test_dir}/bin:${PATH}" \
      "${subject}"
}

result=$(run_start 0)
[[ ${result} == *'"attested":"true"'* ]]
assert_nonsecret_result "${result}"
result=$(run_start 1)
[[ ${result} == *'"preferred_username":"ldap_user"'* ]]
assert_nonsecret_result "${result}"
[[ $(grep -c '^called$' "${curl_calls}") == 3 ]]

run_preview() {
  printf '%s\n' "${binding_request}" \
    | env -u CODER_WORKSPACE_BUILD_ID \
      -u CODER_WORKSPACE_OWNER_OIDC_ACCESS_TOKEN \
      -u CODER_WORKSPACE_OWNER_SESSION_TOKEN \
      ATTEST_OWNER_TEST_CALLS="${curl_calls}" \
      CODER_WORKSPACE_OWNER_ID="${2:-}" \
      CODER_WORKSPACE_TRANSITION="$1" \
      PATH="${test_dir}/bin:${PATH}" \
      "${subject}"
}

for transition in "" unspecified start; do
  result=$(run_preview "${transition}")
  [[ ${result} == *'"attested":"false"'* ]]
  [[ ${result} == *'"preview":"true"'* ]]
  assert_nonsecret_result "${result}"
done

result=$(run_preview start 8826ee2e-7933-4665-aef2-2393f84a0d05)
[[ ${result} == *'"attested":"false"'* ]]
[[ ${result} == *'"preview":"true"'* ]]
assert_nonsecret_result "${result}"

for transition in stop destroy; do
  result=$(
    printf '%s\n' "${binding_request}" \
      | env \
        ATTEST_OWNER_TEST_CALLS="${curl_calls}" \
        CODER_WORKSPACE_BUILD_ID=087c0102-fd2d-424f-b359-1d438b31cc45 \
        CODER_WORKSPACE_OWNER_ID=8826ee2e-7933-4665-aef2-2393f84a0d05 \
        CODER_WORKSPACE_TRANSITION="${transition}" \
        PATH="${test_dir}/bin:${PATH}" \
        "${subject}"
  )
  [[ ${result} == *'"attested":"false"'* ]]
  [[ ${result} == *'"preview":"false"'* ]]
  assert_nonsecret_result "${result}"
done

if printf '%s\n' "${binding_request}" \
  | env \
    ATTEST_OWNER_TEST_CALLS="${curl_calls}" \
    CODER_WORKSPACE_BUILD_ID=087c0102-fd2d-424f-b359-1d438b31cc45 \
    CODER_WORKSPACE_TRANSITION=start \
    PATH="${test_dir}/bin:${PATH}" \
    "${subject}"; then
  echo 'real build without an owner was accepted' >&2
  exit 1
fi

[[ $(grep -c '^called$' "${curl_calls}") == 3 ]]

failure=${test_dir}/failure
if printf '%s\n' "${invalid_binding_request}" \
  | env \
    ATTEST_OWNER_TEST_CALLS="${curl_calls}" \
    CODER_WORKSPACE_BUILD_ID=087c0102-fd2d-424f-b359-1d438b31cc45 \
    CODER_WORKSPACE_OWNER_ID=8826ee2e-7933-4665-aef2-2393f84a0d05 \
    CODER_WORKSPACE_OWNER_OIDC_ACCESS_TOKEN=oidc-fixture \
    CODER_WORKSPACE_OWNER_SESSION_TOKEN=session-fixture \
    CODER_WORKSPACE_TRANSITION=invalid \
    PATH="${test_dir}/bin:${PATH}" \
    "${subject}" >"${failure}" 2>&1; then
  echo 'invalid transition was accepted' >&2
  exit 1
fi
assert_nonsecret_result "$(cat "${failure}")"
[[ $(grep -c '^called$' "${curl_calls}") == 3 ]]
