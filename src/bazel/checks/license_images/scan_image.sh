#!/usr/bin/env bash
# Scan the OCI layout of one digest-pinned container base image for package licenses with Trivy.

set -euo pipefail

trivy="${1:?missing trivy path}"
jq="${2:?missing jq path}"
image="${3:?missing image repository}"
layout="${4:?missing OCI layout directory}"
report="${5:?missing report path}"

# The layout indexes exactly the pinned image manifest.
digest="$("${jq}" -er '
  .manifests
  | select(length == 1)
  | .[0].digest
  | select(test("^sha256:[0-9a-f]{64}$"))
' "${layout}/index.json")"

work="$(mktemp -d)"
trap 'rm -rf -- "${work}"' EXIT

# Trivy rejects symlinked layout blobs, and the sandbox links every input.
scanned="${work}/layout"
cp -RL -- "${layout}" "${scanned}"

HOME="${work}" "${trivy}" image \
  --cache-dir "${work}/cache" \
  --disable-telemetry \
  --format json \
  --input "${scanned}" \
  --offline-scan \
  --quiet \
  --scanners license \
  --skip-version-check \
  --output "${work}/report.json"

# Trivy names a local layout by its path. Naming it by the pinned reference
# gives the report the identity a registry scan of the same image reports,
# and dropping the scan time and report ID makes it a function of the image.
# shellcheck disable=SC2016 # $scanned and $ref are jq variables.
"${jq}" --arg scanned "${scanned}" --arg ref "${image}@${digest}" '
  del(.CreatedAt, .ReportID)
  | .ArtifactName = $ref
  | .Results |= map(
      if .Target == $scanned then .Target = $ref
      elif (.Target | startswith($scanned + " ")) then .Target = $ref + (.Target | ltrimstr($scanned))
      else .
      end
    )
' "${work}/report.json" >"${report}"
