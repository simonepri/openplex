#!/bin/sh
# Continuously watches template source repository and triggers template reconciler on change.

set -eu

: "${SOURCE_REPOSITORY:?SOURCE_REPOSITORY is required}"
: "${SOURCE_REVISION:=main}"
: "${WATCH_INTERVAL_SECONDS:=15}"
: "${WATCHER_HEARTBEAT_FILE:=/tmp/watcher-healthy}"

trap 'printf "Stopping template reconciler watcher loop...\n"; exit 0' TERM INT

last_commit=""

printf 'Starting template reconciler watcher loop (poll interval: %ss)...\n' "${WATCH_INTERVAL_SECONDS}"

while true; do
  touch "${WATCHER_HEARTBEAT_FILE}"
  current_commit="$(git ls-remote "${SOURCE_REPOSITORY}" "refs/heads/${SOURCE_REVISION}" 2>/dev/null | awk '{print $1}' || true)"

  if [ -n "${current_commit}" ] && [ "${current_commit}" != "${last_commit}" ]; then
    printf 'Detected new commit on %s (%s -> %s). Syncing repository...\n' \
      "${SOURCE_REVISION}" "${last_commit:-initial}" "${current_commit}"

    if [ -d /source/repository/.git ]; then
      git -C /source/repository fetch --depth=1 origin "${SOURCE_REVISION}"
      git -C /source/repository checkout --detach FETCH_HEAD
    else
      /program/prepare-template-source.sh
    fi

    # Trigger reconciliation
    printf 'Triggering template reconciliation...\n'
    if /program/template-reconciler.sh; then
      printf 'Template reconciliation successful for commit %s.\n' "${current_commit}"
      last_commit="${current_commit}"
    else
      printf 'Template reconciliation encountered an error. Will retry in %ss...\n' "${WATCH_INTERVAL_SECONDS}" >&2
    fi
  fi

  sleep "${WATCH_INTERVAL_SECONDS}"
done
