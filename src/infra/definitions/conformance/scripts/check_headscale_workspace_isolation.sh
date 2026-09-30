#!/bin/sh
# Checks Headscale process isolation, mounted token protections, and admission deny rules to defend network sidecars against credential theft.

set -eu

busybox_image="${BUSYBOX_IMAGE:?busybox image is required}"
namespace=headscale-workspace-isolation-e2e
headscale_namespace=headscale
team=headscale-isolation-team
argocd=system:serviceaccount:argocd:argocd-application-controller
deployment_controller=system:serviceaccount:kube-system:deployment-controller
replicaset_controller=system:serviceaccount:kube-system:replicaset-controller
kube_controller_manager=system:kube-controller-manager
deployment_message='Only Argo CD may manage the exact isolated Headscale workspace process shape.'
replicaset_message='Only the Deployment controller may materialize the isolated Headscale ReplicaSet.'
pod_message='Only the ReplicaSet controller may materialize the isolated Headscale Pod.'
session_message='Interactive access to the isolated Headscale Pod is forbidden.'
fixture="${FIXTURE:?fixture path is required}"
temp_dir="$(mktemp -d)"

cleanup() {
  cleanup_status=$?
  set +e
  kubectl delete pod ordinary-debug-target --namespace="${namespace}" \
    --as="${team}" --ignore-not-found=true --wait=true >/dev/null 2>&1
  kubectl delete rolebinding headscale-workspace-session-conformance \
    --namespace="${headscale_namespace}" --ignore-not-found=true >/dev/null 2>&1
  kubectl delete role headscale-workspace-session-conformance \
    --namespace="${headscale_namespace}" --ignore-not-found=true >/dev/null 2>&1
  kubectl delete rolebinding headscale-workspace-isolation-conformance \
    --namespace="${namespace}" --ignore-not-found=true >/dev/null 2>&1
  kubectl delete role headscale-workspace-isolation-conformance \
    --namespace="${namespace}" --ignore-not-found=true >/dev/null 2>&1
  rm -r -- "${temp_dir}"
  exit "${cleanup_status}"
}

expect_allowed() {
  description="$1"
  shift
  set +e
  output="$("$@" 2>&1)"
  command_status=$?
  set -e
  if [ "${command_status}" -ne 0 ]; then
    printf '%s unexpectedly failed:\n%s\n' "${description}" "${output}" >&2
    return 1
  fi
}

expect_denied() {
  description="$1"
  expected="$2"
  shift 2
  set +e
  output="$("$@" 2>&1)"
  command_status=$?
  set -e
  if [ "${command_status}" -eq 0 ]; then
    printf '%s unexpectedly succeeded\n' "${description}" >&2
    return 1
  fi
  if ! printf '%s\n' "${output}" | grep -F "${expected}" >/dev/null; then
    printf '%s failed outside the expected policy:\n%s\n' \
      "${description}" "${output}" >&2
    return 1
  fi
}

expect_connect_denied() {
  description="$1"
  shift
  output_file="${temp_dir}/connect-output"
  set +e
  "$@" >"${output_file}" 2>&1 &
  command_pid=$!
  elapsed=0
  while kill -0 "${command_pid}" 2>/dev/null; do
    if [ "${elapsed}" -eq 50 ]; then
      kill "${command_pid}" 2>/dev/null
      wait "${command_pid}" 2>/dev/null
      set -e
      printf '%s did not fail closed within five seconds\n' "${description}" >&2
      return 1
    fi
    sleep 0.1
    elapsed=$((elapsed + 1))
  done
  wait "${command_pid}"
  command_status=$?
  set -e
  if [ "${command_status}" -eq 0 ]; then
    printf '%s unexpectedly succeeded\n' "${description}" >&2
    return 1
  fi
  if ! grep -F "${session_message}" "${output_file}" >/dev/null; then
    printf '%s failed outside the expected policy:\n' "${description}" >&2
    sed -n '1,20p' "${output_file}" >&2
    return 1
  fi
}

trap cleanup EXIT HUP INT TERM

kubectl apply --filename="${fixture}" >/dev/null

for label in audit audit-version enforce enforce-version warn warn-version; do
  case "${label}" in
    *-version) expected=latest ;;
    *) expected=restricted ;;
  esac
  actual="$(kubectl get namespace "${headscale_namespace}" --output=json \
    | jq --raw-output --arg key "pod-security.kubernetes.io/${label}" \
      '.metadata.labels[$key] // ""')"
  if [ "${actual}" != "${expected}" ]; then
    printf 'Headscale namespace %s label is %s, expected %s\n' \
      "${label}" "${actual}" "${expected}" >&2
    exit 1
  fi
done

kubectl get deployment headscale --namespace="${headscale_namespace}" --output=json \
  >"${temp_dir}/live-deployment.json"
