#!/bin/sh
# shellcheck disable=SC2310
# Monitors Kueue workload admission status and pod startup progression, surfacing scheduling delays or errors.

set -eu

: "${KUBERNETES_CONFIG_PATH:?Kubernetes config path is required}"
: "${WORKSPACE_CELL:?Workspace cell is required}"
: "${WORKSPACE_NAMESPACE:?Workspace namespace is required}"
: "${CODER_WORKSPACE_BUILD_ID:?Coder workspace build ID is required}"
: "${CODER_WORKSPACE_ID:?Coder workspace ID is required}"
CODER_WORKSPACE_NAME="${CODER_WORKSPACE_NAME:-workspace}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-2400}"

case "${KUBERNETES_CONFIG_PATH}" in
  /*) ;;
  *)
    printf '%s\n' 'Workspace admission probe requires an absolute kubeconfig path' >&2
    exit 1
    ;;
esac
if [ ! -r "${KUBERNETES_CONFIG_PATH}" ]; then
  printf '%s\n' 'Workspace admission probe kubeconfig is not readable' >&2
  exit 1
fi
if ! printf '%s\n' "${WORKSPACE_NAMESPACE}" | grep -Eq '^[a-z][a-z0-9-]{1,61}[a-z0-9]$'; then
  printf '%s\n' 'Workspace admission probe namespace is invalid' >&2
  exit 1
fi
if ! printf '%s\n' "${CODER_WORKSPACE_ID}" \
  | grep -Eq '^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'; then
  printf '%s\n' 'Workspace admission probe workspace UUID is invalid' >&2
  exit 1
fi
if ! printf '%s\n' "${CODER_WORKSPACE_BUILD_ID}" \
  | grep -Eq '^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'; then
  printf '%s\n' 'Workspace admission probe build UUID is invalid' >&2
  exit 1
fi
if ! printf '%s\n' "${WORKSPACE_CELL}" | grep -Eq '^cell-[a-z0-9]([-a-z0-9]*[a-z0-9])?$'; then
  printf '%s\n' 'Workspace admission probe context is invalid' >&2
  exit 1
fi
if ! printf '%s\n' "${TIMEOUT_SECONDS}" | grep -Eq '^[1-9][0-9]*$'; then
  printf '%s\n' 'Workspace admission probe timeout must be a positive integer' >&2
  exit 1
fi

context_kubeconfig="$(dirname -- "${KUBERNETES_CONFIG_PATH}")/contexts/${WORKSPACE_CELL}"
if [ -d "$(dirname -- "${context_kubeconfig}")" ]; then
  if [ ! -r "${context_kubeconfig}" ]; then
    printf '%s\n' 'Workspace admission probe selected context kubeconfig is not readable' >&2
    exit 1
  fi
else
  context_kubeconfig=${KUBERNETES_CONFIG_PATH}
fi

kubeconfig_value() {
  key=$1
  value=$(sed -n "s/^[[:space:]]*\"${key}\": \"\([^\"]*\)\"$/\\1/p" "${context_kubeconfig}")
  value_lines=$(printf '%s\n' "${value}" | wc -l | tr -d ' ')
  if [ -z "${value}" ] || [ "${value_lines}" -ne 1 ]; then
    printf 'Workspace admission probe requires one %s kubeconfig value\n' "${key}" >&2
    exit 1
  fi
  printf '%s\n' "${value}"
}

configured_context=$(kubeconfig_value current-context)
configured_cluster=$(kubeconfig_value cluster)
kubeconfig_value user >/dev/null
if [ "${configured_context}" != "${WORKSPACE_CELL}" ] || [ "${configured_cluster}" != "${WORKSPACE_CELL}" ]; then
  printf '%s\n' 'Workspace admission probe selected kubeconfig does not match its context' >&2
  exit 1
fi

server=$(kubeconfig_value server)
case "${server}" in
  https://*/* | https://*) ;;
  *)
    printf '%s\n' 'Workspace admission probe Kubernetes server must use HTTPS' >&2
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
  printf '%s\n' 'Workspace admission probe kubeconfig must use exactly one authentication method' >&2
  exit 1
