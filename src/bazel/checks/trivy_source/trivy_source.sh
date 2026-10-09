#!/usr/bin/env bash
# Scan workspace source files and configuration manifests for security misconfigurations using Trivy.

set -o errexit -o nounset -o pipefail
umask 077

root="${BUILD_WORKSPACE_DIRECTORY:-$(git rev-parse --show-toplevel)}"
cd "${root}"

# LINT.IfChange(kubernetes-version)
readonly KUBERNETES_VERSION=1.36.0
# LINT.ThenChange(//MODULE.bazel:kubernetes-version)
readonly TRIVY_TIMEOUT=20m
policy_file="src/bazel/checks/trivy_source/policy.json"
policy_dir="src/bazel/checks/trivy_source"
ignore_policy="src/bazel/checks/trivy_source/trivyignore.yaml"
data_policy="${root}/src/bazel/checks/trivy_source/data"
output_dir="${root}/.tmp/artifacts/security/source"
cache_dir="${REPO_CACHE_DIR:-${XDG_CACHE_HOME:-${HOME}/.cache}/repo}/security/trivy"

die() {
  printf '%s\n' "$*" >&2
  exit 2
}

for policy_path in "${policy_file}" "${ignore_policy}" "${data_policy}" "${policy_dir}/trivy_triage.py"; do
  [[ -e ${policy_path} && ! -L ${policy_path} ]] \
    || die "security scan policy is missing or unsafe: ${policy_path}"
done

# The fixture path drives what the scans skip and force, so it is read before
# any report exists; triage.rego re-validates the same constraints afterwards.
negative_fixture="$(jq -er '
  .intentionalSecurityNegativeFixtures
  | select(type == "array" and length == 1)
  | .[0].path
  | select(type == "string" and startswith("src/infra/definitions/conformance/") and endswith(".test.k8s.yaml"))