jq --arg namespace "${namespace}" '
  del(
    .metadata.annotations,
    .metadata.creationTimestamp,
    .metadata.generation,
    .metadata.managedFields,
    .metadata.resourceVersion,
    .metadata.uid,
    .status
  ) |
  .metadata.namespace = $namespace |
  .spec.replicas = 0
' "${temp_dir}/live-deployment.json" >"${temp_dir}/deployment.json"

expect_allowed 'canonical isolated Headscale Deployment' \
  kubectl create --dry-run=server --output=name \
  --filename="${temp_dir}/deployment.json" --as="${argocd}"
expect_denied 'team-created Headscale Deployment' "${deployment_message}" \
  kubectl create --dry-run=server --output=name \
  --filename="${temp_dir}/deployment.json" --as="${team}"

jq '.spec.template.spec.shareProcessNamespace = true' \
  "${temp_dir}/deployment.json" >"${temp_dir}/deployment-shared-process.json"
expect_denied 'shared process namespace' "${deployment_message}" \
  kubectl create --dry-run=server --output=name \
  --filename="${temp_dir}/deployment-shared-process.json" --as="${argocd}"
jq '.spec.template.spec.securityContext.seccompProfile.type = "Unconfined"' \
  "${temp_dir}/deployment.json" >"${temp_dir}/deployment-unconfined.json"
expect_denied 'unconfined Headscale Pod' "${deployment_message}" \
  kubectl create --dry-run=server --output=name \
  --filename="${temp_dir}/deployment-unconfined.json" --as="${argocd}"
jq '(.spec.template.spec.containers[] | select(.name == "workspace-enrollment") |
  .securityContext.readOnlyRootFilesystem) = false' \
  "${temp_dir}/deployment.json" >"${temp_dir}/deployment-writable-public-root.json"
expect_denied 'writable public process root' "${deployment_message}" \
  kubectl create --dry-run=server --output=name \
  --filename="${temp_dir}/deployment-writable-public-root.json" --as="${argocd}"
jq '(.spec.template.spec.containers[] | select(.name == "workspace-enrollment") |
  .volumeMounts) += [{"name":"broker-tls","mountPath":"/stolen","readOnly":true}]' \
  "${temp_dir}/deployment.json" >"${temp_dir}/deployment-public-broker-tls.json"
expect_denied 'broker TLS mounted in public process' "${deployment_message}" \
  kubectl create --dry-run=server --output=name \
  --filename="${temp_dir}/deployment-public-broker-tls.json" --as="${argocd}"
jq '(.spec.template.spec.containers[] | select(.name == "workspace-enrollment") |
  .env) += [{"name":"SNAPSHOT_ROOT_KEY","valueFrom":{"secretKeyRef":{
    "name":"workspace-snapshot-root","key":"workspace_snapshot_root_key"}}}]' \
  "${temp_dir}/deployment.json" >"${temp_dir}/deployment-public-root-key.json"
expect_denied 'snapshot root exposed to public process' "${deployment_message}" \
  kubectl create --dry-run=server --output=name \
  --filename="${temp_dir}/deployment-public-root-key.json" --as="${argocd}"
jq '(.spec.template.spec.containers[] | select(.name == "workspace-enrollment") |
  .volumeMounts[] | select(.name == "enrollment-state")).readOnly = false' \
  "${temp_dir}/deployment.json" >"${temp_dir}/deployment-public-writable-enrollment.json"
expect_denied 'public process writable enrollment state' "${deployment_message}" \
  kubectl create --dry-run=server --output=name \
  --filename="${temp_dir}/deployment-public-writable-enrollment.json" --as="${argocd}"
jq '(.spec.template.spec.containers[] | select(.name == "workspace-coordinator") |
  .volumeMounts[] | select(.name == "enrollment-state")).readOnly = true' \
  "${temp_dir}/deployment.json" >"${temp_dir}/deployment-coordinator-readonly-enrollment.json"
expect_denied 'coordinator without writable key ledger' "${deployment_message}" \
  kubectl create --dry-run=server --output=name \
  --filename="${temp_dir}/deployment-coordinator-readonly-enrollment.json" --as="${argocd}"
jq '(.spec.template.spec.containers[] | select(.name == "workspace-coordinator") |
  .volumeMounts[] | select(.name == "state")) |= del(.subPath)' \
  "${temp_dir}/deployment.json" >"${temp_dir}/deployment-coordinator-full-state.json"
expect_denied 'full Headscale state mounted in coordinator' "${deployment_message}" \
  kubectl create --dry-run=server --output=name \
  --filename="${temp_dir}/deployment-coordinator-full-state.json" --as="${argocd}"
