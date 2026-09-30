#!/usr/bin/env bash
# shellcheck disable=SC2310,SC2312
# Tests agent registration HTTP exchanges, backoff retry logic, and memory-only credential lifecycle handling.

set -euo pipefail

subject=${1:?workspace-agent registration script path was not supplied}
context_subject=${2:?workspace build context script path was not supplied}
jq_bin=${3:-$(command -v jq || true)}
test_root=$(mktemp -d)
cleanup() {
  for pid_file in "${test_root}"/*/subshell.pid; do
    if [[ -f ${pid_file} ]]; then
      kill "$(<"${pid_file}")" 2>/dev/null || true
    fi
  done
  rm -rf -- "${test_root}"
}
trap cleanup EXIT
mkdir -p "${test_root}/bin"
if [[ -n ${jq_bin} && -x ${jq_bin} ]]; then
  cp "${jq_bin}" "${test_root}/bin/jq"
elif host_jq=$(command -v jq 2>/dev/null); then
  cp "${host_jq}" "${test_root}/bin/jq"
else
  printf 'Error: jq executable was not found\n' >&2
  exit 1
fi
export PATH="${test_root}/bin:${PATH}"

cat >"${test_root}/bin/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
[[ -z ${CODER_AGENT_TOKEN:-} ]]
[[ -z ${CODER_WORKSPACE_OWNER_SESSION_TOKEN:-} ]]

config_path=
output=
write_out=
printf '<%s>' "$@" >>"${TEST_CASE:?}/curl.args"
while (($#)); do
	case "$1" in
		--config) config_path=$2; shift 2 ;;
		--output) output=$2; shift 2 ;;
		--write-out) write_out=$2; shift 2 ;;
		-q) shift ;;
		*) printf 'unexpected curl argument: %s\n' "$1" >&2; exit 2 ;;
	esac
done
[[ $config_path == - ]]
[[ -n $output ]]
[[ $write_out == '%{http_code} %{size_download}' ]]
: >"$output"

config=$(cat)
request=$(sed -n 's/^data-binary = "\(.*\)"$/\1/p' <<<"$config" | sed 's/\\"/"/g; s/\\\\/\\/g')
grep -Fx 'url = "https://headscale-workspace-registration.headscale.svc.cluster.local:8443/v1/workspace-agents/register"' <<<"$config" >/dev/null
grep -Fx 'header = "Authorization: Bearer eyFixture.Projected.Token"' <<<"$config" >/dev/null
grep -Fx 'header = "Coder-Session-Token: coder-owner-session-fixture"' <<<"$config" >/dev/null
jq -e '
  . == {
    agentToken: "11111111-1111-4111-8111-111111111111",
    buildId: "22222222-2222-4222-8222-222222222222",
    cell: "cell-eaws-lh1",
    incarnation: "b4fb171ce4bf",
    isPrebuildClaim: false,
    lineage: "0123456789abcdef0123456789abcdef01234567",
    machine: "ldap-dev",
    ownerId: "33333333-3333-4333-8333-333333333333",
    team: "examples",
    workspaceId: "44444444-4444-4444-8444-444444444444",
    workspaceName: "dev",
    workspaceVolume: "/var/lib/workspace"
  }
' <<<"$request" >/dev/null

count=0
[[ ! -f $TEST_CASE/count ]] || count=$(<"$TEST_CASE/count")
count=$((count + 1))
printf '%s' "$count" >"$TEST_CASE/count"
response=$(sed -n "${count}p" "$TEST_CASE/responses")
[[ -n $response ]] || response=$(tail -1 "$TEST_CASE/responses")
printf '%s\n' "$response" >>"$TEST_CASE/events"
case "$response" in
	transport) printf '000 0'; exit 7 ;;
	*:*) printf '%s %s' "${response%%:*}" "${response#*:}" ;;
	*) printf '%s 0' "$response" ;;
esac
EOF

cat >"${test_root}/bin/awk" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
[[ ${2:-} == /proc/uptime ]]
read -r now _ <"${TEST_CASE:?}/clock"
printf '%s\n' "${now%%.*}"
EOF

real_sleep=$(command -v sleep)
cat >"${test_root}/bin/sleep" <<EOF
#!/usr/bin/env bash
set -euo pipefail
[[ \$1 == 5 ]]
clock=\${TEST_CASE:?}/clock
printf '%s\\n' "\$PPID" >"\${TEST_CASE:?}/subshell.pid"
read -r now _ <"\$clock"
printf '%s.0 0.0\\n' "\$(( \${now%%.*} + \${TEST_CLOCK_STEP:-100} ))" >"\$clock"
"${real_sleep}" 0.02
EOF
chmod +x "${test_root}/bin/awk" "${test_root}/bin/curl" "${test_root}/bin/sleep"

run_subject() {
  local case_name=$1
  local responses=$2
  shift 2
  local expect_fail=0
  if [[ ${1:-} == "--expect-fail" ]]; then
    expect_fail=1
    shift
  fi
  local case_directory=${test_root}/${case_name}
  mkdir -p "${case_directory}/runtime"
  printf '%s\n' "${responses}" >"${case_directory}/responses"
  printf '100.0 0.0\n' >"${case_directory}/clock"
  printf '%s' 'eyFixture.Projected.Token' >"${case_directory}/registration-token"
  set +e
  env \
    CODER_WORKSPACE_OWNER_SESSION_TOKEN=coder-owner-session-fixture \
    CODER_AGENT_TOKEN=11111111-1111-4111-8111-111111111111 \
    CODER_WORKSPACE_BUILD_ID=22222222-2222-4222-8222-222222222222 \
    WORKSPACE_CELL=cell-eaws-lh1 \
    WORKSPACE_CELL_INCARNATION=b4fb171ce4bf \
    CODER_WORKSPACE_IS_PREBUILD_CLAIM=false \
    WORKSPACE_MACHINE=ldap-dev \
    CODER_WORKSPACE_OWNER_ID=33333333-3333-4333-8333-333333333333 \
    WORKSPACE_TEAM=examples \
    TEST_CASE="${case_directory}" \
    CODER_WORKSPACE_AGENT_REGISTRATION_TOKEN_FILE="${case_directory}/registration-token" \
    CODER_WORKSPACE_AGENT_REGISTRATION_URL=https://headscale-workspace-registration.headscale.svc.cluster.local:8443/v1/workspace-agents/register \
    CODER_WORKSPACE_ID=44444444-4444-4444-8444-444444444444 \
    WORKSPACE_LINEAGE=0123456789abcdef0123456789abcdef01234567 \
    CODER_WORKSPACE_NAME=dev \
    PATH="${test_root}/bin:${PATH}" \
    TMPDIR="${case_directory}/runtime" \
    "$@" "${subject}" >"${case_directory}/stdout" 2>"${case_directory}/stderr"
  local status=$?
  set -e
  if [[ ${status} -ne 0 && ${expect_fail} -eq 0 ]]; then
    printf 'run_subject (%s) failed with exit code %d:\n' "${case_name}" "${status}" >&2
    if [[ -s "${case_directory}/stderr" ]]; then
      cat "${case_directory}/stderr" >&2
    fi
    if [[ -s "${case_directory}/stdout" ]]; then
      cat "${case_directory}/stdout" >&2
    fi
  fi
  return "${status}"
}

wait_for_calls() {
  local case_name=$1
  local wanted=$2
  local case_directory=${test_root}/${case_name}
  for _ in {1..100}; do
    if [[ -f ${case_directory}/count ]] && [[ $(<"${case_directory}/count") -ge ${wanted} ]]; then
      return
    fi
    "${real_sleep}" 0.02
  done
  local count=0
  [[ ! -f ${case_directory}/count ]] || count=$(<"${case_directory}/count")
  printf '%s reached only %s registration calls, want %s\n' \
    "${case_name}" "${count}" "${wanted}" >&2
  exit 1
}

wait_for_subshell() {
  local case_name=$1
  local case_directory=${test_root}/${case_name}
  for _ in {1..100}; do
    if [[ ! -f ${case_directory}/subshell.pid ]]; then
      "${real_sleep}" 0.02
      continue
    fi
    local pid
    pid=$(<"${case_directory}/subshell.pid")
    if ! kill -0 "${pid}" 2>/dev/null; then
      return 0
    fi
    "${real_sleep}" 0.02
  done
  printf '%s subshell did not terminate\n' "${case_name}" >&2
  exit 1
}

run_subject restart-recovery $'202\ntransport\n503\n202\n409'
wait_for_calls restart-recovery 5
wait_for_subshell restart-recovery
grep -Fx transport "${test_root}/restart-recovery/events" >/dev/null
grep -Fx 503 "${test_root}/restart-recovery/events" >/dev/null
grep -Fx 202 "${test_root}/restart-recovery/events" >/dev/null
grep -Fx 409 "${test_root}/restart-recovery/events" >/dev/null
test ! -s "${test_root}/restart-recovery/stdout"
test ! -s "${test_root}/restart-recovery/stderr"
test -z "$(find "${test_root}/restart-recovery/runtime" -mindepth 1 -print -quit)"
if grep -Eq 'eyFixture|coder-owner-session|11111111' "${test_root}/restart-recovery/curl.args"; then
  printf '%s\n' 'a registration credential was passed in curl argv' >&2
  exit 1
fi

run_subject bounded-expiry $'202\n202' \
  TEST_CLOCK_STEP=400
wait_for_calls bounded-expiry 3
wait_for_subshell bounded-expiry
[[ $(<"${test_root}/bounded-expiry/count") == 3 ]]

for response_case in wrong-status non-empty transport-failure; do
  case "${response_case}" in
    wrong-status) responses=200 ;;
    non-empty) responses=202:1 ;;
    transport-failure) responses=transport ;;
    *) ;;
  esac
  if run_subject "${response_case}" "${responses}" --expect-fail; then
    printf '%s response was accepted\n' "${response_case}" >&2
    exit 1
  fi
done

if run_subject public-url 202 --expect-fail \
  CODER_WORKSPACE_AGENT_REGISTRATION_URL=https://headscale.ctrl.example.com/v1/workspace-agents/register; then
  printf '%s\n' 'a public registration URL was accepted' >&2
  exit 1
fi

if run_subject prebuild 202 --expect-fail CODER_WORKSPACE_IS_PREBUILD_CLAIM=true; then
  printf '%s\n' 'a prebuild claim registered a workspace agent' >&2
  exit 1
fi

test_build_context=$(CODER_WORKSPACE_BUILD_ID=55555555-5555-4555-8555-555555555555 "${context_subject}")
[[ $(jq -r .build_id <<<"${test_build_context}") == '55555555-5555-4555-8555-555555555555' ]]
[[ $(jq -r .timestamp <<<"${test_build_context}") =~ ^[0-9]{10,}$ ]]
empty_build_context=$(env -u CODER_WORKSPACE_BUILD_ID "${context_subject}")
[[ $(jq -r .build_id <<<"${empty_build_context}") == '' ]]
[[ $(jq -r .timestamp <<<"${empty_build_context}") =~ ^[0-9]{10,}$ ]]
if CODER_WORKSPACE_BUILD_ID=not-a-uuid "${context_subject}" 2>/dev/null; then
  printf '%s\n' 'an invalid Coder build ID was accepted' >&2
  exit 1
fi