fi
if [ "${auth_methods}" -eq 0 ]; then
  printf '%s\n' 'Workspace admission probe kubeconfig has no supported authentication method' >&2
  exit 1
fi
if [ -n "${client_certificate}${client_key}" ]; then
  if [ -z "${client_certificate}" ] || [ -z "${client_key}" ]; then
    printf '%s\n' 'Workspace admission probe client certificate authentication is incomplete' >&2
    exit 1
  fi
fi
if [ -n "${exec_command}" ]; then
  case "${exec_command}" in
    /*) ;;
    *)
      printf '%s\n' 'Workspace admission probe exec command must be absolute' >&2
      exit 1
      ;;
  esac
fi
if [ -n "${bearer_token}" ]; then
  bearer_token_lines=$(printf '%s\n' "${bearer_token}" | wc -l | tr -d ' ')
  if [ "${bearer_token_lines}" -ne 1 ] || ! printf '%s\n' "${bearer_token}" | grep -Eq '^[A-Za-z0-9._~+/=-]+$'; then
    printf '%s\n' 'Workspace admission probe bearer token is invalid' >&2
    exit 1
  fi
fi

runtime=$(mktemp -d "${TMPDIR:-/tmp}/workspace-admission-probe.XXXXXX")
trap 'rm -rf "$runtime"' EXIT
printf '%s' "${certificate_authority}" | openssl base64 -d -A >"${runtime}/ca.crt"

set --
if [ -n "${exec_command}" ]; then
  sed -n 's/^[[:space:]]*- "\([^"]*\)"$/\1/p' "${context_kubeconfig}" >"${runtime}/exec-arguments"
  if [ ! -s "${runtime}/exec-arguments" ]; then
    printf '%s\n' 'Workspace admission probe exec authentication requires arguments' >&2
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
    printf '%s\n' 'Workspace admission probe exec environment is invalid' >&2
    exit 1
  fi
  while IFS="$(printf '\t')" read -r environment_name environment_value; do
    [ -n "${environment_name}" ] || continue
    if ! printf '%s\n' "${environment_name}" | grep -Eq '^[A-Z][A-Z0-9_]*$'; then
      printf '%s\n' 'Workspace admission probe exec environment name is invalid' >&2
      exit 1
    fi
    export "${environment_name}=${environment_value}"
  done <"${runtime}/exec-environment"

  credential=$("${exec_command}" "$@")
  token=$(printf '%s' "${credential}" | tr -d '\r\n' \
    | sed -n 's/.*"token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
  if ! printf '%s\n' "${token}" | grep -Eq '^[A-Za-z0-9._~+/=-]+$'; then
    printf '%s\n' 'Workspace admission probe exec authentication returned an invalid token' >&2
    exit 1
  fi
  printf 'header = "Authorization: Bearer %s"\n' "${token}" >"${runtime}/token.config"
  chmod 0600 "${runtime}/token.config"
elif [ -n "${bearer_token}" ]; then
  printf 'header = "Authorization: Bearer %s"\n' "${bearer_token}" >"${runtime}/token.config"
  chmod 0600 "${runtime}/token.config"
else
  printf '%s' "${client_certificate}" | openssl base64 -d -A >"${runtime}/client.crt"
  printf '%s' "${client_key}" | openssl base64 -d -A >"${runtime}/client.key"
fi

# The API server pretty-prints JSON for curl's default user agent, and the
# parsers below expect the compact form every other client receives.
k8s_curl() {
  url=$1
  shift
  set -- "$@" --silent --show-error --max-time 10 --cacert "${runtime}/ca.crt" \
    --user-agent workspace-admission-probe
  if [ -n "${exec_command}" ] || [ -n "${bearer_token}" ]; then
    set -- "$@" --config "${runtime}/token.config"
  else
    set -- "$@" --cert "${runtime}/client.crt" --key "${runtime}/client.key"
  fi
  curl "$@" "${url}"
}

# Reports one Workload's queue, admission conditions, and the CPU and memory it
# asks for, as "key=value" lines. Splitting on braces isolates each JSON object
# onto its own line so conditions never bleed into one another.
workload_status() {
  tr '{' '\n' | awk '
    function cpu_millicores(text) {
      if (text == "") return 0
      if (substr(text, length(text)) == "m") return substr(text, 1, length(text) - 1) + 0
      return (text + 0) * 1000
    }
    function memory_bytes(text,   suffix, value) {
      if (text == "") return 0
      suffix = ""
      value = text
      while (value ~ /[A-Za-z]$/) {
        suffix = substr(value, length(value)) suffix
        value = substr(value, 1, length(value) - 1)
      }
      value = value + 0
      if (suffix == "Ki") return value * 1024
      if (suffix == "Mi") return value * 1048576
      if (suffix == "Gi") return value * 1073741824
      if (suffix == "Ti") return value * 1099511627776
      if (suffix == "K" || suffix == "k") return value * 1000
      if (suffix == "M") return value * 1000000
      if (suffix == "G") return value * 1000000000
      return value
    }
    function quoted(text, name,   pattern, found) {
      pattern = "\"" name "\":\"[^\"]*\""
      if (!match(text, pattern)) return ""
      found = substr(text, RSTART, RLENGTH)
      sub("\"" name "\":\"", "", found)
      sub("\"$", "", found)
      return found
    }
    previous ~ /"requests":$/ {
      cpu += cpu_millicores(quoted($0, "cpu"))
      memory += memory_bytes(quoted($0, "memory"))
    }
    /"queueName":/ { if (queue == "") queue = quoted($0, "queueName") }
    /"type":"Admitted"/ {
      admitted = quoted($0, "status")
      admitted_reason = quoted($0, "reason")
      admitted_message = quoted($0, "message")
    }
    /"type":"QuotaReserved"/ {
      qr_reason = quoted($0, "reason")
      qr_message = quoted($0, "message")
    }
    { previous = $0 }
    END {
      print "queue=" queue
      print "admitted=" admitted
      print "admitted_reason=" admitted_reason
      print "admitted_message=" admitted_message
      print "qr_reason=" qr_reason
      print "qr_message=" qr_message
      printf "requested=cpu=%dm memory=%dMi\n", cpu, memory / 1048576
    }
  '
}

# Reports why a workspace pod is not running yet, as "key=value" lines: its
# phase, an unschedulable verdict, and the container currently blocking it.
pod_blocker() {
  tr '{' '\n' | awk '
    function quoted(text, name,   pattern, found) {
      pattern = "\"" name "\"[[:space:]]*:[[:space:]]*\"[^\"]*\""
      if (!match(text, pattern)) return ""
      found = substr(text, RSTART, RLENGTH)
      sub(/^"[^"]*"[[:space:]]*:[[:space:]]*"/, "", found)
      sub(/"$/, "", found)
      return found
    }
    # Messages carry spaces and escaped quotes, so walk to the closing quote.
    function message(text,   rest, out, index_, character) {
      if (!match(text, /"message"[[:space:]]*:[[:space:]]*"/)) return ""
      rest = substr(text, RSTART + RLENGTH)
      out = ""
      for (index_ = 1; index_ <= length(rest); index_++) {
        character = substr(rest, index_, 1)
        if (character == "\\") {
          out = out character substr(rest, index_ + 1, 1)
          index_++
          continue
        }
        if (character == "\"") break
        out = out character
      }
      return substr(out, 1, 300)
    }
    /"phase"[[:space:]]*:/ { if (phase == "") phase = quoted($0, "phase") }
    /"name"[[:space:]]*:/ { name = quoted($0, "name") }
    /"type"[[:space:]]*:[[:space:]]*"PodScheduled"/ {
      if (quoted($0, "status") == "False") {
        schedule_reason = quoted($0, "reason")
        schedule_message = message($0)
      }
    }
    # A state object follows its key on the next line; lastState holds history.
    previous ~ /"lastState"[[:space:]]*:[[:space:]]*$/ { previous = $0; next }
    previous ~ /"state"[[:space:]]*:[[:space:]]*$/ {
      if ($0 ~ /^"waiting"[[:space:]]*:[[:space:]]*$/ && blocker == "") blocker = name
      previous = $0
      next
    }
    previous ~ /"waiting"[[:space:]]*:[[:space:]]*$/ {
      if (blocker != "" && blocker_reason == "") {
        blocker_reason = quoted($0, "reason")
        blocker_message = message($0)
      }
    }
    { previous = $0 }
    END {
      print "phase=" phase
      print "schedule_reason=" schedule_reason
      print "schedule_message=" schedule_message
      print "blocker=" blocker
      print "blocker_reason=" blocker_reason
      print "blocker_message=" blocker_message
    }
  '
}

# A failed build leaves its Deployment behind, and the gated pod would keep its
# place in the Kueue queue and take quota once admitted. Scaling to zero removes
# the pod and its Workload; the next start build restores the replica count.
release_workspace() {
  status=$?
  trap - EXIT
  if [ "${status}" -ne 0 ]; then
    if k8s_curl "${server}/apis/apps/v1/namespaces/${WORKSPACE_NAMESPACE}/deployments/coder-${CODER_WORKSPACE_ID}" \
      --fail --request PATCH --header 'Content-Type: application/merge-patch+json' \
      --data '{"spec":{"replicas":0}}' >/dev/null 2>&1; then
      printf '%s\n' '[INFO] Scaled the workspace deployment to zero so it leaves the Kueue queue.' >&2
    else
      printf '%s\n' '[WARN] Could not scale the workspace deployment to zero; it may keep its Kueue queue position.' >&2
    fi
  fi
  rm -rf "${runtime}"
  exit "${status}"
}
trap release_workspace EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

deadline=$(($(date +%s) + TIMEOUT_SECONDS))
started=$(date +%s)
progress_interval_seconds=15
next_progress=${started}
admission_confirmed=false
deployment_generation=
deployment_observed_generation=
deployment_updated_replicas=0
deployment_ready_replicas=0
deployment_available_replicas=0
workspace_container_running=false
workload_found=false
pod_uid=
queue=
requested=
effective_reason=
effective_message=

while :; do
  pods_json=$(
    k8s_curl "${server}/api/v1/namespaces/${WORKSPACE_NAMESPACE}/pods?labelSelector=com.coder.workspace.id%3D${CODER_WORKSPACE_ID}%2Ccom.coder.workspace.build.id%3D${CODER_WORKSPACE_BUILD_ID}" 2>/dev/null || true
  )
  pods_compact=$(printf '%s' "${pods_json}" | tr -d '[:space:]')
  workspace_container_running=false
  if printf '%s\n' "${pods_compact}" \
    | grep -Eq '"containerStatuses":\[[^]]*"name":"workspace"[^]]*"state":[{]"running":[{]'; then
    workspace_container_running=true
  fi

  if [ "${admission_confirmed}" = false ]; then
    # If a pod has been created and does not contain the admission scheduling
    # gate, Kueue has either admitted it or no scheduling gate was applied.
    if printf '%s\n' "${pods_compact}" | grep -q '"items":\[{' \
      && ! printf '%s\n' "${pods_compact}" | grep -q '"kueue.x-k8s.io/admission"'; then
      admission_confirmed=true
      printf '[INFO] Workspace pod admission confirmed (scheduling gate cleared).\n'
    fi
  fi

  if [ "${admission_confirmed}" = false ]; then
    # The pod names its Workload only after admission, too late to explain a
    # wait, so the build finds the Workload that the pod owns from the start.
    # The first UID in the pod list is the pod's own, ahead of its owners.
    pod_uid=$(printf '%s\n' "${pods_compact}" \
      | grep -o '"uid":"[^"]*"' | head -n 1 | sed 's/^"uid":"\(.*\)"$/\1/')
    if ! printf '%s\n' "${pod_uid}" | grep -Eq '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'; then
      pod_uid=
    fi

    workload_json=
    if [ -n "${pod_uid}" ]; then
      workload_json=$(
        k8s_curl "${server}/apis/kueue.x-k8s.io/v1beta2/namespaces/${WORKSPACE_NAMESPACE}/workloads" 2>/dev/null \
          | tr -d '\n' \
          | awk '{ gsub(/[{]"apiVersion":"kueue[.]x-k8s[.]io\/v1beta2","kind":"Workload",/, "\n&"); print }' \
          | grep -F "\"uid\":\"${pod_uid}\"" | head -n 1 || true
      )
    fi

    if [ -n "${workload_json}" ] && printf '%s\n' "${workload_json}" | grep -q '"kind":"Workload"'; then
      workload_found=true
      parsed=$(printf '%s\n' "${workload_json}" | workload_status)
      admitted=$(printf '%s\n' "${parsed}" | sed -n 's/^admitted=//p')
      admitted_reason=$(printf '%s\n' "${parsed}" | sed -n 's/^admitted_reason=//p')
      admitted_message=$(printf '%s\n' "${parsed}" | sed -n 's/^admitted_message=//p')
      qr_reason=$(printf '%s\n' "${parsed}" | sed -n 's/^qr_reason=//p')
      qr_message=$(printf '%s\n' "${parsed}" | sed -n 's/^qr_message=//p')
      queue=$(printf '%s\n' "${parsed}" | sed -n 's/^queue=//p')
      requested=$(printf '%s\n' "${parsed}" | sed -n 's/^requested=//p')

      if [ "${admitted}" = "True" ]; then
        admission_confirmed=true
        printf '[INFO] Kueue admitted workspace workload onto queue %s.\n' "${queue:-unknown}"
      fi

      # Surface non-transient rejection reasons immediately.
      effective_reason=${qr_reason:-${admitted_reason}}
      effective_message=${qr_message:-${admitted_message}}
      case "${effective_reason}" in
        NoReservation | ClusterQueueDoesNotExist | Misconfigured | Inadmissible | Suspended)
          printf '\n================================================================================\n' >&2
          printf '[ERROR] Kueue admission rejected workspace pod:\n' >&2
          printf '  Queue:   %s\n' "${queue:-unknown}" >&2
          printf '  Reason:  %s\n' "${effective_reason:-Rejected}" >&2
          printf '  Message: %s\n' "${effective_message:-Quota exhausted or queue unavailable}" >&2
          printf '================================================================================\n\n' >&2
          exit 1
          ;;
        *) ;;
      esac

      if [ "${admission_confirmed}" = false ]; then
        progress_now=$(date +%s)
        if [ "${progress_now}" -ge "${next_progress}" ]; then
          next_progress=$((progress_now + progress_interval_seconds))
          printf '[INFO] Waiting for Kueue quota on queue %s (%ss elapsed).\n' \
            "${queue:-unknown}" "$((progress_now - started))"
          printf '[INFO]   Workspace pod requests %s.\n' "${requested:-unknown}"
          if [ -n "${effective_message}" ]; then
            printf '[INFO]   Kueue reports %s: %s\n' \
              "${effective_reason:-Pending}" "${effective_message}"
          fi
        fi
      fi
    else
      progress_now=$(date +%s)
      if [ "${progress_now}" -ge "${next_progress}" ]; then
        next_progress=$((progress_now + progress_interval_seconds))
        if [ -n "${pod_uid}" ]; then
          printf '[INFO] Waiting for Kueue to create the workspace workload (%ss elapsed).\n' \
            "$((progress_now - started))"
        else
          printf '[INFO] Waiting for the workspace pod to appear (%ss elapsed).\n' \
            "$((progress_now - started))"
        fi
      fi
    fi
  fi

  if [ "${admission_confirmed}" = true ]; then
    deployment_json=$(
      k8s_curl "${server}/apis/apps/v1/namespaces/${WORKSPACE_NAMESPACE}/deployments/coder-${CODER_WORKSPACE_ID}" 2>/dev/null || true
    )
    deployment_compact=$(printf '%s' "${deployment_json}" | tr -d '[:space:]')
    deployment_generation=$(printf '%s\n' "${deployment_compact}" \
      | sed -n 's/.*"generation":\([0-9][0-9]*\).*/\1/p')
    deployment_observed_generation=$(printf '%s\n' "${deployment_compact}" \
      | sed -n 's/.*"observedGeneration":\([0-9][0-9]*\).*/\1/p')
    deployment_updated_replicas=$(printf '%s\n' "${deployment_compact}" \
      | sed -n 's/.*"updatedReplicas":\([0-9][0-9]*\).*/\1/p')
    deployment_ready_replicas=$(printf '%s\n' "${deployment_compact}" \
      | sed -n 's/.*"readyReplicas":\([0-9][0-9]*\).*/\1/p')
    deployment_available_replicas=$(printf '%s\n' "${deployment_compact}" \
      | sed -n 's/.*"availableReplicas":\([0-9][0-9]*\).*/\1/p')

    if [ -n "${deployment_generation}" ] \
      && [ "${deployment_generation}" = "${deployment_observed_generation}" ] \
      && [ "${deployment_updated_replicas:-0}" -eq 1 ] \
      && [ "${workspace_container_running}" = true ]; then
      printf '[INFO] Workspace container started; Coder agent authentication follows build completion.\n'
      exit 0
    fi

    progress_now=$(date +%s)
    if [ "${progress_now}" -ge "${next_progress}" ]; then
      next_progress=$((progress_now + progress_interval_seconds))
      blocked=$(printf '%s\n' "${pods_json}" | pod_blocker)
      pod_phase=$(printf '%s\n' "${blocked}" | sed -n 's/^phase=//p')
      schedule_reason=$(printf '%s\n' "${blocked}" | sed -n 's/^schedule_reason=//p')
      schedule_message=$(printf '%s\n' "${blocked}" | sed -n 's/^schedule_message=//p')
      blocker=$(printf '%s\n' "${blocked}" | sed -n 's/^blocker=//p')
      blocker_reason=$(printf '%s\n' "${blocked}" | sed -n 's/^blocker_reason=//p')
      blocker_message=$(printf '%s\n' "${blocked}" | sed -n 's/^blocker_message=//p')

      printf '[INFO] Waiting for the workspace container (%ss elapsed, pod phase %s).\n' \
        "$((progress_now - started))" "${pod_phase:-unknown}"
      if [ -n "${schedule_reason}" ]; then
        printf '[INFO]   Not scheduled, %s: %s\n' \
          "${schedule_reason}" "${schedule_message:-no detail reported}"
      fi
      if [ -n "${blocker}" ]; then
        printf '[INFO]   Container %s is waiting, %s: %s\n' \
          "${blocker}" "${blocker_reason:-unknown}" "${blocker_message:-no detail reported}"
      fi
    fi
  fi

  now=$(date +%s)
  if [ "${now}" -ge "${deadline}" ]; then
    if [ "${admission_confirmed}" = true ]; then
      printf '[ERROR] Workspace container did not start within %ss (generation=%s observed=%s updated=%s container-running=%s pod-ready=%s available=%s).\n' \
        "${TIMEOUT_SECONDS}" \
        "${deployment_generation:-unknown}" \
        "${deployment_observed_generation:-unknown}" \
        "${deployment_updated_replicas:-0}" \
        "${workspace_container_running}" \
        "${deployment_ready_replicas:-0}" \
        "${deployment_available_replicas:-0}" >&2
    else
      printf '[ERROR] Workspace pod was not admitted within %ss.\n' "${TIMEOUT_SECONDS}" >&2
      if [ "${workload_found}" = true ]; then
        printf '[ERROR]   Queue %s, workspace pod requests %s.\n' \
          "${queue:-unknown}" "${requested:-unknown}" >&2
        printf '[ERROR]   Last Kueue status %s: %s\n' \
          "${effective_reason:-Pending}" "${effective_message:-no detail reported}" >&2
      elif [ -n "${pod_uid}" ]; then
        printf '%s\n' '[ERROR]   Kueue never created a Workload for the workspace pod.' >&2
      else
        printf '%s\n' '[ERROR]   The workspace pod never appeared; check the Deployment events.' >&2
      fi
    fi
    exit 1
  fi

  sleep 1
done