jq '(.spec.template.spec.containers[] | select(.name == "workspace-broker") |
  .volumeMounts) += [{"name":"run","mountPath":"/var/run/headscale"}]' \
  "${temp_dir}/deployment.json" >"${temp_dir}/deployment-broker-admin-socket.json"
expect_denied 'Headscale admin socket mounted in broker' "${deployment_message}" \
  kubectl create --dry-run=server --output=name \
  --filename="${temp_dir}/deployment-broker-admin-socket.json" --as="${argocd}"
jq '(.spec.template.spec.initContainers[] | select(.name == "initialize-workspace-dns") |
  .volumeMounts[] | select(.name == "state")).subPath = "workspace-dns"' \
  "${temp_dir}/deployment.json" >"${temp_dir}/deployment-init-subpath.json"
expect_denied 'bootstrap init without its full-state exception' "${deployment_message}" \
  kubectl create --dry-run=server --output=name \
  --filename="${temp_dir}/deployment-init-subpath.json" --as="${argocd}"

jq --arg namespace "${namespace}" '
  {
    apiVersion:"apps/v1",
    kind:"ReplicaSet",
    metadata:{
      name:"headscale-abcde",
      namespace:$namespace,
      labels:.metadata.labels,
      ownerReferences:[{
        apiVersion:"apps/v1",
        kind:"Deployment",
        name:"headscale",
        uid:"11111111-1111-4111-8111-111111111111",
        controller:true,
        blockOwnerDeletion:true
      }]
    },
    spec:{replicas:0,selector:.spec.selector,template:.spec.template}
  }
' "${temp_dir}/deployment.json" >"${temp_dir}/replicaset.json"
for controller in "${kube_controller_manager}" "${deployment_controller}"; do
  expect_allowed "ReplicaSet from ${controller}" \
    kubectl create --dry-run=server --output=name \
    --filename="${temp_dir}/replicaset.json" --as="${controller}"
done
expect_denied 'team-created Headscale ReplicaSet' "${replicaset_message}" \
  kubectl create --dry-run=server --output=name \
  --filename="${temp_dir}/replicaset.json" --as="${team}"

jq --arg namespace "${namespace}" '
  {
    apiVersion:"v1",
    kind:"Pod",
    metadata:{
      name:"headscale-abcde-fghij",
      namespace:$namespace,
      labels:.spec.template.metadata.labels,
      ownerReferences:[{
        apiVersion:"apps/v1",
        kind:"ReplicaSet",
        name:"headscale-abcde",
        uid:"22222222-2222-4222-8222-222222222222",
        controller:true,
        blockOwnerDeletion:true
      }]
    },
    spec:.spec.template.spec
  }
' "${temp_dir}/replicaset.json" >"${temp_dir}/pod.json"
for controller in "${kube_controller_manager}" "${replicaset_controller}"; do
  expect_allowed "Pod from ${controller}" \
    kubectl create --dry-run=server --output=name \
    --filename="${temp_dir}/pod.json" --as="${controller}"
done
expect_denied 'team-created Headscale Pod' "${pod_message}" \
  kubectl create --dry-run=server --output=name \
  --filename="${temp_dir}/pod.json" --as="${team}"

jq --null-input --arg namespace "${namespace}" --arg image "${busybox_image}" '
  {
    apiVersion:"v1",
    kind:"Pod",
    metadata:{name:"raw-protected-probe",namespace:$namespace},
    spec:{
      automountServiceAccountToken:false,
      securityContext:{runAsNonRoot:true,runAsUser:1000,runAsGroup:1000,
        seccompProfile:{type:"RuntimeDefault"}},
      containers:[{
        name:"app",image:$image,command:["sh","-ceu","while :; do sleep 30; done"],
        resources:{requests:{cpu:"1m",memory:"4Mi"},limits:{cpu:"50m",memory:"16Mi"}},
        securityContext:{allowPrivilegeEscalation:false,readOnlyRootFilesystem:true,
          capabilities:{drop:["ALL"]}}
      }]
    }
  }
' >"${temp_dir}/raw-pod.json"

jq '.spec.containers[0].env = [{"name":"ROOT","valueFrom":{"secretKeyRef":{
  "name":"workspace-snapshot-root","key":"workspace_snapshot_root_key"}}}]' \
  "${temp_dir}/raw-pod.json" >"${temp_dir}/raw-env.json"
jq '.spec.containers[0].envFrom = [{"secretRef":{"name":"headscale-workspace-broker-tls"}}]' \
  "${temp_dir}/raw-pod.json" >"${temp_dir}/raw-envfrom.json"
jq '.spec.volumes = [{"name":"protected","projected":{"sources":[{
  "secret":{"name":"workspace-snapshot-root"}}]}}]' \
  "${temp_dir}/raw-pod.json" >"${temp_dir}/raw-projected.json"
