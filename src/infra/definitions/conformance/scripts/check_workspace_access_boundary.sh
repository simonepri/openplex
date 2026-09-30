#!/bin/sh
# Verifies Coder workspace SSH endpoints restrict access to canonical Tailscale network paths to defend developer environments from unauthorized ingress.

set -eu

workspaces="$(
  kubectl get deployments --all-namespaces \
    --selector=app.kubernetes.io/name=coder-workspace --output=json
)"
workspace_count="$(printf '%s' "${workspaces}" | jq '.items | length')"
if test "${workspace_count}" -ne 1; then
  printf 'Expected one running Coder workspace, found %s.\n' \
    "${workspace_count}" >&2
  exit 1
fi

namespace="$(printf '%s' "${workspaces}" | jq --raw-output '.items[0].metadata.namespace')"
instance="$(
  printf '%s' "${workspaces}" \
    | jq --exit-status --raw-output \
      '.items[0].metadata.labels["app.kubernetes.io/instance"] | select(length > 0)'
)"
printf '%s' "${workspaces}" \
  | jq --exit-status '
    .items[0].spec.template.spec as $pod |
    ($pod.securityContext.runAsNonRoot == true) and
    ($pod.securityContext.runAsUser == 1000) and
    ($pod.securityContext.runAsGroup == 1000) and
    ($pod.securityContext.seccompProfile.type == "RuntimeDefault") and
    (all(($pod.initContainers // [])[];
      .securityContext.allowPrivilegeEscalation == false and
      .securityContext.privileged != true and
      .securityContext.runAsNonRoot == true and
      .securityContext.capabilities.drop == ["ALL"] and
      ((.securityContext.capabilities.add // []) | index("SYS_ADMIN") | not))) and
    (all($pod.containers[];
      .securityContext.allowPrivilegeEscalation == false and
      .securityContext.privileged != true and
      .securityContext.runAsNonRoot == true and
      .securityContext.capabilities.drop == ["ALL"] and
      ((.securityContext.capabilities.add // []) | index("SYS_ADMIN") | not))) and
    (all(($pod.volumes // [])[]; has("hostPath") | not)) and
    (all(
      (($pod.initContainers // []) + $pod.containers)[] |
        (.volumeMounts // [])[];
      (.mountPath != "/dev/fuse") and (.mountPath != "/dev/net/tun")
    ))
  ' >/dev/null

kubectl get namespace "${namespace}" --output=json \
  | jq --exit-status '
    .metadata.labels["pod-security.kubernetes.io/audit"] == "restricted" and
    .metadata.labels["pod-security.kubernetes.io/warn"] == "restricted"
  ' >/dev/null

published_services="$(kubectl get services --all-namespaces --output=json)"
workspace_service_names="$(
  printf '%s' "${published_services}" \
    | jq --compact-output --arg namespace "${namespace}" --arg instance "${instance}" '
      [.items[] |
        select(
          .metadata.namespace == $namespace and
          (.spec.selector["app.kubernetes.io/instance"] // "") == $instance
        ) |
        .metadata.name
      ]
    '
)"
published_routes="$(
  kubectl get httproutes.gateway.networking.k8s.io,tlsroutes.gateway.networking.k8s.io \
    --all-namespaces --output=json
)"
printf '%s' "${published_routes}" \
  | jq --exit-status \
    --arg namespace "${namespace}" \
    --argjson service_names "${workspace_service_names}" '
      all(.items[];
        . as $route |
        all((.spec.rules // [])[];
          all((.backendRefs // [])[];
            . as $backend |
            (.kind // "Service") != "Service" or
            (.namespace // $route.metadata.namespace) != $namespace or
            (($service_names | index($backend.name)) == null)
          )
        )
      )
    ' >/dev/null

printf '%s' "${published_services}" \
  | jq --exit-status --arg namespace "${namespace}" --arg instance "${instance}" '
    all(.items[];
      (.spec.type != "LoadBalancer" and .spec.type != "NodePort") or
      .metadata.namespace != $namespace or
      (.spec.selector["app.kubernetes.io/instance"] // "") != $instance
    )
  ' >/dev/null

pod="$(
  kubectl get pods --namespace="${namespace}" \
    --selector=app.kubernetes.io/name=coder-workspace \
    --field-selector=status.phase=Running --output=json \
    | jq --exit-status --raw-output \
      --arg instance "${instance}" \
      '.items[] | select(.metadata.labels["app.kubernetes.io/instance"] == $instance) | .metadata.name' \
    | head -n 1
)"
test -n "${pod}"

kubectl exec --namespace="${namespace}" "pod/${pod}" --container=workspace -- \
  sh -ceu \
  'python3 -c "import socket; s = socket.create_connection((\"127.0.0.1\", 2222), timeout=5); print(s.recv(1024).decode())" | grep "^SSH-2.0-OpenSSH_"'
