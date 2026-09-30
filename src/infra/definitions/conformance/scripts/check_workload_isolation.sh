#!/bin/sh
# Executes resource-stress probes in team pods to defend control plane responsiveness and prevent noisy-neighbor denial of service.

# shellcheck disable=SC2310,SC2312
set -eu

busybox_image="${BUSYBOX_IMAGE:?busybox image is required}"
python_image="${PYTHON_IMAGE:?python image is required}"
script_directory="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
repository_root="$(git -C "${script_directory}" rev-parse --show-toplevel)"
template_directory="${repository_root}/src/infra/definitions/workspaces/templates/dev"
cell_kubeconfig="${repository_root}/.tmp/kubeconfigs/cell-eaws-lh1.yaml"
ctrl_kubeconfig="${repository_root}/.tmp/kubeconfigs/ctrl-eaws-lh1.yaml"
namespace=workload-isolation-e2e
provisioner=cluster:test:workload-isolation-coder-provisioner
provisioner_group=cluster:coder-provisioners
test_root="$(mktemp -d)"
deployment_plan="${test_root}/deployment-plan.json"
pvc_plan="${test_root}/pvc-plan.json"
representative="${test_root}/coder-representative.json"
monitor_stop="${test_root}/monitor-stop"
monitor_failures="${test_root}/monitor-failures"
monitor_pid=""
storage_exit_code=""

cell() {
  kubectl --kubeconfig="${cell_kubeconfig}" --request-timeout=5s "$@"
}

ctrl() {
  kubectl --kubeconfig="${ctrl_kubeconfig}" --request-timeout=2s "$@"
}

as_provisioner() {
  cell --as="${provisioner}" --as-group="${provisioner_group}" "$@"
}

expect_denied() {
  expected="$1"
  shift
  set +e
  output="$("$@" 2>&1)"
  status=$?
  set -e
  if [ "${status}" -eq 0 ]; then
    printf 'Admission unexpectedly accepted the Coder representative.\n' >&2
    return 1
  fi
  if ! printf '%s\n' "${output}" | grep -F "${expected}" >/dev/null; then
    printf 'Admission failed outside the expected policy:\n%s\n' "${output}" >&2
    return 1
  fi
}

