#!/usr/bin/env bash
# Verify Prowler CronJob compliance framework configuration.

set -euo pipefail

yq="${1:?missing yq path}"
cronjob_manifest="${2:?missing cronjob manifest path}"

if [[ ! -f ${cronjob_manifest} ]]; then
  echo "Error: CronJob manifest not found: ${cronjob_manifest}" >&2
  exit 1
fi

compliance_arg="$("${yq}" e '.spec.jobTemplate.spec.template.spec.containers[] | select(.name == "prowler") | .command[]' "${cronjob_manifest}" | grep -A 1 -- "--compliance" | tail -n 1)"

if [[ ${compliance_arg} != "cis_3.0_aws" ]]; then
  echo "Error: expected compliance framework 'cis_3.0_aws', got '${compliance_arg}'" >&2
  exit 1
fi

echo "Prowler compliance framework verified: ${compliance_arg}"
exit 0
