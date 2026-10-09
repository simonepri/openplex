#!/usr/bin/env bash
# Verify Prowler CronJob compliance framework configuration.

set -euo pipefail

yq="${1:?missing yq path}"
cronjob_manifest="${2:?missing cronjob manifest path}"

if [[ ! -f ${cronjob_manifest} ]]; then
  echo "Error: CronJob manifest not found: ${cronjob_manifest}" >&2
  exit 1
fi

# 1. Verify compliance framework and output arguments
compliance_arg="$("${yq}" e '.spec.jobTemplate.spec.template.spec.containers[] | select(.name == "prowler") | .args[]' "${cronjob_manifest}" | grep -A 1 -- "--compliance" | tail -n 1)"

if [[ ${compliance_arg} != "cis_3.0_aws" ]]; then
  echo "Error: expected compliance framework 'cis_3.0_aws', got '${compliance_arg}'" >&2
  exit 1
fi

output_format_arg="$("${yq}" e '.spec.jobTemplate.spec.template.spec.containers[] | select(.name == "prowler") | .args[]' "${cronjob_manifest}" | grep -A 1 -- "-M" | tail -n 1)"

if [[ ${output_format_arg} != "json-ocsf" ]]; then
  echo "Error: expected output format 'json-ocsf', got '${output_format_arg}'" >&2
  exit 1
fi

if ! "${yq}" e '.spec.jobTemplate.spec.template.spec.containers[] | select(.name == "prowler") | .args[]' "${cronjob_manifest}" | grep -Fxq -- "--ignore-exit-code-3"; then
  echo "Error: expected --ignore-exit-code-3 so failed checks do not fail the Job" >&2
  exit 1
fi

mutelist_arg="$("${yq}" e '.spec.jobTemplate.spec.template.spec.containers[] | select(.name == "prowler") | .args[]' "${cronjob_manifest}" | grep -A 1 -- "--mutelist-file" | tail -n 1)"
if [[ ${mutelist_arg} != "/etc/prowler/mutelist.yaml" ]]; then
  echo "Error: expected mutelist file '/etc/prowler/mutelist.yaml', got '${mutelist_arg}'" >&2
  exit 1
fi

# 2. Verify wrapper shell structure and exit code preservation
command_entry="$("${yq}" e '.spec.jobTemplate.spec.template.spec.containers[] | select(.name == "prowler") | .command[0]' "${cronjob_manifest}")"
if [[ ${command_entry} != "/bin/sh" && ${command_entry} != "sh" ]]; then
  echo "Error: expected command entrypoint to be /bin/sh or sh, got '${command_entry}'" >&2
  exit 1
fi

script_content="$("${yq}" e '.spec.jobTemplate.spec.template.spec.containers[] | select(.name == "prowler") | .command[2]' "${cronjob_manifest}")"
# shellcheck disable=SC2016
if ! echo "${script_content}" | grep -F -q 'prowler "$@"'; then
  echo 'Error: wrapper script must invoke prowler with "$@"' >&2
  exit 1
fi

# shellcheck disable=SC2016
if ! echo "${script_content}" | grep -F -q 'exit $rc'; then
  echo "Error: wrapper script must preserve non-zero exit code on real prowler error" >&2
  exit 1
fi

if ! echo "${script_content}" | grep -F -q 'python3 -c'; then
  echo "Error: wrapper script must use python3 to emit OCSF findings as JSON lines" >&2
  exit 1
fi

# 3. Behavioral test: simulate successful scan and verify stdout JSON line emission & dashboard query matching
tmp_test_dir="$(mktemp -d)"
trap 'rm -rf "${tmp_test_dir}"' EXIT

mkdir -p "${tmp_test_dir}/bin" "${tmp_test_dir}/output"

