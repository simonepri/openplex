#!/usr/bin/env bash
# Test release SBOM generation pipeline for digest consistency, credential safety, and license inclusion.

set -euo pipefail

jq_bin="${1:?jq path was not supplied}"
yq_bin="${2:?yq path was not supplied}"
root="$(git rev-parse --show-toplevel)"
workspace="$(mktemp -d)"
trap 'rm -rf "$workspace"' EXIT
fake_bin="${workspace}/bin"
artifacts="${workspace}/artifacts.json"
invalid="${workspace}/invalid.json"
output="${workspace}/output"
log="${workspace}/tools.log"
mkdir -p "${fake_bin}"
cp "${jq_bin}" "${fake_bin}/jq"
cp "${yq_bin}" "${fake_bin}/yq"
export PATH="${fake_bin}:${PATH}"

cat >"${fake_bin}/syft" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'syft %s\n' "$*" >>"${RELEASE_SBOM_TEST_LOG:?}"
for argument in "$@"; do
  case "$argument" in
  spdx-json=*) output="${argument#spdx-json=}" ;;
  esac
done
printf '%s\n' '{"spdxVersion":"SPDX-2.3","packages":[{"name":"fixture"}]}' >"${output:?}"
EOF
cat >"${fake_bin}/oras" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'oras %s\n' "$*" >>"${RELEASE_SBOM_TEST_LOG:?}"
if [[ "$1 $2" == "manifest fetch" ]]; then
	printf '%s\n' '{
  "mediaType": "application/vnd.oci.image.index.v1+json",
  "manifests": [
    {"digest": "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc", "platform": {"os": "linux", "architecture": "amd64"}},
    {"digest": "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd", "platform": {"os": "linux", "architecture": "arm64"}}
  ]
}'
	exit 0
fi
printf '%s\n' '{"digest":"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}'
EOF
cat >"${fake_bin}/trivy" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'trivy %s\n' "$*" >>"${RELEASE_SBOM_TEST_LOG:?}"
EOF
chmod +x "${fake_bin}/syft" "${fake_bin}/oras" "${fake_bin}/trivy"

jq -n '{
  schemaVersion: 1,
  artifacts: [{
    name: "ray-data",
    origin_image_repository: "111122223333.dkr.ecr.us-west-2.amazonaws.com/ray-data",
    subject_digest: ("sha256:" + ("a" * 64)),
    sbom_attachment: {
      authentication: "aws-ecr",
      credential_material: "ambient-ci-identity",
      credential_secret_output: null,
      protocol: "oci-referrers-v1.1",
      referrer_repository: "111122223333.dkr.ecr.us-west-2.amazonaws.com/ray-data",
      requires_artifact_type: true,
      requires_subject_digest: true,
      subject_repository: "111122223333.dkr.ecr.us-west-2.amazonaws.com/ray-data",
      writer_principals: ["arn:aws:iam::111122223333:role/release"]
    }
  }]
}' >"${artifacts}"

RELEASE_ARTIFACTS_FILE="${artifacts}" \
  RELEASE_SBOM_OUTPUT_DIR="${output}" \
  RELEASE_SBOM_TEST_LOG="${log}" \
  SYFT_BINARY="${fake_bin}/syft" \
  ORAS_BINARY="${fake_bin}/oras" \
  TRIVY_BINARY="${fake_bin}/trivy" \
  bash "${root}/src/bazel/rules/oci/generate_sbom.sh" \
  20260830T010203Z_0123456789ab

jq -e '.spdxVersion == "SPDX-2.3"' \
  "${output}/ray-data-linux-amd64-20260830T010203Z_0123456789ab.spdx.json" >/dev/null
jq -e '.spdxVersion == "SPDX-2.3"' \
  "${output}/ray-data-linux-arm64-20260830T010203Z_0123456789ab.spdx.json" >/dev/null
jq -e '.digest == "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"' \
  "${output}/ray-data-20260830T010203Z_0123456789ab.sbom-attachment.json" >/dev/null
cmp "${root}/src/bazel/checks/license_images/exceptions.yaml" "${output}/license-exceptions.yaml"
cmp "${root}/src/bazel/checks/license_images/ray-base-exception.json" "${output}/ray-base-exception.json"
grep -Fq \
  'registry:111122223333.dkr.ecr.us-west-2.amazonaws.com/ray-data@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc' \
  "${log}"
grep -Fq \
  'registry:111122223333.dkr.ecr.us-west-2.amazonaws.com/ray-data@sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd' \
  "${log}"
grep -Fq \
  'trivy image --severity HIGH,CRITICAL --scanners vuln --exit-code 1' \
  "${log}"
grep -Fq \
  '111122223333.dkr.ecr.us-west-2.amazonaws.com/ray-data@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc' \
  "${log}"
grep -Fq \
  '111122223333.dkr.ecr.us-west-2.amazonaws.com/ray-data@sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd' \
  "${log}"
grep -Fq \
  'oras attach --artifact-type application/spdx+json' \
  "${log}"
grep -Fq \
  '111122223333.dkr.ecr.us-west-2.amazonaws.com/ray-data@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
  "${log}"
grep -Fq -- \
  '--annotation org.opencontainers.image.version=20260830T010203Z_0123456789ab' \
  "${log}"
grep -Fq 'license-exceptions.yaml:application/yaml' "${log}"
grep -Fq 'ray-base-exception.json:application/json' "${log}"

jq '.artifacts[0].sbom_attachment.credential_secret_output = "secret"' \
  "${artifacts}" >"${invalid}"
if RELEASE_SBOM_OUTPUT_DIR="${workspace}/invalid-output" \
  SYFT_BINARY="${fake_bin}/syft" ORAS_BINARY="${fake_bin}/oras" TRIVY_BINARY="${fake_bin}/trivy" \
  bash "${root}/src/bazel/rules/oci/generate_sbom.sh" \
  20260830T010203Z_0123456789ab "${invalid}"; then
  echo 'Credential-bearing SBOM attachment handoff unexpectedly passed.' >&2
  exit 1
fi
