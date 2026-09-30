#!/usr/bin/env bash
# shellcheck shell=bash
#
# Brings up the Dragonfly prepull unit, embedded into the userData shellscript MIME part.
# The prepull warms the dfdaemon image the moment containerd is up, so dfdaemon serves
# 127.0.0.1:4001 before the model image's first lazy resolve instead of losing that race and
# forcing a fallback pull.

set -euo pipefail

# The prepull unit mints ECR creds via the aws CLI; fail loud now if it is absent rather than
# silently losing the dfdaemon-before-first-resolve race on every boot.
command -v aws >/dev/null || {
  echo "dragonfly-prepull: aws CLI not found on PATH; required to mint ECR pull creds" >&2
  exit 1
}

# Pick up the write_files-placed unit before enabling it.
systemctl daemon-reload
# --no-block: the unit waits for containerd, which starts only after cloud-init finishes — a
# blocking start here would deadlock node boot.
systemctl enable dragonfly-prepull.service
systemctl start --no-block dragonfly-prepull.service