render_workspace_contract() {
  module="${test_root}/coder-template"
  mkdir -p "${module}/tests"
  cp "${template_directory}"/*.tf "${template_directory}/.terraform.lock.hcl" \
    "${template_directory}/gpu-catalog.json" \
    "${template_directory}/gpu-catalog.schema.json" \
    "${template_directory}/workspace-mise.toml" "${module}/"
  test -s "${module}/gpu-catalog.json"
  test -s "${module}/gpu-catalog.schema.json"
  cp -R "${template_directory}/modules" "${template_directory}/scripts" "${module}/"
  awk '
    /^run "/ {
      runs++
      if (runs == 2) exit
    }
    { print }
  ' "${template_directory}/writer_inventory.tftest.hcl" \
    >"${module}/tests/workspace_shape.tftest.hcl"

  if ! TF_DATA_DIR="${test_root}/terraform-data" terraform -chdir="${module}" \
    init -backend=false -input=false -lockfile=readonly -no-color \
    >"${test_root}/terraform-init.log" 2>&1; then
    cat "${test_root}/terraform-init.log" >&2
    return 1
  fi
  if ! TF_DATA_DIR="${test_root}/terraform-data" terraform -chdir="${module}" \
    test -test-directory=tests -verbose -json \
    >"${test_root}/terraform-test.json" 2>"${test_root}/terraform-test.stderr"; then
    cat "${test_root}/terraform-test.stderr" >&2
    jq -r 'select(."@level" == "error") | ."@message"' \
      "${test_root}/terraform-test.json" >&2
    return 1
  fi

  jq -c '
    select(.test_plan) |
    .test_plan.resource_changes[] |
    select(.address == "kubernetes_deployment_v1.workspace[0]") |
    .change.after
  ' "${test_root}/terraform-test.json" >"${deployment_plan}"
  jq -c '
    select(.test_plan) |
    .test_plan.resource_changes[] |
    select(.address == "kubernetes_persistent_volume_claim_v1.home") |
    .change.after
  ' "${test_root}/terraform-test.json" >"${pvc_plan}"
  test "$(wc -l <"${deployment_plan}" | tr -d ' ')" = 1
  test "$(wc -l <"${pvc_plan}" | tr -d ' ')" = 1
}

assert_workspace_contract() {
  jq --exit-status --slurpfile pvc "${pvc_plan}" '
    .spec[0].template[0].spec[0] as $pod |
    $pod.container[0] as $workspace |
    ($pod.init_container[] | select(.name == "backup-proxy")) as $backup |
    ($pod.init_container[] | select(.name == "tailnet")) as $tailnet |
    ($pod.volume | map(select(.empty_dir | length == 1) |
      {key:.name,value:.empty_dir[0].size_limit}) | from_entries) as $emptyDirs |
    $pod.automount_service_account_token == false and
    $pod.share_process_namespace == false and
    $pod.service_account_name == "coder-workspace" and
    $workspace.resources[0] == {
      requests:{cpu:"1",memory:"8Gi"},
      limits:{cpu:"1",memory:"8Gi"}
    } and
    $backup.restart_policy == "Always" and
    $backup.resources[0] == {
      requests:{cpu:"25m",memory:"64Mi"},
      limits:{cpu:"500m",memory:"256Mi"}
    } and
    $tailnet.restart_policy == "Always" and
    $tailnet.resources[0] == {
      requests:{cpu:"10m",memory:"32Mi"},
      limits:{cpu:"250m",memory:"128Mi"}
    } and
    ([$workspace, $backup, $tailnet,
      ($pod.init_container[] | select(.name == "prepare-workspace-volume"))] |
      all(.security_context[0].read_only_root_filesystem == true)) and
    $emptyDirs == {
      "backup-proxy-tmp":"128Mi",
      "tailnet-state":"1Mi",
      "tailnet-tmp":"64Mi",
      "tmp":"4Gi"
    } and
    any($workspace.volume_mount[];
      .name == "tmp" and .mount_path == "/tmp" and .read_only == false) and
    any($backup.volume_mount[];
      .name == "backup-proxy-tmp" and .mount_path == "/tmp" and .read_only == false) and
    any($tailnet.volume_mount[];
      .name == "tailnet-state" and
      .mount_path == "/var/run/workspace/tailnet" and .read_only == false) and
    any($tailnet.volume_mount[];
      .name == "tailnet-tmp" and .mount_path == "/tmp" and .read_only == false) and
    any($pod.volume[];
      .name == "workspace" and
      .persistent_volume_claim[0].claim_name == $pvc[0].metadata[0].name and
      .persistent_volume_claim[0].read_only == false) and
    $pvc[0].spec[0].storage_class_name == "workspace-expandable" and
    $pvc[0].spec[0].resources[0].requests.storage == "16Gi"
  ' "${deployment_plan}" >/dev/null
}

write_admission_representative() {
  jq --arg namespace "${namespace}" --arg image "${busybox_image}" '
    def securityContext($source): {
      allowPrivilegeEscalation:$source.allow_privilege_escalation,
      capabilities:{drop:$source.capabilities[0].drop},
      readOnlyRootFilesystem:$source.read_only_root_filesystem,
      runAsGroup:($source.run_as_group | tonumber),
      runAsNonRoot:$source.run_as_non_root,
      runAsUser:($source.run_as_user | tonumber)
    };
    .spec[0].template[0].spec[0] as $pod |
    ($pod.init_container[] | select(.name == "backup-proxy")) as $backup |
    ($pod.init_container[] | select(.name == "tailnet")) as $tailnet |
    {
      apiVersion:"apps/v1",
      kind:"Deployment",
      metadata:{
        name:.metadata[0].name,
        namespace:$namespace,
        labels:.metadata[0].labels
      },
      spec:{
        replicas:1,
        selector:{matchLabels:.spec[0].selector[0].match_labels},
        strategy:{type:"Recreate"},
        template:{
          metadata:{labels:.spec[0].template[0].metadata[0].labels},
          spec:{
            automountServiceAccountToken:$pod.automount_service_account_token,
            serviceAccountName:$pod.service_account_name,
            securityContext:{
              runAsGroup:($pod.security_context[0].run_as_group | tonumber),
              runAsNonRoot:$pod.security_context[0].run_as_non_root,
              runAsUser:($pod.security_context[0].run_as_user | tonumber),
              seccompProfile:{type:$pod.security_context[0].seccomp_profile[0].type}
            },
            initContainers:[
              {
                name:$backup.name,image:$image,restartPolicy:$backup.restart_policy,
                command:["sh","-c","while :; do sleep 30; done"],
                resources:$backup.resources[0],
                securityContext:securityContext($backup.security_context[0]),
                volumeMounts:[{name:"backup-proxy-tmp",mountPath:"/tmp"}]
              },
              {
                name:$tailnet.name,image:$image,restartPolicy:$tailnet.restart_policy,
                command:["sh","-c","while :; do sleep 30; done"],
                resources:$tailnet.resources[0],
                securityContext:securityContext($tailnet.security_context[0]),
                volumeMounts:[
                  {name:"tailnet-state",mountPath:"/var/run/workspace/tailnet"},
                  {name:"tailnet-tmp",mountPath:"/tmp"}
                ]
              }
            ],
            containers:[{
              name:$pod.container[0].name,image:$image,
              command:["sh","-c","while :; do sleep 30; done"],
              resources:$pod.container[0].resources[0],
              securityContext:securityContext($pod.container[0].security_context[0]),
              volumeMounts:[{name:"tmp",mountPath:"/tmp"}]
            }],
            volumes:($pod.volume | map(select(.empty_dir | length == 1) | {
              name:.name,emptyDir:{sizeLimit:.empty_dir[0].size_limit}
            }))
          }
        }
      }
    }
  ' "${deployment_plan}" >"${representative}"
}

assert_admission_contract() {
  jq '
    del(.metadata.labels["availability-class"]) |
    del(.metadata.labels["latency-class"]) |
    del(.metadata.labels["kueue.x-k8s.io/queue-name"]) |
    del(.spec.template.spec.priorityClassName)
  ' "${representative}" >"${test_root}/unclassified-representative.json"
  expect_denied '[rule:availability-class-supported]' \
    cell --as="${provisioner}" create --dry-run=server --output=name \
    --filename="${test_root}/unclassified-representative.json"

  jq '
    .metadata.labels["availability-class"] = "be" |
    .metadata.labels["latency-class"] = "ls"
  ' "${representative}" >"${test_root}/queued-representative.json"
  cell --as="${provisioner}" create --dry-run=server --output=json \
    --filename="${test_root}/queued-representative.json" \
    | jq --exit-status '
      .metadata.labels["kueue.x-k8s.io/queue-name"] == "be" and
      .spec.template.spec.priorityClassName == "be-ls"
    ' >/dev/null

  as_provisioner create --dry-run=server --output=json \
    --filename="${test_root}/unclassified-representative.json" \
    | jq --exit-status '
      (.metadata.labels | has("kueue.x-k8s.io/queue-name") | not) and
      (.spec.template.spec | has("priorityClassName") | not)
    ' >/dev/null
}

stop_monitor() {
  if [ -z "${monitor_pid}" ]; then
    return
  fi
  : >"${monitor_stop}"
  wait "${monitor_pid}"
  monitor_pid=""
}

cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  stop_monitor || true
  cell delete namespace "${namespace}" --ignore-not-found=true --wait=false \
    >/dev/null 2>&1 || true
  rm -rf -- "${test_root}"
  exit "${status}"
}

monitor_cluster_apis() {
  while [ ! -e "${monitor_stop}" ]; do
    if ! ctrl get --raw=/readyz >/dev/null 2>&1; then
      printf 'ctrl %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        >>"${monitor_failures}"
    fi
    if ! cell get --raw=/readyz >/dev/null 2>&1; then
      printf 'cell %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        >>"${monitor_failures}"
    fi
    sleep 1
  done
}

wait_for_phase() {
  pod="$1"
  expected="$2"
  timeout_seconds="$3"
  attempt=0
  while [ "${attempt}" -lt "${timeout_seconds}" ]; do
    phase="$(
      cell get "pod/${pod}" --namespace="${namespace}" \
        --output=jsonpath='{.status.phase}' 2>/dev/null || true
    )"
    if [ "${phase}" = "${expected}" ]; then
      return
    fi
    attempt=$((attempt + 1))
    sleep 1
  done
  printf 'Pod %s did not reach %s within %ss.\n' \
    "${pod}" "${expected}" "${timeout_seconds}" >&2
  cell get "pod/${pod}" --namespace="${namespace}" --output=yaml >&2 || true
  return 1
}

assert_cluster_healthy() {
  kubeconfig="$1"
  kubectl --kubeconfig="${kubeconfig}" --request-timeout=5s get nodes \
    --output=json \
    | jq --exit-status '
      .items | length > 0 and all(
        .[];
        .status.conditions as $conditions |
        any($conditions[]; .type == "Ready" and .status == "True") and
        any($conditions[]; .type == "DiskPressure" and .status == "False") and
        any($conditions[]; .type == "MemoryPressure" and .status == "False")
      )
    ' >/dev/null
}

assert_workspace_storage_reserve() {
  cell --namespace=openebs get daemonset/rawfile-localpv-node --output=json \
    | jq --exit-status '
      [
        .spec.template.spec.containers[] |
        select(.name == "csi-driver") |
        .env[] |
        select(.name == "CSI_DRIVER__STORAGE_POOLS") |
        (.value | fromjson) |
        .default
      ] == [{
        path:"/var/lib/rancher/k3s/openplex/rawfile-localpv/default-pool/",
        reserved_capacity:"52GiB",
        reserved_capacity_mode:"plain"
      }]
    ' >/dev/null
}

trap cleanup EXIT HUP INT TERM

test -f "${cell_kubeconfig}"
test -f "${ctrl_kubeconfig}"
render_workspace_contract
assert_workspace_contract
write_admission_representative
ctrl get --raw=/readyz >/dev/null
assert_cluster_healthy "${cell_kubeconfig}"
assert_cluster_healthy "${ctrl_kubeconfig}"
assert_workspace_storage_reserve
cell delete namespace "${namespace}" --ignore-not-found=true --wait=true \
  --timeout=60s >/dev/null
jq --null-input --arg namespace "${namespace}" '
  {
    apiVersion:"v1",
    kind:"Namespace",
    metadata:{
      name:$namespace,
      labels:{
        "app.kubernetes.io/part-of":"team-lane",
        "cost-center":"examples",
        "environment":"production",
        "kueue.x-k8s.io/managed":"true",
        "pod-security.kubernetes.io/audit":"restricted",
        "pod-security.kubernetes.io/audit-version":"latest",
        "pod-security.kubernetes.io/warn":"restricted",
        "pod-security.kubernetes.io/warn-version":"latest",
        "team":"examples"
      }
    }
  }
' | cell create --filename=- >/dev/null
cell create serviceaccount coder-workspace --namespace="${namespace}" >/dev/null
jq --null-input --arg namespace "${namespace}" --arg provisioner "${provisioner}" '
  {
    apiVersion:"rbac.authorization.k8s.io/v1",
    kind:"Role",
    metadata:{name:"workload-isolation-provisioner",namespace:$namespace},
    rules:[{
      apiGroups:["apps"],resources:["deployments"],
      verbs:["create","delete","get","list","watch"]
    }]
  },
  {
    apiVersion:"rbac.authorization.k8s.io/v1",
    kind:"RoleBinding",
    metadata:{name:"workload-isolation-provisioner",namespace:$namespace},
    subjects:[{
      apiGroup:"rbac.authorization.k8s.io",kind:"User",name:$provisioner
    }],
    roleRef:{
      apiGroup:"rbac.authorization.k8s.io",kind:"Role",
      name:"workload-isolation-provisioner"
    }
  }
' | cell create --filename=- >/dev/null
assert_admission_contract
cell delete namespace "${namespace}" --wait=true --timeout=60s >/dev/null
cell create namespace "${namespace}" >/dev/null

monitor_cluster_apis &
monitor_pid=$!

jq --null-input --arg image "${busybox_image}" --arg namespace "${namespace}" '
  {
    apiVersion:"v1",
    kind:"Pod",
    metadata:{name:"workload-isolation-cpu",namespace:$namespace},
    spec:{
      automountServiceAccountToken:false,
      enableServiceLinks:false,
      restartPolicy:"Never",
      securityContext:{
        runAsGroup:1000,
        runAsNonRoot:true,
        runAsUser:1000,
        seccompProfile:{type:"RuntimeDefault"}
      },
      containers:[{
        name:"cpu",
        image:$image,
        imagePullPolicy:"IfNotPresent",
        command:["sh","-ceu"],
        args:[
          "before=$(awk \u0027$1 == \"nr_throttled\" { print $2 }\u0027 /sys/fs/cgroup/cpu.stat); yes >/dev/null & worker=$!; sleep 3; kill $worker; wait $worker 2>/dev/null || true; after=$(awk \u0027$1 == \"nr_throttled\" { print $2 }\u0027 /sys/fs/cgroup/cpu.stat); test $after -gt $before; printf \u0027cpu-throttled\\n\u0027"
        ],
        resources:{
          requests:{cpu:"25m",memory:"8Mi"},
          limits:{cpu:"25m",memory:"32Mi"}
        },
        securityContext:{
          allowPrivilegeEscalation:false,
          capabilities:{drop:["ALL"]},
          readOnlyRootFilesystem:true
        }
      }]
    }
  }
' >"${test_root}/cpu.json"
cell apply --filename="${test_root}/cpu.json" >/dev/null
wait_for_phase workload-isolation-cpu Succeeded 60
test "$(cell logs pod/workload-isolation-cpu --namespace="${namespace}")" = \
  cpu-throttled

jq --null-input --arg image "${python_image}" --arg namespace "${namespace}" '
  {
    apiVersion:"v1",
    kind:"Pod",
    metadata:{name:"workload-isolation-memory",namespace:$namespace},
    spec:{
      automountServiceAccountToken:false,
      enableServiceLinks:false,
      restartPolicy:"Never",
      securityContext:{
        runAsGroup:1000,
        runAsNonRoot:true,
        runAsUser:1000,
        seccompProfile:{type:"RuntimeDefault"}
      },
      containers:[{
        name:"memory",
        image:$image,
        imagePullPolicy:"IfNotPresent",
        command:["python3","-c","bytearray(128 * 1024 * 1024)"],
        resources:{
          requests:{cpu:"10m",memory:"16Mi"},
          limits:{cpu:"100m",memory:"48Mi"}
        },
        securityContext:{
          allowPrivilegeEscalation:false,
          capabilities:{drop:["ALL"]},
          readOnlyRootFilesystem:true
        }
      }]
    }
  }
' >"${test_root}/memory.json"
cell apply --filename="${test_root}/memory.json" >/dev/null
wait_for_phase workload-isolation-memory Failed 90
test "$(
  cell get pod/workload-isolation-memory --namespace="${namespace}" \
    --output=jsonpath='{.status.containerStatuses[?(@.name=="memory")].state.terminated.reason}'
)" = OOMKilled

jq --null-input --arg namespace "${namespace}" '
  {
    apiVersion:"v1",
    kind:"PersistentVolumeClaim",
    metadata:{name:"workload-isolation-storage",namespace:$namespace},
    spec:{
      accessModes:["ReadWriteOnce"],
      resources:{requests:{storage:"64Mi"}},
      storageClassName:"workspace-expandable"
    }
  }
' >"${test_root}/storage-pvc.json"
cell apply --filename="${test_root}/storage-pvc.json" >/dev/null

jq --null-input --arg image "${busybox_image}" --arg namespace "${namespace}" '
  {
    apiVersion:"v1",
    kind:"Pod",
    metadata:{name:"workload-isolation-storage",namespace:$namespace},
    spec:{
      automountServiceAccountToken:false,
      enableServiceLinks:false,
      restartPolicy:"Never",
      securityContext:{
        fsGroup:1000,
        fsGroupChangePolicy:"OnRootMismatch",
        runAsGroup:1000,
        runAsNonRoot:true,
        runAsUser:1000,
        seccompProfile:{type:"RuntimeDefault"}
      },
      containers:[{
        name:"storage",
        image:$image,
        imagePullPolicy:"IfNotPresent",
        command:["sh","-ceu"],
        args:["dd if=/dev/zero of=/workspace/fill bs=1M count=128 conv=fsync"],
        resources:{
          requests:{cpu:"10m",memory:"8Mi"},
          limits:{cpu:"50m",memory:"32Mi"}
        },
        securityContext:{
          allowPrivilegeEscalation:false,
          capabilities:{drop:["ALL"]},
          readOnlyRootFilesystem:true
        },
        volumeMounts:[{name:"workspace",mountPath:"/workspace"}]
      }],
      volumes:[{
        name:"workspace",
        persistentVolumeClaim:{claimName:"workload-isolation-storage"}
      }]
    }
  }
' >"${test_root}/storage-pod.json"
cell apply --filename="${test_root}/storage-pod.json" >/dev/null
cell wait persistentvolumeclaim/workload-isolation-storage \
  --namespace="${namespace}" --for=jsonpath='{.status.phase}'=Bound \
  --timeout=90s >/dev/null
wait_for_phase workload-isolation-storage Failed 120
storage_exit_code="$(
  cell get pod/workload-isolation-storage --namespace="${namespace}" \
    --output=jsonpath='{.status.containerStatuses[?(@.name=="storage")].state.terminated.exitCode}'
)"
test "${storage_exit_code}" -ne 0
cell logs pod/workload-isolation-storage --namespace="${namespace}" 2>&1 \
  | grep -F 'No space left on device' >/dev/null
cell delete pod/workload-isolation-storage --namespace="${namespace}" \
  --wait=true --timeout=60s >/dev/null
cell delete persistentvolumeclaim/workload-isolation-storage \
  --namespace="${namespace}" --wait=true --timeout=90s >/dev/null

jq --null-input --arg image "${busybox_image}" --arg namespace "${namespace}" '
  {
    apiVersion:"v1",
    kind:"Pod",
    metadata:{name:"workload-isolation-emptydir",namespace:$namespace},
    spec:{
      automountServiceAccountToken:false,
      enableServiceLinks:false,
      restartPolicy:"Never",
      securityContext:{
        runAsGroup:1000,
        runAsNonRoot:true,
        runAsUser:1000,
        seccompProfile:{type:"RuntimeDefault"}
      },
      containers:[{
        name:"emptydir",
        image:$image,
        imagePullPolicy:"IfNotPresent",
        command:["sh","-ceu"],
        args:["dd if=/dev/zero of=/scratch/fill bs=1M count=16; sync; sleep 300"],
        resources:{
          requests:{cpu:"10m","ephemeral-storage":"1Mi",memory:"8Mi"},
          limits:{cpu:"50m","ephemeral-storage":"32Mi",memory:"32Mi"}
        },
        securityContext:{
          allowPrivilegeEscalation:false,
          capabilities:{drop:["ALL"]},
          readOnlyRootFilesystem:true
        },
        volumeMounts:[{name:"scratch",mountPath:"/scratch"}]
      }],
      volumes:[{name:"scratch",emptyDir:{sizeLimit:"8Mi"}}]
    }
  }
' >"${test_root}/emptydir.json"
cell apply --filename="${test_root}/emptydir.json" >/dev/null
wait_for_phase workload-isolation-emptydir Failed 120
test "$(
  cell get pod/workload-isolation-emptydir --namespace="${namespace}" \
    --output=jsonpath='{.status.reason}'
)" = Evicted

stop_monitor
ctrl get --raw=/readyz >/dev/null
cell get --raw=/readyz >/dev/null
if [ -s "${monitor_failures}" ]; then
  printf 'A local API failed readiness checks during bounded cell pressure:\n' >&2
  cat "${monitor_failures}" >&2
  exit 1
fi
