#!/bin/sh
# Starts capacity-bounded k3s with durable node, network, registry, and JWT contracts.

# shellcheck disable=SC2310
set -eu

mountinfo_path="${K3S_MOUNTINFO_PATH:-/proc/self/mountinfo}"
local_role=""
if [ -f "${mountinfo_path}" ]; then
  if grep -q -- "-cell-eaws-lh1" "${mountinfo_path}" 2>/dev/null; then
    local_role="cell"
    K3S_NODE_IPV4_CIDR="172.19.255.11/16"
    K3S_POD_IPV4_CIDR="10.241.0.0/16"
    K3S_SERVICE_IPV4_CIDR="172.31.16.0/20"
    K3S_SHARED_IPV4_CIDR="172.19.0.0/16"
    K3S_ZONE="${K3S_ZONE:-lh1-a}"
  elif grep -q -- "-ctrl-eaws-lh1" "${mountinfo_path}" 2>/dev/null; then
    local_role="ctrl"
    K3S_NODE_IPV4_CIDR="172.19.255.10/16"
    K3S_POD_IPV4_CIDR="10.240.0.0/16"
    K3S_SERVICE_IPV4_CIDR="172.31.0.0/20"
    K3S_SHARED_IPV4_CIDR="172.19.0.0/16"
  fi
fi

if [ ! -f /var/lib/rancher/k3s/agent/etc/kubelet.conf.d/10-max-pods.conf ]; then
  mkdir -p /var/lib/rancher/k3s/agent/etc/kubelet.conf.d 2>/dev/null || true
  printf 'apiVersion: kubelet.config.k8s.io/v1beta1\nkind: KubeletConfiguration\nmaxPods: 250\n' >/var/lib/rancher/k3s/agent/etc/kubelet.conf.d/10-max-pods.conf 2>/dev/null || true
fi

if [ -z "${K3S_INSTANCE_TYPE:-}" ]; then
  if [ -n "${local_role}" ]; then
    K3S_INSTANCE_TYPE="m6gd.4xlarge"
  else
    K3S_INSTANCE_TYPE="m5.large"
  fi
fi
instance_type_memory_mib=8192
instance_type_cpu_millicores=2000
case "${K3S_INSTANCE_TYPE}" in
  t2.micro)
    instance_type_memory_mib=1024
    instance_type_cpu_millicores=1000
    ;;
  t3.micro | t4g.micro)
    instance_type_memory_mib=1024
    instance_type_cpu_millicores=2000
    ;;
  t3.small | t4g.small)
    instance_type_memory_mib=2048
    instance_type_cpu_millicores=2000
    ;;
  t3.medium | t4g.medium)
    instance_type_memory_mib=4096
    instance_type_cpu_millicores=2000
    ;;
  m8gd.medium)
    instance_type_memory_mib=4096
    instance_type_cpu_millicores=1000
    ;;
  m5.large | m6gd.large | m7gd.large | m8gd.large)
    instance_type_memory_mib=8192
    instance_type_cpu_millicores=2000
    ;;
  m6gd.2xlarge | m7gd.2xlarge | m8gd.2xlarge)
    instance_type_memory_mib=32768
    instance_type_cpu_millicores=8000
    ;;
  m6gd.4xlarge | m7gd.4xlarge | m8gd.4xlarge)
    instance_type_memory_mib=65536
    instance_type_cpu_millicores=16000
    ;;
  *) ;;
esac

