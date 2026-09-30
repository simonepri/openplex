#!/usr/bin/env bash
# Run Kubescape NSA framework compliance scans over rendered Kubernetes manifests and fixtures.

set -o errexit -o nounset -o pipefail
umask 077

kubescape_bin="${1:-kubescape}"
if [[ ${kubescape_bin#/} == "${kubescape_bin}" ]] && [[ -f ${kubescape_bin} ]]; then
  kubescape_bin="${PWD}/${kubescape_bin}"
fi

root="${BUILD_WORKSPACE_DIRECTORY:-$(git rev-parse --show-toplevel)}"
cd "${root}"

policy_file="src/bazel/checks/kubescape/policy.json"
policy_dir="src/bazel/checks/kubescape"
output_dir="${root}/.tmp/artifacts/security/kubescape"
cache_dir="${REPO_CACHE_DIR:-${XDG_CACHE_HOME:-${HOME}/.cache}/repo}/security/kubescape"

die() {
  printf '%s\n' "$*" >&2
  exit 2
}

for policy_path in "${policy_file}" "${policy_dir}/kubescape_triage.py"; do
  [[ -e ${policy_path} && ! -L ${policy_path} ]] \
    || die "security scan policy is missing or unsafe: ${policy_path}"
done

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

bazel_root="${BAZEL_OUTPUT_ROOT:-${root}/.tmp/state/bazel}"
# shellcheck disable=SC2086
bazel_bin_dir="${CHECK_BAZEL_BIN:-$(bazel --output_user_root="${bazel_root}" info ${BAZEL_CONFIG_FLAGS:-} bazel-bin 2>/dev/null)}/src/infra/argocd/components"

primary_report="${output_dir}/kubescape-rendered.json"
negative_report="${output_dir}/kubescape-negative.json"

# Collect all non-empty rendered manifests
scannable_manifests=()
collect_manifests() {
  scannable_manifests=()
  if [[ -d ${bazel_bin_dir} ]]; then
    while IFS= read -r f; do
      [[ -n ${f} ]] || continue
      # Only include files containing Kubernetes resources (apiVersion or kind)
      if grep -Eq '^[[:space:]]*(apiVersion|kind):' "${f}" 2>/dev/null; then
        scannable_manifests+=("${f}")
      fi
    done < <(find "${bazel_bin_dir}" -type f -name "*_render*.yaml" | LC_ALL=C sort || true)
  fi
}

collect_manifests

if [[ ${#scannable_manifests[@]} -eq 0 ]]; then
  echo "No rendered manifests found in ${bazel_bin_dir}; rendering ArgoCD components..." >&2
  # shellcheck disable=SC2086
  bazel --output_user_root="${bazel_root}" build ${BAZEL_CONFIG_FLAGS:-} --remote_download_outputs=all //src/infra/argocd/components/... >/dev/null 2>&1 || true
  collect_manifests
fi

if [[ ${#scannable_manifests[@]} -eq 0 ]]; then
  die "no rendered manifests found in ${bazel_bin_dir}; ensure bazel build -- //... ran before check"
fi

# Run primary scan
temp_primary="$(mktemp "${output_dir}/.ks-primary.XXXXXX")"
mv -f -- "${temp_primary}" "${temp_primary}.json"
temp_primary="${temp_primary}.json"
"${kubescape_bin}" scan framework nsa \
  --keep-local \
  --cache-dir "${cache_dir}" \
  --format json \
  --output "${temp_primary}" \
  "${scannable_manifests[@]}" >"${output_dir}/kubescape-scan.stdout.log" 2>"${output_dir}/kubescape-scan.stderr.log"

jq -e 'type == "object"' "${temp_primary}" >/dev/null \
  || die "kubescape primary scan produced invalid JSON: ${temp_primary}"
chmod 0600 "${temp_primary}"
mv -f -- "${temp_primary}" "${primary_report}"

# Run negative fixture scan
temp_negative="$(mktemp "${output_dir}/.ks-negative.XXXXXX")"
mv -f -- "${temp_negative}" "${temp_negative}.json"
temp_negative="${temp_negative}.json"
"${kubescape_bin}" scan framework nsa \
  --keep-local \
  --cache-dir "${cache_dir}" \
  --format json \
  --output "${temp_negative}" \
  "${root}/${negative_fixture}" >"${output_dir}/kubescape-negative.stdout.log" 2>"${output_dir}/kubescape-negative.stderr.log"

jq -e 'type == "object"' "${temp_negative}" >/dev/null \
  || die "kubescape negative scan produced invalid JSON: ${temp_negative}"
chmod 0600 "${temp_negative}"
mv -f -- "${temp_negative}" "${negative_report}"

# Run triage validation
python3 "${policy_dir}/kubescape_triage.py" \
  "${policy_file}" \
  "${negative_report}" \
  "${primary_report}"
