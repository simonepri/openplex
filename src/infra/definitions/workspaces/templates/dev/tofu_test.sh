#!/usr/bin/env bash
# shellcheck disable=SC2016
# Executes Terraform test suites and validates configuration plans across supported Coder workspace scenarios.
# The suites use Terraform-only test syntax (override_during), so OpenTofu cannot run them.

set -euo pipefail

tf_bin="${1:?terraform path was not supplied}"
jq_bin="${2:?jq path was not supplied}"

tmp_state="$(mktemp -d)"
trap 'rm -rf "$tmp_state"' EXIT

export TF_DATA_DIR="${tmp_state}"
"${tf_bin}" -chdir=src/infra/definitions/workspaces/templates/dev init -backend=false -input=false -lockfile=readonly >/dev/null
report="${TEST_UNDECLARED_OUTPUTS_DIR:-${tmp_state}}/terraform-test.jsonl"
status=0
"${tf_bin}" -chdir=src/infra/definitions/workspaces/templates/dev test -json -verbose >"${report}" || status=$?
"${jq_bin}" -r '
  select(.type != "test_plan" and .type != "test_state" and .type != "diagnostic") |
  .["@message"], (.diagnostic.detail // empty)
' "${report}"
if [[ ${status} -ne 0 ]]; then
  "${jq_bin}" -r 'select(.type == "diagnostic") | .["@message"], .diagnostic.detail' "${report}"
  exit "${status}"
fi

"${jq_bin}" --exit-status --slurp '
  def resources($run):
    [.[] | select(.type == "test_plan" and .["@testrun"] == $run) |
      .test_plan.resource_changes] |
    if length != 1 then error("expected exactly one plan for " + $run)
    else .[0] | map({key: .address, value: .change.after}) | from_entries end;
  resources("accepts_kubernetes_empty_list_encodings") as $initial |
  resources("retains_the_pod_template_within_one_build") as $repeated |
  resources("rotates_runtime_credentials_for_a_new_build") as $next |
  "kubernetes_deployment_v1.workspace[0]" as $deployment |
  "module.coder_snapshots.kubernetes_persistent_volume_claim_v1.home" as $module_home |
  "kubernetes_persistent_volume_claim_v1.home" as $legacy_home |
  ($initial[$module_home] // $initial[$legacy_home]) as $home_init |
  ($repeated[$module_home] // $repeated[$legacy_home]) as $home_rep |
  ($next[$module_home] // $next[$legacy_home]) as $home_next |
  ($initial[$deployment].spec[0].template == $repeated[$deployment].spec[0].template) and
  ($initial[$deployment].spec[0].template != $next[$deployment].spec[0].template) and
  ($initial[$deployment].spec[0].strategy[0].type == "Recreate") and
  ($next[$deployment].spec[0].strategy[0].type == "Recreate") and
  ($home_init == $home_rep and $home_init == $home_next)
' "${report}" >/dev/null || {
  printf '%s\n' 'A new workspace build must replace the Pod while retaining its home volume; the same build must keep its Pod template.' >&2
  exit 1
}