node_memory_kib="$(awk '/^MemTotal:/ { print $2; exit }' /proc/meminfo)"
node_cpu_millicores="$(awk '/^processor[[:space:]]*:/ { count++ } END { print count * 1000 }' /proc/cpuinfo)"
if [ -n "${local_role}" ]; then
  # Both canonical nodes see the same VM. Reserve shared services once and
  # partition its remaining memory, rather than advertising it twice.
  shared_memory_reserve_mib=1024
  shared_memory_mib=$((node_memory_kib / 1024 - shared_memory_reserve_mib))
  if [ "${shared_memory_mib}" -le 0 ]; then
    printf 'The VM must leave memory beyond the shared service reserve\n' >&2
    exit 1
  fi
  memory_weight=8
  [ "${local_role}" != cell ] || memory_weight=7
  local_memory_share_mib=$((shared_memory_mib * memory_weight / 15))
  shared_pod_cpu_millicores=$((node_cpu_millicores - 1000))
  if [ "${shared_pod_cpu_millicores}" -le 0 ]; then
    printf 'The VM must leave CPU beyond both nodes system reserves\n' >&2
    exit 1
  fi
  local_pod_cpu_millicores=$((shared_pod_cpu_millicores * 7 / 13))
  if [ "${local_role}" = cell ]; then
    local_pod_cpu_millicores=$((shared_pod_cpu_millicores - local_pod_cpu_millicores))
  fi
  local_cpu_share_millicores=$((local_pod_cpu_millicores + 500))
  : "${K3S_MEMORY_MIB:=${local_memory_share_mib}}"
  : "${K3S_CPU_MILLICORES:=${local_cpu_share_millicores}}"
  : "${K3S_SYSTEM_CPU_MILLICORES:=500}"
else
  : "${K3S_MEMORY_MIB:=${instance_type_memory_mib}}"
  : "${K3S_CPU_MILLICORES:=${instance_type_cpu_millicores}}"
  : "${K3S_SYSTEM_CPU_MILLICORES:=1000}"
fi

case "${K3S_MEMORY_MIB}" in
  *[!0-9]* | "")
    printf 'K3S_MEMORY_MIB must be a positive integer\n' >&2
    exit 1
    ;;
  *) ;;
esac
case "${K3S_CPU_MILLICORES}" in
  *[!0-9]* | "")
    printf 'K3S_CPU_MILLICORES must be a positive integer\n' >&2
    exit 1
    ;;
  *) ;;
esac
case "${K3S_SYSTEM_CPU_MILLICORES}" in
  *[!0-9]* | "")
    printf 'K3S_SYSTEM_CPU_MILLICORES must be a positive integer\n' >&2
    exit 1
    ;;
  *) ;;
esac
if [ "${K3S_SYSTEM_CPU_MILLICORES}" -ge "${K3S_CPU_MILLICORES}" ]; then
  printf 'K3S_SYSTEM_CPU_MILLICORES must be smaller than K3S_CPU_MILLICORES\n' >&2
  exit 1
fi
if [ -n "${local_role}" ] && { [ "${K3S_MEMORY_MIB}" -gt "${local_memory_share_mib}" ] || [ "${K3S_CPU_MILLICORES}" -gt "${local_cpu_share_millicores}" ]; }; then
  printf 'Local node CPU and memory must not exceed their shared VM allocation\n' >&2
  exit 1
fi
if [ -n "${local_role}" ] && [ "${K3S_SYSTEM_CPU_MILLICORES}" -lt 500 ]; then
  printf 'Each local node must reserve at least 500 millicores for system processes\n' >&2
  exit 1
fi

inotify_instances_path="${K3S_PROC_SYS_ROOT:-/proc/sys}/fs/inotify/max_user_instances"
# K3s and containerd run outside Pod accounting. Reserve their memory and
# eviction headroom within each node's share of the common VM.
k3s_daemon_memory_reserve_mib=2048
kubelet_eviction_memory_buffer_mib=1024
pod_memory_budget_mib=$((K3S_MEMORY_MIB - k3s_daemon_memory_reserve_mib - kubelet_eviction_memory_buffer_mib))
if [ "${K3S_MEMORY_MIB}" -le "${k3s_daemon_memory_reserve_mib}" ]; then
  printf 'K3S_MEMORY_MIB must exceed the k3s daemon memory reserve\n' >&2
  exit 1
fi
if [ "${pod_memory_budget_mib}" -le 0 ]; then
  printf 'K3S_MEMORY_MIB must leave memory for kubelet eviction\n' >&2
  exit 1
