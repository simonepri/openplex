#!/usr/bin/env bash
# Verify that repository BUILD files match Gazelle generated definitions without drift.

set -euo pipefail

root="${BUILD_WORKSPACE_DIRECTORY:-$(git rev-parse --show-toplevel)}"
cd "${root}"

bazel_root="${BAZEL_OUTPUT_ROOT:-${root}/.tmp/state/bazel}"
# shellcheck disable=SC2086
bazel --output_user_root="${bazel_root}" run ${BAZEL_CONFIG_FLAGS:-} --ui_event_filters=-info,-stdout --noshow_progress //:gazelle -- --mode=diff
