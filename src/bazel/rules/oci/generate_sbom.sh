#!/usr/bin/env bash
# Generate and attach SPDX software bill of materials (SBOM) to container images using Syft and Crane.

set -euo pipefail

if (($# < 1 || $# > 2)); then
  printf '%s\n' 'usage: generate_sbom.sh <stream-tag> [artifact-manifest]' >&2
  exit 2
fi

stream_tag="$1"
artifact_manifest="${2:-${RELEASE_ARTIFACTS_FILE:-}}"
root="$(git rev-parse --show-toplevel)"
exceptions="${root}/src/bazel/checks/license_images/exceptions.yaml"
trivy_ignore="${root}/src/bazel/checks/trivy_source/trivyignore.yaml"
syft_binary="${SYFT_BINARY:-syft}"
oras_binary="${ORAS_BINARY:-oras}"
trivy_binary="${TRIVY_BINARY:-trivy}"

if [[ ! ${stream_tag} =~ ^[0-9]{8}T[0-9]{6}Z_[0-9a-f]{12}$ ]]; then
  printf 'Invalid stream tag: %s\n' "${stream_tag}" >&2
  exit 2
fi
if [[ -z ${artifact_manifest} ]]; then
  printf '%s\n' \
    'RELEASE_ARTIFACTS_FILE is required: shipped OCI subjects are never inferred.' >&2
  exit 2
fi
if [[ ! -f ${artifact_manifest} || -L ${artifact_manifest} ]]; then
  printf 'Artifact manifest is missing or unsafe: %s\n' "${artifact_manifest}" >&2
  exit 2
fi

command -v jq >/dev/null
command -v "${oras_binary}" >/dev/null
command -v "${syft_binary}" >/dev/null
command -v "${trivy_binary}" >/dev/null
command -v yq >/dev/null

jq -e '
  .schemaVersion == 1 and
  (.artifacts | type == "array" and length > 0) and
  ((.artifacts | map(.name) | unique | length) == (.artifacts | length)) and
  ((.artifacts | map([.origin_image_repository, .subject_digest]) | unique | length) == (.artifacts | length)) and
  all(
    .artifacts[];
    (keys | sort) == ["name", "origin_image_repository", "sbom_attachment", "subject_digest"] and
    (.name | test("^[a-z0-9][a-z0-9-]*$")) and
    (.origin_image_repository | test("^[a-z0-9][a-z0-9.-]*(?::[0-9]+)?/[a-z0-9._/-]+$")) and
    ((.origin_image_repository | contains("@")) | not) and
    (.subject_digest | test("^sha256:[a-f0-9]{64}$")) and
    (.sbom_attachment | type == "object") and
    ((.sbom_attachment | keys | sort) == [
      "authentication",
      "credential_material",
      "credential_secret_output",
      "protocol",
      "referrer_repository",
      "requires_artifact_type",
      "requires_subject_digest",
      "subject_repository",
      "writer_principals"
    ]) and
    (.sbom_attachment.subject_repository == .origin_image_repository) and
    (.sbom_attachment.referrer_repository == .origin_image_repository) and
    (.sbom_attachment.protocol == "oci-referrers-v1.1") and
    (.sbom_attachment.requires_subject_digest == true) and
    (.sbom_attachment.requires_artifact_type == true) and
    (.sbom_attachment.credential_material == "ambient-ci-identity") and
    (.sbom_attachment.credential_secret_output == null) and
    (.sbom_attachment.authentication | type == "string" and length > 0) and
    (.sbom_attachment.writer_principals | type == "array" and length > 0) and
    all(.sbom_attachment.writer_principals[]; type == "string" and length > 0)
  )
' "${artifact_manifest}" >/dev/null || {
  printf '%s\n' \
    'Artifact manifest must contain digest-pinned active-origin OCI handoffs with no credential output.' >&2
  exit 2
}

output_dir="${RELEASE_SBOM_OUTPUT_DIR:-${root}/.tmp/artifacts/releases/${stream_tag}}"
mkdir -p "${output_dir}"
cp "${exceptions}" "${output_dir}/license-exceptions.yaml"
exception_layers=("${output_dir}/license-exceptions.yaml:application/yaml")
while IFS=$'\t' read -r evidence expected_sha256; do
  evidence_path="${root}/${evidence}"
  [[ -f ${evidence_path} && ! -L ${evidence_path} ]] || {
    printf 'License exception evidence is missing or unsafe: %s\n' "${evidence}" >&2
    exit 1
  }
  actual_sha256="$(shasum -a 256 "${evidence_path}" | cut -d ' ' -f 1)"
  [[ ${actual_sha256} == "${expected_sha256}" ]] || {
    printf 'License exception evidence hash mismatch: %s\n' "${evidence}" >&2
    exit 1
  }
  evidence_copy="${output_dir}/$(basename "${evidence}")"
  cp "${evidence_path}" "${evidence_copy}"
  exception_layers+=("${evidence_copy}:application/json")
done < <(yq -r '.exceptions[] | select(has("evidence")) | [.evidence, .evidence_sha256] | @tsv' "${exceptions}" || true)

while IFS= read -r artifact; do
  name="$(jq -r '.name' <<<"${artifact}")"
  repository="$(jq -r '.origin_image_repository' <<<"${artifact}")"
  digest="$(jq -r '.subject_digest' <<<"${artifact}")"
  subject="${repository}@${digest}"
  manifest="${output_dir}/${name}-${stream_tag}.oci-manifest.json"
  receipt="${output_dir}/${name}-${stream_tag}.sbom-attachment.json"
  sbom_layers=()

  "${oras_binary}" manifest fetch "${subject}" >"${manifest}"
  jq -e '
    .mediaType == "application/vnd.oci.image.index.v1+json" or
    .mediaType == "application/vnd.docker.distribution.manifest.list.v2+json" or
    .mediaType == "application/vnd.oci.image.manifest.v1+json" or
    .mediaType == "application/vnd.docker.distribution.manifest.v2+json"
  ' "${manifest}" >/dev/null || {
    printf 'Unsupported OCI subject manifest for %s.\n' "${name}" >&2
    exit 1
  }

  if jq -e '.manifests != null' "${manifest}" >/dev/null; then
    jq -e '
      (.manifests | type == "array" and length > 0) and
      all(
        .manifests[];
        (.digest | test("^sha256:[a-f0-9]{64}$")) and
        (.platform.os | test("^[a-z0-9_]+$")) and
        (.platform.architecture | test("^[a-z0-9_]+$")) and
        .platform.os != "unknown" and
        .platform.architecture != "unknown" and
        ((.platform.variant? // "") | test("^[a-z0-9_.-]*$"))
      )
    ' "${manifest}" >/dev/null || {
      printf 'OCI index has an unscannable platform descriptor for %s.\n' "${name}" >&2
      exit 1
    }
  else
    jq -n --arg digest "${digest}" \
      '{manifests: [{digest: $digest, platform: {os: "image", architecture: "single"}}]}' \
      >"${manifest}.platforms"
    manifest="${manifest}.platforms"
  fi

  while IFS= read -r platform_manifest; do
    platform_digest="$(jq -r '.digest' <<<"${platform_manifest}")"
    platform="$(jq -r '.platform | .os + "-" + .architecture + (if .variant then "-" + .variant else "" end)' <<<"${platform_manifest}")"
    sbom="${output_dir}/${name}-${platform}-${stream_tag}.spdx.json"
    "${syft_binary}" "registry:${repository}@${platform_digest}" \
      --source-name "${name}-${platform}" \
      --source-version "${stream_tag}" \
      --output "spdx-json=${sbom}"
    jq -e '
      .spdxVersion == "SPDX-2.3" and
      (.packages | type == "array" and length > 0)
    ' "${sbom}" >/dev/null
    sbom_layers+=("${sbom}:application/spdx+json")

    trivy_args=(
      image
      --severity
      "HIGH,CRITICAL"
      --scanners
      vuln
      --exit-code
      1
    )
    if [[ -f ${trivy_ignore} ]]; then
      trivy_args+=(--ignorefile "${trivy_ignore}")
    fi
    "${trivy_binary}" "${trivy_args[@]}" "${repository}@${platform_digest}"
  done < <(jq -c '.manifests[]' "${manifest}" || true)

  "${oras_binary}" attach \
    --artifact-type application/spdx+json \
    --annotation "org.opencontainers.image.version=${stream_tag}" \
    --annotation "org.opencontainers.image.title=${name} SBOM" \
    --format json \
    "${subject}" \
    "${sbom_layers[@]}" \
    "${exception_layers[@]}" \
    >"${receipt}"
  jq -e '.digest | test("^sha256:[a-f0-9]{64}$")' "${receipt}" >/dev/null
done < <(jq -c '.artifacts[]' "${artifact_manifest}" || true)
