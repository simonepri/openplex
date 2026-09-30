#!/usr/bin/env bash
# Verify that packaged Helm charts contain non-empty file assets at their expected archive paths.

set -euo pipefail

chart=$1
shift

entries=$(tar -tzf "${chart}")
if grep -Eq '/helm/files/' <<<"${entries}"; then
  echo "Helm file asset retained the source-tree helm/ prefix" >&2
  exit 1
fi

root=$(head -n 1 <<<"${entries}" | cut -d/ -f1)
for path in "$@"; do
  if [[ ${path} != files/* || ${path} == *../* ]]; then
    echo "invalid chart-relative file asset path: ${path}" >&2
    exit 1
  fi
  entry="${root}/${path}"
  count=$(grep -Fxc "${entry}" <<<"${entries}")
  if [[ ${count} -ne 1 ]]; then
    echo "expected exactly one archive entry ${entry}, found ${count}" >&2
    exit 1
  fi
  bytes=$(tar -xOzf "${chart}" "${entry}" | wc -c | tr -d ' ')
  if [[ ${bytes} -eq 0 ]]; then
    echo "Helm file asset is empty: ${entry}" >&2
    exit 1
  fi
done