fi
registry_hosts_source="${K3S_REGISTRY_HOSTS_SOURCE:-/usr/local/share/k3s-runtime/registry/localhost-15100/hosts.toml}"
data_root="${K3S_DATA_DIR:-/var/lib/rancher/k3s}"
node_identity_path="${data_root}/.floci-node-name"
registry_hosts_root="${data_root}/agent/etc/containerd/certs.d"
registry_mirror_path="${K3S_REGISTRIES_PATH:-/etc/rancher/k3s/registries.yaml}"
hosts_path="${K3S_HOSTS_PATH:-/etc/hosts}"

is_canonical_node_name() {
  case "${1:-}" in
    "" | *[!0-9a-f]*) return 1 ;;
    *) ;;
  esac
  [ "${#1}" -eq 12 ]
}

load_or_create_node_name() (
  umask 077
  if [ -e "${node_identity_path}" ] || [ -L "${node_identity_path}" ]; then
    if [ ! -f "${node_identity_path}" ] || [ -L "${node_identity_path}" ]; then
      printf 'The retained node identity must be a regular file\n' >&2
      exit 1
    fi

    node_identity_size="$(wc -c <"${node_identity_path}" | tr -d '[:space:]')"
    node_name="$(cat "${node_identity_path}")"
    if [ "${node_identity_size}" != 13 ] || ! is_canonical_node_name "${node_name}"; then
      printf 'The retained node identity must contain exactly 12 lowercase hexadecimal characters\n' >&2
      exit 1
    fi
    printf '%s\n' "${node_name}"
    exit 0
  fi

  if [ -e "${data_root}/server/db" ]; then
    printf 'The retained K3s data volume requires a node identity backfill at %s\n' \
      "${node_identity_path}" >&2
    exit 1
  fi

  node_name="$(hostname)"
  if ! is_canonical_node_name "${node_name}"; then
    printf 'The container hostname must be 12 lowercase hexadecimal characters\n' >&2
    exit 1
  fi

  mkdir -p "${data_root}"
  node_identity_temporary="${node_identity_path}.tmp.$$"
  trap 'rm -f "${node_identity_temporary}"' 0 HUP INT TERM
  printf '%s\n' "${node_name}" >"${node_identity_temporary}"
  chmod 0400 "${node_identity_temporary}"
  mv -f "${node_identity_temporary}" "${node_identity_path}"
  trap - 0 HUP INT TERM
  printf '%s\n' "${node_name}"
)

node_name="$(load_or_create_node_name)"

materialize_registry_hosts() (
  registry_hosts_directory="${registry_hosts_root}/${1:?Registry authority is required}"
  registry_hosts_target="${registry_hosts_directory}/hosts.toml"
  umask 077
  if [ ! -f "${registry_hosts_source}" ]; then
    printf 'The immutable containerd hosts configuration is unavailable\n' >&2
    exit 1
  fi

  mkdir -p "${registry_hosts_directory}"
  chmod 0700 "${registry_hosts_directory}"
  if [ -f "${registry_hosts_target}" ] && [ ! -L "${registry_hosts_target}" ] \
    && cmp "${registry_hosts_source}" "${registry_hosts_target}" >/dev/null 2>&1; then
    chmod 0444 "${registry_hosts_target}"
    exit 0
  fi

  registry_hosts_temporary="${registry_hosts_target}.tmp.$$"
  trap 'rm -f "${registry_hosts_temporary}"' 0 HUP INT TERM
  cp "${registry_hosts_source}" "${registry_hosts_temporary}"
  chmod 0444 "${registry_hosts_temporary}"
  mv -f "${registry_hosts_temporary}" "${registry_hosts_target}"
)

materialize_registry_hosts "localhost:15100"
materialize_registry_hosts "127.0.0.1:15100"
# Floci starts the ECR child before injecting this headerless mirror. Remove it
# only after the native hosts contract is durable and before K3s reconciles it.
rm -f "${registry_mirror_path}"

# The three nested nodes share Colima's kernel and exhaust its default of 128
# instances during concurrent recovery. Each node applies the same host limit
# before containerd starts, so startup order cannot change the kernel contract.
printf '1024\n' >"${inotify_instances_path}"
inotify_instances="$(cat "${inotify_instances_path}")"
if [ "${inotify_instances}" -ne 1024 ]; then
  printf 'The kernel inotify instance limit does not match 1024\n' >&2
  exit 1
