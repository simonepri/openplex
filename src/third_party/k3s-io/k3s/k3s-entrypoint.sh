#!/bin/sh
# Starts k3s with network, registry, and single-server contracts.

# shellcheck disable=SC2310
set -eu

mountinfo_path="${K3S_MOUNTINFO_PATH:-/proc/self/mountinfo}"
if [ -f "${mountinfo_path}" ]; then
  if grep -q -- "-cell-eaws-lh1" "${mountinfo_path}" 2>/dev/null; then
    K3S_NODE_IPV4_CIDR="172.19.255.11/16"
    K3S_POD_IPV4_CIDR="10.241.0.0/16"
    K3S_SERVICE_IPV4_CIDR="172.31.16.0/20"
    K3S_SHARED_IPV4_CIDR="172.19.0.0/16"
  elif grep -q -- "-ctrl-eaws-lh1" "${mountinfo_path}" 2>/dev/null; then
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

registry_mirror_path="${K3S_REGISTRIES_PATH:-/etc/rancher/k3s/registries.yaml}"
# Floci starts the ECR child before injecting this headerless mirror. Remove it
# only after the native hosts contract is durable and before K3s reconciles it.
rm -f "${registry_mirror_path}"

# The three nested nodes share Colima's kernel and exhaust its default of 128
# instances during concurrent recovery. Each node applies the same host limit
# before containerd starts, so startup order cannot change the kernel contract.
inotify_instances_path="${K3S_PROC_SYS_ROOT:-/proc/sys}/fs/inotify/max_user_instances"
printf '1024\n' >"${inotify_instances_path}"
inotify_instances="$(cat "${inotify_instances_path}")"
if [ "${inotify_instances}" -ne 1024 ]; then
  printf 'The kernel inotify instance limit does not match 1024\n' >&2
  exit 1
fi

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

node_ip="${K3S_NODE_IPV4_CIDR%/*}"

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
  --kubelet-arg="eviction-minimum-reclaim=nodefs.available=2%,imagefs.available=2%" \
  --kube-apiserver-arg=feature-gates=MutablePVNodeAffinity=true