' "${policy_file}")" || die 'security scan policy must name exactly one safe negative fixture'
case "/${negative_fixture}/" in
  */../*) die 'security scan policy contains a parent path traversal' ;;
  *) ;;
esac
[[ -f ${negative_fixture} && ! -L ${negative_fixture} ]] \
  || die "intentional security-negative fixture is missing or unsafe: ${negative_fixture}"
grep -F 'Intentionally unsafe scanner self-test input' "${negative_fixture}" >/dev/null \
  || die 'intentional security-negative fixture lacks its explicit source marker'

for directory in "${output_dir}" "${cache_dir}/reports"; do
  [[ ! -L ${directory} ]] || die "security scan directory must not be a symlink: ${directory}"
  mkdir -p "${directory}"
  chmod 0700 "${directory}"
done

scan() {
  local target="$1" destination="$2" log_stem="$3"
  shift 3
  local temporary_report
  temporary_report="$(mktemp "${output_dir}/.trivy-config.XXXXXX")"
  if ! trivy \
    --cache-dir "${cache_dir}" \
    --timeout "${TRIVY_TIMEOUT}" \
    --disable-telemetry \
    --skip-version-check \
    config \
    --exit-code 0 \
    --format json \
    --ignorefile "${ignore_policy}" \
    --data "${data_policy}" \
    --k8s-version "${KUBERNETES_VERSION}" \
    --output "${temporary_report}" \
    --severity UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL \
    --tf-exclude-downloaded-modules \
    "$@" \
    "${target}" >"${output_dir}/${log_stem}.stdout.log" \
    2>"${output_dir}/${log_stem}.stderr.log"; then
    printf 'Trivy source configuration scan failed for %s; the end of %s.stderr.log:\n' \
      "${target}" "${log_stem}" >&2
    tail -n 20 "${output_dir}/${log_stem}.stderr.log" >&2
    rm -f -- "${temporary_report}"
    return 1
  fi
  jq -e 'type == "object"' "${temporary_report}" >/dev/null \
    || die "scanner produced invalid JSON: ${temporary_report}"
  if [[ -d ${target} && ${target} != "." ]]; then
    local normalized_report
    normalized_report="$(mktemp "${output_dir}/.trivy-config.XXXXXX")"
    jq --arg dir "${target}" 'walk(if type == "object" and has("Target") and (.Target | startswith("/") | not) then .Target = ($dir + "/" + .Target) else . end)' "${temporary_report}" >"${normalized_report}"
    mv -f -- "${normalized_report}" "${temporary_report}"
  fi
  chmod 0600 "${temporary_report}"
  mv -f -- "${temporary_report}" "${destination}"
}

scan_fs() {
  local target="$1" destination="$2" log_stem="$3"
  shift 3
  local temporary_report
  temporary_report="$(mktemp "${output_dir}/.trivy-fs.XXXXXX")"
  if ! trivy \
    --cache-dir "${cache_dir}" \
    --timeout "${TRIVY_TIMEOUT}" \
    --disable-telemetry \
    --skip-version-check \
    fs \
    --scanners vuln \
    --exit-code 0 \
    --format json \
    --ignorefile "${ignore_policy}" \
    --output "${temporary_report}" \
    --severity UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL \
    "$@" \
    "${target}" >"${output_dir}/${log_stem}.stdout.log" \
    2>"${output_dir}/${log_stem}.stderr.log"; then
    printf 'Trivy source filesystem scan failed for %s; the end of %s.stderr.log:\n' \
      "${target}" "${log_stem}" >&2
    tail -n 20 "${output_dir}/${log_stem}.stderr.log" >&2
    rm -f -- "${temporary_report}"
    return 1
  fi
  jq -e 'type == "object"' "${temporary_report}" >/dev/null \
    || die "scanner produced invalid JSON: ${temporary_report}"
  if [[ -d ${target} && ${target} != "." ]]; then
    local normalized_report
    normalized_report="$(mktemp "${output_dir}/.trivy-fs.XXXXXX")"
    jq --arg dir "${target}" 'walk(if type == "object" and has("Target") and (.Target | startswith("/") | not) then .Target = ($dir + "/" + .Target) else . end)' "${temporary_report}" >"${normalized_report}"
    mv -f -- "${normalized_report}" "${temporary_report}"
  fi
  chmod 0600 "${temporary_report}"
  mv -f -- "${temporary_report}" "${destination}"
}

policy_inputs=("${policy_file}" "${ignore_policy}" "${data_policy}" "${policy_dir}/trivy_triage.py")

cached_scan() {
  local kind="$1" target="$2" destination="$3" log_stem="$4"
  shift 4
  local shard_changes="" shard_key="" cached_report=""
  shard_changes="$(git status --porcelain --untracked-files=all -- "${target}" "${policy_inputs[@]}")"
  if [[ -z ${shard_changes} ]]; then
    shard_key="$({
      git ls-files -s -- "${target}" "${policy_inputs[@]}"
      trivy --version | sed -n 's/^Version: //p'
      jq -r .Digest "${cache_dir}/policy/metadata.json" 2>/dev/null || echo "no-digest"
      printf '%s\n' "${KUBERNETES_VERSION}"
    } | git hash-object --stdin)"
    cached_report="${cache_dir}/reports/${log_stem}-${shard_key}.json"
    if [[ -f ${cached_report} ]]; then
      cp -- "${cached_report}" "${destination}"
      return 0
    fi
  fi

  if [[ ${kind} == "config" ]]; then
    scan "${target}" "${destination}" "${log_stem}" "$@"
  else
    scan_fs "${target}" "${destination}" "${log_stem}" "$@"
  fi

  if [[ -n ${shard_key} && -n ${cached_report} ]]; then
    cp -- "${destination}" "${cached_report}"
  fi
}

negative="${output_dir}/trivy-config-intentional-negative.json"
cached_scan config "${root}/${negative_fixture}" "${negative}" trivy-config-intentional-negative

primary_reports=(
  "${output_dir}/shard-argocd.json"
  "${output_dir}/shard-definitions.json"
  "${output_dir}/shard-examples.json"
  "${output_dir}/shard-terraform.json"
  "${output_dir}/shard-tools.json"
  "${output_dir}/shard-fs-tools.json"
  "${output_dir}/shard-fs-uv.json"
)

pids=()
cached_scan config src/infra/argocd "${output_dir}/shard-argocd.json" trivy-argocd &
pids+=($!)
cached_scan config src/infra/definitions "${output_dir}/shard-definitions.json" trivy-definitions \
  --skip-files "${root}/${negative_fixture}" &
pids+=($!)
cached_scan config src/examples "${output_dir}/shard-examples.json" trivy-examples &
pids+=($!)
cached_scan config src/infra/terraform "${output_dir}/shard-terraform.json" trivy-terraform \
  --misconfig-scanners terraform &
pids+=($!)
cached_scan config src/infra/tools "${output_dir}/shard-tools.json" trivy-tools &
pids+=($!)
cached_scan fs src/infra/tools "${output_dir}/shard-fs-tools.json" trivy-fs-tools &
pids+=($!)
cached_scan fs uv.lock "${output_dir}/shard-fs-uv.json" trivy-fs-uv &
pids+=($!)

scan_status=0
for pid in "${pids[@]}"; do
  wait "${pid}" || scan_status=1
done
((scan_status == 0)) || exit 1

triage_status=0
python3 "${policy_dir}/trivy_triage.py" \
  "${policy_file}" \
  "${negative}" \
  "${primary_reports[@]}" || triage_status=$?

if ((triage_status != 0)); then
  printf 'Trivy source triage gate failed.\n' >&2
  exit 1
fi
