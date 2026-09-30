#!/usr/bin/env sh
# Renders Homer dashboard configuration from source template and runtime environment.

# shellcheck shell=sh
set -e

: "${ACCESS_DOMAIN:=""}"
: "${AWS_ACCOUNT_ID:=""}"
: "${AWS_REGION:=""}"
: "${CLUSTERS:=""}"
: "${CONFIG_SOURCE:="/config-source"}"
: "${CONFIG_RENDERED:="/config-rendered"}"
: "${PROVIDER:=""}"
: "${PUBLIC_DOMAIN:=""}"
: "${SSO_DOMAIN:="configured-by-applicationset.invalid"}"

cluster_count=$(printf '%s\n' "${CLUSTERS}" | wc -w)
if [ "${cluster_count}" -eq 1 ]; then
  VELERO_URL="https://velero-ui.${CLUSTERS}.${ACCESS_DOMAIN}"
else
  VELERO_URL=""
fi
export VELERO_URL

# shellcheck disable=SC2086
QUICK_YAML=$(for c in ${CLUSTERS}; do printf '\n          - name: "%s"\n            url: "https://velero-ui.%s.%s"\n            target: "_blank"' "${c}" "${c}" "${ACCESS_DOMAIN}"; done)
if [ -n "${QUICK_YAML}" ]; then
  QUICK_YAML="        quick:${QUICK_YAML}"
fi
export QUICK_YAML

case "${PROVIDER},${PUBLIC_DOMAIN},${ACCESS_DOMAIN}" in
  floci,* | *.localhost.floci.io,* | *local* | *floci*)
    REGISTRY_URL="https://floci.${PUBLIC_DOMAIN}"
    ;;
  gcp,*)
    REGISTRY_URL="https://console.cloud.google.com/artifacts"
    ;;
  *)
    if [ "${PROVIDER}" = "aws" ] || [ -n "${AWS_REGION}" ] || [ -n "${AWS_ACCOUNT_ID}" ]; then
      if [ -z "${SSO_DOMAIN}" ] || [ "${SSO_DOMAIN}" = "configured-by-applicationset.invalid" ]; then
        echo "Error: SSO_DOMAIN is required for AWS clusters in cloud mode (missing sso-domain annotation)" >&2
        exit 1
      fi
      if [ -n "${AWS_REGION}" ] && [ -n "${AWS_ACCOUNT_ID}" ]; then
        REGISTRY_URL="https://${SSO_DOMAIN}/start/#/console?account_id=${AWS_ACCOUNT_ID}&role_name=AdministratorAccess&destination=https%3A%2F%2F${AWS_REGION}.console.aws.amazon.com%2Fecr%2Fprivate-registry%2Frepositories%3Fregion%3D${AWS_REGION}"
      elif [ -n "${AWS_REGION}" ]; then
        REGISTRY_URL="https://${AWS_REGION}.console.aws.amazon.com/ecr/private-registry/repositories?region=${AWS_REGION}"
      elif [ "${PROVIDER}" = "aws" ]; then
        REGISTRY_URL="https://console.aws.amazon.com/ecr/repositories"
      fi
    else
      REGISTRY_URL=""
    fi
    ;;
esac
export REGISTRY_URL

sed -e "s|https://coder.invalid|https://coder.${PUBLIC_DOMAIN}|g;s|https://dragonfly.invalid|https://dragonfly.${PUBLIC_DOMAIN}|g;s|https://kargo.invalid|https://kargo.${PUBLIC_DOMAIN}|g;s|https://headlamp.invalid|https://headlamp.${PUBLIC_DOMAIN}|g;s|https://opencost.invalid|https://opencost.${PUBLIC_DOMAIN}|g" \
  -e "s|https://argocd.invalid|https://argocd.${PUBLIC_DOMAIN}|g;s|https://atlantis.invalid|https://atlantis.${PUBLIC_DOMAIN}|g;s|https://signoz.invalid|https://signoz.${PUBLIC_DOMAIN}|g;s|https://parca.invalid|https://parca.${PUBLIC_DOMAIN}|g" \
  "${CONFIG_SOURCE}/config.yml" | awk '
      BEGIN {
        reg_url = ENVIRON["REGISTRY_URL"]
        gsub(/&/, "\\\\&", reg_url)
      }
      /      - name: "Registry"/ {
        in_registry = 1
        registry_block = $0 "\n"
        next
      }
      in_registry {
        registry_block = registry_block $0 "\n"
        if ($0 ~ /target: "_blank"/) {
          in_registry = 0
          if (reg_url != "") {
            sub("#REGISTRY_URL#", reg_url, registry_block)
            printf "%s", registry_block
          }
        }
        next
      }
      /#VELERO_URL#/ { sub("#VELERO_URL#", ENVIRON["VELERO_URL"]) }
      /#REGISTRY_URL#/ { sub("#REGISTRY_URL#", reg_url) }
      /# VELERO_QUICK_LINKS/ { if (ENVIRON["QUICK_YAML"] != "") print ENVIRON["QUICK_YAML"]; next }
      { print }
    ' >"${CONFIG_RENDERED}/config.yml"
