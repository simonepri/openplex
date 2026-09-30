#!/bin/sh
# Stages the template publisher token into memory tmpfs for reconciler execution.

set -eu

: "${CODER_TEMPLATE_PUBLISHER_TOKEN_SOURCE:?}"
: "${CODER_SESSION_TOKEN_FILE:?}"

if [ ! -f "${CODER_TEMPLATE_PUBLISHER_TOKEN_SOURCE}" ] \
  || [ -L "${CODER_TEMPLATE_PUBLISHER_TOKEN_SOURCE}" ]; then
  exit 1
fi
umask 077
cp "${CODER_TEMPLATE_PUBLISHER_TOKEN_SOURCE}" "${CODER_SESSION_TOKEN_FILE}"
[ -s "${CODER_SESSION_TOKEN_FILE}" ]