fi

cluster_memory_limit_kib=$((K3S_MEMORY_MIB * 1024))
if [ "${cluster_memory_limit_kib}" -gt "${node_memory_kib}" ]; then
  if [ -n "${K3S_INSTANCE_TYPE:-}" ] && [ "${K3S_MEMORY_MIB}" -eq "${instance_type_memory_mib}" ]; then
    cluster_memory_limit_kib=$((node_memory_kib - 2048 * 1024))
    if [ "${cluster_memory_limit_kib}" -lt $((1024 * 1024)) ]; then
      cluster_memory_limit_kib=$((node_memory_kib / 2))
    fi
    K3S_MEMORY_MIB=$((cluster_memory_limit_kib / 1024))
  else
    printf 'K3S_MEMORY_MIB must not exceed the host memory visible to k3s\n' >&2
    exit 1
  fi
fi
# A nested node sees the VM's MemTotal and its own working set. Translate its
# smaller allocation into memory.available so kubelet evicts one GiB before
# exhausting that allocation. Allocatable excludes both safeguards.
system_reserved_memory_kib=$((k3s_daemon_memory_reserve_mib * 1024))
eviction_memory_available_kib=$((node_memory_kib - cluster_memory_limit_kib + kubelet_eviction_memory_buffer_mib * 1024))
# Cell runtimes carry a zone and host user workloads. They must stop workload
# writes while the shared Colima filesystem still has enough space for the
# control runtime. The unzoned control runtime retains K3s's lower emergency
# threshold so cell pressure does not evict unrelated control-plane Pods.
nodefs_eviction_available_percent="${NODEFS_EVICTION_AVAILABLE_PERCENT:-5}"
if [ -n "${K3S_ZONE:-}" ]; then
  nodefs_eviction_available_percent="${NODEFS_EVICTION_AVAILABLE_PERCENT:-20}"
fi

case "${node_cpu_millicores}" in
  *[!0-9]* | "")
    printf 'The host CPU capacity is unavailable\n' >&2
    exit 1
    ;;
  *) ;;
esac
pod_cpu_millicores=$((K3S_CPU_MILLICORES - K3S_SYSTEM_CPU_MILLICORES))
if [ "${pod_cpu_millicores}" -ge "${node_cpu_millicores}" ]; then
  if [ "${node_cpu_millicores}" -gt "${K3S_SYSTEM_CPU_MILLICORES}" ]; then
    pod_cpu_millicores=$((node_cpu_millicores - K3S_SYSTEM_CPU_MILLICORES))
  else
    printf 'K3S_CPU_MILLICORES must leave CPU reserved for the host system\n' >&2
    exit 1
  fi
fi
system_reserved_cpu_millicores=$((node_cpu_millicores - pod_cpu_millicores))

# k3s embeds the API server, controller manager, scheduler, kubelet, and etcd
# in one Go process. The VM CPU count is not reduced by a CFS quota, so bound
# its scheduler concurrency to the whole CPUs inside this child share.
gomaxprocs=$((K3S_CPU_MILLICORES / 1000))
host_cores=$((node_cpu_millicores / 1000))
if [ "${gomaxprocs}" -gt "${host_cores}" ]; then
  gomaxprocs="${host_cores}"
fi
if [ "${gomaxprocs}" -lt 1 ]; then
  gomaxprocs=1
fi
export GOMAXPROCS="${gomaxprocs}"

# Rawfile LocalPV asks the kernel for loop devices after the node starts. Docker
# provides a writable /dev but no device manager to materialize those uevents.
# Listen before scanning so a device created between the two operations is not
# missed. Pre-create loop device nodes so they are immediately available.
for i in $(seq 0 63); do
  [ -e "/dev/loop${i}" ] || mknod "/dev/loop${i}" b 7 "${i}" 2>/dev/null || true
done
mdev -d
mdev -s

