#!/bin/sh
# Executes Ray compile-cache test stages in sequence to defend cross-process cache reuse, reader validation, and unavailable-cache fallback handling.

set -eu

readonly python=/home/ray/anaconda3/bin/python
readonly program=/opt/conformance/remote_cache.py

TORCHINDUCTOR_CACHE_DIR=/tmp/torchinductor-writer \
  "${python}" "${program}" writer
TORCHINDUCTOR_CACHE_DIR=/tmp/torchinductor-reader \
  "${python}" "${program}" reader
TORCHINDUCTOR_CACHE_DIR=/tmp/torchinductor-fallback \
  TORCHINDUCTOR_REDIS_URL='redis://127.0.0.1:1/0?socket_connect_timeout=1&socket_timeout=1' \
  "${python}" "${program}" fallback
