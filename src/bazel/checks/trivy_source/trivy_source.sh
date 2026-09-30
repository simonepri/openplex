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

for directory in "${output_dir}" "${cache_dir}"; do
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
  chmod 0600 "${temporary_report}"
  mv -f -- "${temporary_report}" "${destination}"
}

# Scan only what git sees: every ignored path is skipped, so local state,
# agent worktrees, and build outputs never reach the scanners.
ignored_paths="$(mktemp "${output_dir}/.ignored.XXXXXX")"
git ls-files -z --others --ignored --exclude-standard --directory >"${ignored_paths}"
skip_ignored=(--skip-dirs .git)
while IFS= read -r -d '' path; do
  if [[ ${path} == */ ]]; then
    skip_ignored+=(--skip-dirs "${path%/}")
  else
    skip_ignored+=(--skip-files "${path}")
  fi
done <"${ignored_paths}"
rm -f -- "${ignored_paths}"

primary_kubernetes="${output_dir}/trivy-config-kubernetes.json"
primary_config="${output_dir}/trivy-config.json"
primary_fs="${output_dir}/trivy-fs.json"
negative="${output_dir}/trivy-config-intentional-negative.json"

# Read the default scanners from the tool, so a scanner a Trivy upgrade adds
# cannot fall out of the split below.
default_scanners="$(trivy config --help | sed -n 's/.*--misconfig-scanners .*(default \[\(.*\)\])$/\1/p')"
[[ ,${default_scanners}, == *,kubernetes,* ]] \
  || die 'cannot read the default Trivy misconfiguration scanners'
other_scanners="$(tr ',' '\n' <<<"${default_scanners}" | grep -vx kubernetes | paste -sd , -)"

scan_remaining() {
  scan "${root}" "${primary_config}" trivy-config \
    --misconfig-scanners "${other_scanners}" \
    --skip-files "${root}/${negative_fixture}" \
    "${skip_ignored[@]}"
  scan_fs "${root}" "${primary_fs}" trivy-fs "${skip_ignored[@]}" --skip-dirs .cache
}

# Trivy downloads its checks bundle into the cache on first use, and a scan
# that starts while another one writes it loads a partial bundle. The
# one-file scan of the negative fixture fetches the bundle before the
# concurrent scans start.
scan "${root}/${negative_fixture}" "${negative}" trivy-config-intentional-negative

# The Kubernetes scan reads only YAML and JSON files, so its report is a
# function of those files, the policy inputs, Trivy, and the checks bundle
# the negative scan just refreshed. The key is empty while any of those
# files differs from the index, so local edits always get a fresh scan.
kubernetes_inputs=('*.yaml' '*.yml' '*.json' "${policy_dir}")
kubernetes_key=""
kubernetes_changes="$(git status --porcelain --untracked-files=all -- "${kubernetes_inputs[@]}")"
if [[ -z ${kubernetes_changes} ]]; then
  kubernetes_key="$({
    git ls-files -s -- "${kubernetes_inputs[@]}"
    trivy --version | sed -n 's/^Version: //p'
    jq -r .Digest "${cache_dir}/policy/metadata.json"
    printf '%s\n' "${KUBERNETES_VERSION}" "${negative_fixture}"
  } | git hash-object --stdin)"
fi
kubernetes_reports="${cache_dir}/kubernetes-reports"
cached_kubernetes="${kubernetes_reports}/${kubernetes_key}.json"

# The Kubernetes checks take most of the run on a single core, so they get
# their own process while the remaining scans run beside them. They still see
# every manifest at once: a check applies to the whole batch when any
# manifest in it matches the check's resource kind.
scan_kubernetes() {
  if [[ -n ${kubernetes_key} && -f ${cached_kubernetes} ]]; then
    cp -- "${cached_kubernetes}" "${primary_kubernetes}"
    return
  fi
  scan "${root}" "${primary_kubernetes}" trivy-config-kubernetes \
    --misconfig-scanners kubernetes \
    --skip-files "${root}/${negative_fixture}" \
    "${skip_ignored[@]}"
  if [[ -n ${kubernetes_key} ]]; then
    rm -rf -- "${kubernetes_reports}"
    mkdir -p -- "${kubernetes_reports}"
    cp -- "${primary_kubernetes}" "${cached_kubernetes}"
  fi
}

scan_kubernetes &
kubernetes_pid=$!
scan_remaining &
remaining_pid=$!
scan_status=0
wait "${kubernetes_pid}" || scan_status=1
wait "${remaining_pid}" || scan_status=1
((scan_status == 0)) || exit 1

triage_status=0
python3 "${policy_dir}/trivy_triage.py" \
  "${policy_file}" \
  "${negative}" \
  "${primary_kubernetes}" \
  "${primary_config}" \
  "${primary_fs}" || triage_status=$?
if ((triage_status != 0)); then
  printf 'Trivy source triage gate failed.\n' >&2
  exit 1
fi
