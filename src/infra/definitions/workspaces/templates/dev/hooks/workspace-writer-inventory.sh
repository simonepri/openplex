#!/bin/sh
# Queries Kubernetes pod inventories to detect active volume writers and enforce single-writer volume isolation.

set -eu

kubeconfig=${1:-}
namespace=${2:-}
owner_id=${3:-}
context=${4:-}

case "${kubeconfig}" in
  /*) ;;
  *)
    printf '%s\n' 'Workspace writer inventory requires an absolute kubeconfig path' >&2
    exit 1
    ;;
esac
if [ ! -r "${kubeconfig}" ]; then
  printf '%s\n' 'Workspace writer inventory kubeconfig is not readable' >&2
  exit 1
fi
if ! printf '%s\n' "${namespace}" | grep -Eq '^[a-z][a-z0-9-]{1,61}[a-z0-9]$'; then
  printf '%s\n' 'Workspace writer inventory namespace is invalid' >&2
  exit 1
fi
if ! printf '%s\n' "${owner_id}" \
  | grep -Eq '^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'; then
  printf '%s\n' 'Workspace writer inventory owner UUID is invalid' >&2
  exit 1
fi
if ! printf '%s\n' "${context}" | grep -Eq '^cell-[a-z0-9]([-a-z0-9]*[a-z0-9])?$'; then
  printf '%s\n' 'Workspace writer inventory context is invalid' >&2
  exit 1
fi

context_kubeconfig="$(dirname -- "${kubeconfig}")/contexts/${context}"
if [ -d "$(dirname -- "${context_kubeconfig}")" ]; then
  if [ ! -r "${context_kubeconfig}" ]; then
    printf '%s\n' 'Workspace writer inventory selected context kubeconfig is not readable' >&2
    exit 1
  fi
else
  context_kubeconfig=${kubeconfig}
fi

kubeconfig_value() {
  key=$1
  value=$(sed -n "s/^[[:space:]]*\"${key}\": \"\([^\"]*\)\"$/\\1/p" "${context_kubeconfig}")
  value_lines=$(printf '%s\n' "${value}" | wc -l | tr -d ' ')
  if [ -z "${value}" ] || [ "${value_lines}" -ne 1 ]; then
    printf 'Workspace writer inventory requires one %s kubeconfig value\n' "${key}" >&2
    exit 1
  fi
  printf '%s\n' "${value}"
}

configured_context=$(kubeconfig_value current-context)
configured_cluster=$(kubeconfig_value cluster)
configured_user=$(kubeconfig_value user)
if [ "${configured_context}" != "${context}" ] || [ "${configured_cluster}" != "${context}" ]; then
  printf '%s\n' 'Workspace writer inventory selected kubeconfig does not match its context' >&2
  exit 1
fi
context_name_count=$(grep -Fc -- "\"name\": \"${context}\"" "${context_kubeconfig}" || true)
user_name_count=$(grep -Fc -- "\"name\": \"${configured_user}\"" "${context_kubeconfig}" || true)
if [ "${context_name_count}" -ne 2 ] || [ "${user_name_count}" -ne 1 ]; then
  printf '%s\n' 'Workspace writer inventory selected kubeconfig has ambiguous context links' >&2
  exit 1
fi

server=$(kubeconfig_value server)
case "${server}" in
  https://*/* | https://*) ;;
  *)
    printf '%s\n' 'Workspace writer inventory Kubernetes server must use HTTPS' >&2
    exit 1
    ;;
esac
server=${server%/}
certificate_authority=$(kubeconfig_value certificate-authority-data)
client_certificate=$(sed -n 's/^[[:space:]]*"client-certificate-data": "\([^"]*\)"$/\1/p' "${context_kubeconfig}")
client_key=$(sed -n 's/^[[:space:]]*"client-key-data": "\([^"]*\)"$/\1/p' "${context_kubeconfig}")
exec_command=$(sed -n 's/^[[:space:]]*"command": "\([^"]*\)"$/\1/p' "${context_kubeconfig}")
bearer_token=$(sed -n 's/^[[:space:]]*"token": "\([^"]*\)"$/\1/p' "${context_kubeconfig}")

auth_methods=0
if [ -n "${client_certificate}${client_key}" ]; then
  auth_methods=$((auth_methods + 1))
fi
if [ -n "${exec_command}" ]; then
  auth_methods=$((auth_methods + 1))
fi
if [ -n "${bearer_token}" ]; then
  auth_methods=$((auth_methods + 1))
fi

if [ "${auth_methods}" -gt 1 ]; then
  printf '%s\n' 'Workspace writer inventory kubeconfig must use exactly one authentication method' >&2
  exit 1
fi
if [ "${auth_methods}" -eq 0 ]; then
  printf '%s\n' 'Workspace writer inventory kubeconfig has no supported authentication method' >&2
  exit 1
fi
if [ -n "${client_certificate}${client_key}" ]; then
  if [ -z "${client_certificate}" ] || [ -z "${client_key}" ]; then
    printf '%s\n' 'Workspace writer inventory client certificate authentication is incomplete' >&2
    exit 1
  fi
