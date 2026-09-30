#!/bin/sh
# Runs Chainsaw synthetic smoke checks verifying binary readiness, cluster DNS resolution, and Kubernetes API health.

set -eu

case "${1:-}" in
  cli | chainsaw-smoke-cli)
    exec "$(dirname "$0")/run-chainsaw-smoke-cli.sh"
    ;;
  dns | chainsaw-smoke-dns)
    exec "$(dirname "$0")/run-chainsaw-smoke-dns.sh"
    ;;
  apiserver | chainsaw-smoke-apiserver)
    exec "$(dirname "$0")/run-chainsaw-smoke-apiserver.sh"
    ;;
  metrics | chainsaw-smoke-metrics)
    exec "$(dirname "$0")/run-chainsaw-smoke-metrics.sh"
    ;;
  *)
    "$(dirname "$0")/run-chainsaw-smoke-cli.sh"
    "$(dirname "$0")/run-chainsaw-smoke-dns.sh"
    "$(dirname "$0")/run-chainsaw-smoke-apiserver.sh"
    "$(dirname "$0")/run-chainsaw-smoke-metrics.sh"
    ;;
esac