# Create mock prowler that writes sample OCSF findings
cat <<'EOF' >"${tmp_test_dir}/bin/prowler"
#!/usr/bin/env bash
cat <<'JSON' > "${PROWLER_OUTPUT_DIR}/prowler-output.ocsf.json"
[
  {
    "activity_id": 1,
    "activity_name": "Create",
    "category_name": "Findings",
    "category_uid": 2,
    "class_name": "Detection Finding",
    "class_uid": 2004,
    "finding_info": {
      "desc": "Ensure IAM password policy requires minimum length of 14 or greater",
      "title": "iam_password_policy_minimum_length",
      "uid": "prowler-aws-iam_password_policy_minimum_length-400920695547-us-east-1"
    },
    "message": "IAM password policy does not require minimum length of 14 or greater",
    "metadata": {
      "event_code": "iam_password_policy_minimum_length",
      "product": {
        "name": "Prowler",
        "uid": "prowler",
        "version": "4.4.0"
      }
    },
    "severity": "Medium",
    "severity_id": 3,
    "status": "New",
    "status_code": "FAIL",
    "status_detail": "IAM password policy does not require minimum length of 14 or greater",
    "status_id": 1,
    "type_name": "Detection Finding: Create",
    "type_uid": 200401
  },
  {
    "activity_id": 1,
    "activity_name": "Create",
    "category_name": "Findings",
    "category_uid": 2,
    "class_name": "Detection Finding",
    "class_uid": 2004,
    "finding_info": {
      "desc": "Ensure root account has MFA enabled",
      "title": "iam_root_mfa_enabled",
      "uid": "prowler-aws-iam_root_mfa_enabled-400920695547-us-east-1"
    },
    "message": "Root account has MFA enabled",
    "metadata": {
      "event_code": "iam_root_mfa_enabled",
      "product": {
        "name": "Prowler",
        "uid": "prowler",
        "version": "4.4.0"
      }
    },
    "severity": "Critical",
    "severity_id": 5,
    "status": "New",
    "status_code": "PASS",
    "status_detail": "Root account has MFA enabled",
    "status_id": 1,
    "type_name": "Detection Finding: Create",
    "type_uid": 200401
  }
]
JSON
echo "Prowler 4.4.0 scan completed: 1 FAIL, 1 PASS"
exit 0
EOF
chmod +x "${tmp_test_dir}/bin/prowler"

PATH="${tmp_test_dir}/bin:${PATH}" PROWLER_OUTPUT_DIR="${tmp_test_dir}/output" /bin/sh -c "${script_content}" -- aws --compliance cis_3.0_aws -M json-ocsf --ignore-exit-code-3 >"${tmp_test_dir}/stdout.log"

# Verify stdout has valid JSON lines matching dashboard query expectations
python3 -c '
import json, sys

with open(sys.argv[1], "r") as f:
    lines = [line.strip() for line in f if line.strip()]

findings = []
for line in lines:
    if line.startswith("{") and line.endswith("}"):
        try:
            findings.append(json.loads(line))
        except Exception:
            pass

assert len(findings) == 2, f"Expected 2 JSON findings on stdout, found {len(findings)}"
assert findings[0]["status_code"] == "FAIL"
assert findings[0]["severity"] == "Medium"
assert findings[1]["status_code"] == "PASS"
assert findings[1]["severity"] == "Critical"

# Test dashboard query logic against finding 0
f0_raw = json.dumps(findings[0])
assert "\"status_code\":\"FAIL\"" in f0_raw or "\"status_code\": \"FAIL\"" in f0_raw, "Dashboard query body LIKE check must match"
assert "status_code" in findings[0], "Dashboard status_code filter must match"

print("Emitted JSON findings and dashboard query field matching verified successfully.")
' "${tmp_test_dir}/stdout.log"

# 4. Behavioral test: verify exit code semantics on error
cat <<'EOF' >"${tmp_test_dir}/bin/prowler"
#!/usr/bin/env bash
echo "Fatal: Invalid AWS credentials" >&2
exit 2
EOF
chmod +x "${tmp_test_dir}/bin/prowler"

set +e
PATH="${tmp_test_dir}/bin:${PATH}" PROWLER_OUTPUT_DIR="${tmp_test_dir}/output" /bin/sh -c "${script_content}" -- aws --compliance cis_3.0_aws -M json-ocsf --ignore-exit-code-3 >"${tmp_test_dir}/stdout_error.log" 2>&1
err_rc=$?
set -e

if [[ ${err_rc} -ne 2 ]]; then
  echo "Error: expected wrapper to exit with code 2 on real prowler error, got ${err_rc}" >&2
  exit 1
fi

echo "Exit code preservation verified (code 2)."
echo "Prowler compliance framework verified: ${compliance_arg}"
echo "Prowler output format verified: ${output_format_arg}"
exit 0
