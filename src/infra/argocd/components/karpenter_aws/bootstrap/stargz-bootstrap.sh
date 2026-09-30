#!/usr/bin/env bash
# Installs and verifies pinned AWS eStargz snapshotter before nodeadm starts containerd and kubelet.

set -euo pipefail

readonly stargz_image="ghcr.io/containerd/stargz-snapshotter:v0.18.2@sha256:74cd59bda98d35bcd2e0067ccb8304e7d4ae25c9a1c8906c8d5322a3d806e441"
readonly stargz_socket=/run/containerd-stargz-grpc/containerd-stargz-grpc.sock
mount_dir="$(mktemp -d /tmp/stargz-mount.XXXXXX)"
readonly mount_dir

# shellcheck disable=SC2329 # Invoked by the EXIT trap.
cleanup() {
  ctr images unmount "${mount_dir}" 2>/dev/null || true
  rm -rf "${mount_dir}"
}
trap cleanup EXIT

# Pull and extract the snapshotter binary directly from the official OCI image
ctr images pull "${stargz_image}"
ctr images mount "${stargz_image}" "${mount_dir}"
"${mount_dir}/usr/local/bin/containerd-stargz-grpc" --version
install -m 0755 "${mount_dir}/usr/local/bin/containerd-stargz-grpc" /usr/local/bin/containerd-stargz-grpc
ctr images unmount "${mount_dir}"
rm -rf "${mount_dir}"

systemctl daemon-reload
systemctl enable --now stargz-snapshotter.service
for _ in {1..60}; do
  if systemctl is-active --quiet stargz-snapshotter.service && [[ -S ${stargz_socket} ]]; then
    exit 0
  fi
  sleep 1
done

systemctl status --no-pager stargz-snapshotter.service >&2 || true
exit 1