jq '.spec.volumes = [{"name":"protected","csi":{"driver":"fixture.csi.invalid",
  "nodePublishSecretRef":{"name":"headscale-workspace-broker-tls"}}}]' \
  "${temp_dir}/raw-pod.json" >"${temp_dir}/raw-csi.json"
jq '.spec.volumes = [{"name":"protected","cephfs":{"monitors":["10.0.0.1:6789"],
  "secretRef":{"name":"headscale-tls"}}}]' \
  "${temp_dir}/raw-pod.json" >"${temp_dir}/raw-legacy.json"
jq '.spec.imagePullSecrets = [{"name":"workspace-snapshot-root"}]' \
  "${temp_dir}/raw-pod.json" >"${temp_dir}/raw-imagepull.json"
jq '.spec.initContainers = [(.spec.containers[0] | .name = "init" |
  .env = [{"name":"ROOT","valueFrom":{"secretKeyRef":{
    "name":"workspace-snapshot-root","key":"workspace_snapshot_root_key"}}}])]' \
  "${temp_dir}/raw-pod.json" >"${temp_dir}/raw-init.json"
jq '.metadata.ownerReferences = [{"apiVersion":"batch/v1","kind":"Job",
  "name":"team-job","uid":"33333333-3333-4333-8333-333333333333",
  "controller":true,"blockOwnerDeletion":true}]' \
  "${temp_dir}/raw-env.json" >"${temp_dir}/job-pod.json"
for surface in env envfrom projected csi legacy imagepull init; do
  expect_denied "raw Pod ${surface} protected reference" "${pod_message}" \
    kubectl create --dry-run=server --output=name \
    --filename="${temp_dir}/raw-${surface}.json" --as="${team}"
done
expect_denied 'Job-controller Pod protected reference' "${pod_message}" \
  kubectl create --dry-run=server --output=name \
  --filename="${temp_dir}/job-pod.json" --as="${kube_controller_manager}"

jq '.metadata.name = "ordinary-debug-target"' \
  "${temp_dir}/raw-pod.json" >"${temp_dir}/ordinary-pod.json"
kubectl create --filename="${temp_dir}/ordinary-pod.json" --as="${team}" >/dev/null
kubectl wait pod/ordinary-debug-target --namespace="${namespace}" \
  --for=condition=Ready --timeout=120s >/dev/null
expect_allowed 'ordinary Pod exec' \
  kubectl exec pod/ordinary-debug-target --namespace="${namespace}" \
  --as="${team}" -- true

protected_pod="$(kubectl get pod --namespace="${headscale_namespace}" \
  --selector=app.kubernetes.io/name=headscale \
  --field-selector=status.phase=Running \
  --output=jsonpath='{.items[0].metadata.name}')"
if [ -z "${protected_pod}" ]; then
  printf 'no running Headscale Pod found\n' >&2
  exit 1
fi
expect_connect_denied 'Headscale Pod exec' \
  kubectl exec pod/"${protected_pod}" --namespace="${headscale_namespace}" \
  --container=workspace-enrollment --as="${team}" -- true
expect_connect_denied 'Headscale Pod attach' \
  kubectl attach pod/"${protected_pod}" --namespace="${headscale_namespace}" \
  --container=workspace-enrollment --as="${team}"
local_port=$((20000 + $$ % 10000))
expect_connect_denied 'Headscale Pod port-forward' \
  kubectl port-forward pod/"${protected_pod}" --namespace="${headscale_namespace}" \
  --as="${team}" "${local_port}:8080"
kubectl get pod "${protected_pod}" --namespace="${headscale_namespace}" --output=json \
  >"${temp_dir}/protected-ephemeral.json"
expect_denied 'Headscale Pod ephemeral-container update' "${session_message}" \
  kubectl replace --subresource=ephemeralcontainers --dry-run=server \
  --filename="${temp_dir}/protected-ephemeral.json" --as="${team}"

jq --null-input --arg image "${busybox_image}" '
  {spec:{ephemeralContainers:[{
    name:"root-reader",image:$image,command:["sh","-c","true"],
    env:[{name:"ROOT",valueFrom:{secretKeyRef:{
      name:"workspace-snapshot-root",key:"workspace_snapshot_root_key"}}}],
    securityContext:{allowPrivilegeEscalation:false,readOnlyRootFilesystem:true,
      capabilities:{drop:["ALL"]}}
  }]}}
' >"${temp_dir}/ordinary-ephemeral.json"
expect_denied 'ordinary Pod ephemeral protected reference' "${pod_message}" \
  kubectl patch pod/ordinary-debug-target --namespace="${namespace}" \
  --subresource=ephemeralcontainers --type=merge \
  --patch-file="${temp_dir}/ordinary-ephemeral.json" --as="${team}" --dry-run=server

trap - EXIT HUP INT TERM
cleanup