fi
if [ -n "${exec_command}" ]; then
  case "${exec_command}" in
    /*) ;;
    *)
      printf '%s\n' 'Workspace writer inventory exec command must be absolute' >&2
      exit 1
      ;;
  esac
fi
if [ -n "${bearer_token}" ]; then
  bearer_token_lines=$(printf '%s\n' "${bearer_token}" | wc -l | tr -d ' ')
  if [ "${bearer_token_lines}" -ne 1 ] || ! printf '%s\n' "${bearer_token}" | grep -Eq '^[A-Za-z0-9._~+/=-]+$'; then
    printf '%s\n' 'Workspace writer inventory bearer token is invalid' >&2
    exit 1
  fi
fi

runtime=$(mktemp -d "${TMPDIR:-/tmp}/workspace-writer-inventory.XXXXXX")
trap 'rm -rf "$runtime"' EXIT
printf '%s' "${certificate_authority}" | openssl base64 -d -A >"${runtime}/ca.crt"

set --
if [ -n "${exec_command}" ]; then
  sed -n 's/^[[:space:]]*- "\([^"]*\)"$/\1/p' "${context_kubeconfig}" >"${runtime}/exec-arguments"
  if [ ! -s "${runtime}/exec-arguments" ]; then
    printf '%s\n' 'Workspace writer inventory exec authentication requires arguments' >&2
    exit 1
  fi
  while IFS= read -r argument; do
    set -- "$@" "${argument}"
  done <"${runtime}/exec-arguments"

  if ! awk '
    /^[[:space:]]*"env":$/ { in_environment = 1; next }
    in_environment && /^[[:space:]]*- "name": "[^"]+"$/ {
      name = $0
      sub(/^[[:space:]]*- "name": "/, "", name)
      sub(/"$/, "", name)
      next
    }
    in_environment && /^[[:space:]]*"value": "[^"]*"$/ {
      if (name == "") exit 2
      value = $0
      sub(/^[[:space:]]*"value": "/, "", value)
      sub(/"$/, "", value)
      print name "\t" value
      name = ""
      next
    }
    END { if (name != "") exit 2 }
  ' "${context_kubeconfig}" >"${runtime}/exec-environment"; then
    printf '%s\n' 'Workspace writer inventory exec environment is invalid' >&2
    exit 1
  fi
  while IFS="$(printf '\t')" read -r environment_name environment_value; do
    [ -n "${environment_name}" ] || continue
    if ! printf '%s\n' "${environment_name}" | grep -Eq '^[A-Z][A-Z0-9_]*$'; then
      printf '%s\n' 'Workspace writer inventory exec environment name is invalid' >&2
      exit 1
    fi
    export "${environment_name}=${environment_value}"
  done <"${runtime}/exec-environment"

  credential=$("${exec_command}" "$@")
  token=$(printf '%s' "${credential}" | tr -d '\r\n' \
    | sed -n 's/.*"token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
  if ! printf '%s\n' "${token}" | grep -Eq '^[A-Za-z0-9._~+/=-]+$'; then
    printf '%s\n' 'Workspace writer inventory exec authentication returned an invalid token' >&2
    exit 1
  fi
elif [ -n "${bearer_token}" ]; then
  printf 'header = "Authorization: Bearer %s"\n' "${bearer_token}" >"${runtime}/token.config"
  chmod 0600 "${runtime}/token.config"
else
  printf '%s' "${client_certificate}" | openssl base64 -d -A >"${runtime}/client.crt"
  printf '%s' "${client_key}" | openssl base64 -d -A >"${runtime}/client.key"
fi

label_selector="app.kubernetes.io/name=coder-workspace,com.coder.user.id=${owner_id}"
deployments=$(
  set -- --silent --show-error --fail --max-time 15 \
    --cacert "${runtime}/ca.crt"
  if [ -n "${exec_command}" ]; then
    set -- "$@" --header "Authorization: Bearer ${token}"
  elif [ -n "${bearer_token}" ]; then
    set -- "$@" --config "${runtime}/token.config"
  else
    set -- "$@" --cert "${runtime}/client.crt" --key "${runtime}/client.key"
  fi
  curl "$@" \
    --get \
    --data-urlencode "labelSelector=${label_selector}" \
    "${server}/apis/apps/v1/namespaces/${namespace}/deployments"
)
pods=$(
  set -- --silent --show-error --fail --max-time 15 \
    --cacert "${runtime}/ca.crt"
  if [ -n "${exec_command}" ]; then
    set -- "$@" --header "Authorization: Bearer ${token}"
  elif [ -n "${bearer_token}" ]; then
    set -- "$@" --config "${runtime}/token.config"
  else
    set -- "$@" --cert "${runtime}/client.crt" --key "${runtime}/client.key"
  fi
  curl "$@" \
    --header 'Accept: application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1' \
    --get \
    --data-urlencode "labelSelector=${label_selector}" \
    --data-urlencode 'fieldSelector=status.phase!=Succeeded,status.phase!=Failed' \
    "${server}/api/v1/namespaces/${namespace}/pods"
)

deployments_base64=$(printf '%s' "${deployments}" | openssl base64 -A)
pods_base64=$(printf '%s' "${pods}" | openssl base64 -A)
printf '{"deployments":"%s","pods":"%s"}\n' "${deployments_base64}" "${pods_base64}"
