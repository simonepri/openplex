#!/usr/bin/env bash
# Compute deterministic OCI image stream tags based on git commit metadata and branch lineage.

set -euo pipefail

if (($# > 1)); then
  printf '%s\n' 'Usage: stream_tag.sh [commit]' >&2
  exit 64
fi

commit="${1:-HEAD}"
if ! git rev-parse --verify --quiet "${commit}^{commit}" >/dev/null; then
  printf 'Not a commit: %s\n' "${commit}" >&2
  exit 1
fi

committer_date="$(TZ=UTC0 git show --no-patch --format='%cd' --date=format-local:%Y%m%dT%H%M%SZ "${commit}")"
sha="$(git rev-parse --verify "${commit}^{commit}" | cut -c1-12)"
stream_tag="${committer_date}_${sha}"

if [[ ! ${stream_tag} =~ ^[0-9]{8}T[0-9]{6}Z_[0-9a-f]{12}$ ]]; then
  printf 'Invalid stream tag: %s\n' "${stream_tag}" >&2
  exit 1
fi

printf '%s\n' "${stream_tag}"