# Share existing mounts recursively so CSI mounts reach the kubelet. Binding
# /var/lib/kubelet over itself hides retained volume mounts below that path.
mount --make-rshared /

node_interface=""
attempt=0
while [ "${attempt}" -lt 30 ]; do
  node_interface="$(
    ip -4 -o route show "${K3S_SHARED_IPV4_CIDR}" 2>/dev/null \
      | awk 'NR == 1 { for (field = 1; field <= NF; field++) if ($field == "dev") { print $(field + 1); exit } }'
  )"
  [ -n "${node_interface}" ] && break

  attempt=$((attempt + 1))
  sleep 1
done

if [ -z "${node_interface}" ]; then
  printf 'No interface routes the shared k3s network %s\n' "${K3S_SHARED_IPV4_CIDR}" >&2
  exit 1
fi

if ! ip -4 -o address show dev "${node_interface}" \
  | awk -v expected="${K3S_NODE_IPV4_CIDR}" '$4 == expected { found = 1 } END { exit !found }'; then
  ip -4 address add "${K3S_NODE_IPV4_CIDR}" dev "${node_interface}"
fi

if ! grep -Fq 's3.amazonaws.com' "${hosts_path}" 2>/dev/null; then
  printf '172.19.0.2 s3.amazonaws.com\n' >>"${hosts_path}"
fi
node_ip="${K3S_NODE_IPV4_CIDR%/*}"
set -- "$@" "--node-name=${node_name}" "--node-label=hostname=${node_name}"
set -- "$@" "--node-label=node.kubernetes.io/instance-type=${K3S_INSTANCE_TYPE:-m5.large}"
set -- "$@" "--node-label=topology.kubernetes.io/region=${K3S_REGION:-us-west-2}"

if [ -n "${K3S_ZONE:-}" ]; then
  set -- "$@" "--node-label=topology.kubernetes.io/zone=${K3S_ZONE}"
  set -- "$@" "--node-label=karpenter.sh/capacity-type=on-demand"
fi

if [ "${1:-}" = "server" ]; then
  # Each Floci cluster has one server, so leader election cannot provide
  # failover and turns a transient shared-host stall into a full K3s exit.
  set -- "$@" \
    --cluster-init \
    --tls-san="${node_ip}" \
    --kube-cloud-controller-manager-arg=leader-elect=false \
    --kube-controller-manager-arg=leader-elect=false \
    --kube-scheduler-arg=leader-elect=false \
    --kube-apiserver-arg=anonymous-auth=false \
    --protect-kernel-defaults=true
fi

k3s_args=""
for arg in "$@"; do
  case "${arg}" in
    --cluster-cidr=* | --service-cidr=*) ;;
    *)
      if [ -z "${k3s_args}" ]; then
        set -- "${arg}"
        k3s_args=1
      else
        set -- "$@" "${arg}"
      fi
      ;;
  esac
done

exec "${K3S_BINARY:-/bin/k3s}" "$@" \
  --cluster-cidr="${K3S_POD_IPV4_CIDR}" \
  --flannel-iface="${node_interface}" \
  --node-ip="${node_ip}" \
  --service-cidr="${K3S_SERVICE_IPV4_CIDR}" \
  --snapshotter=stargz \
  --kubelet-arg=feature-gates=MutablePVNodeAffinity=true \
  --kubelet-arg=fail-swap-on=true \
  --kubelet-arg="image-gc-high-threshold=60" \
  --kubelet-arg="image-gc-low-threshold=40" \
  --kubelet-arg="eviction-hard=memory.available<${eviction_memory_available_kib}Ki,nodefs.available<${nodefs_eviction_available_percent}%,imagefs.available<${nodefs_eviction_available_percent}%" \
  --kubelet-arg="eviction-minimum-reclaim=nodefs.available=2%,imagefs.available=2%" \
  --kubelet-arg="system-reserved=cpu=${system_reserved_cpu_millicores}m,memory=${system_reserved_memory_kib}Ki" \
  --kube-apiserver-arg=feature-gates=MutablePVNodeAffinity=true
