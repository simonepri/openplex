#!/usr/bin/env bash
# Installs and verifies pinned AWS eStargz snapshotter before nodeadm starts containerd and kubelet.

set -euo pipefail

readonly stargz_version="v0.18.2"
readonly stargz_sha256="515a3c3af0012f192ace31fb79e910597977c77227e976680aeaaef6e9ae50a9"
readonly stargz_url="https://github.com/containerd/stargz-snapshotter/releases/download/${stargz_version}/stargz-snapshotter-${stargz_version}-linux-amd64.tar.gz"
readonly stargz_tarball=/tmp/stargz.tgz
readonly stargz_socket=/run/containerd-stargz-grpc/containerd-stargz-grpc.sock
readonly containerd_hosts_file="${CONTAINERD_HOSTS_FILE:-/etc/containerd/certs.d/_default/hosts.toml}"
readonly containerd_certs_dir="${containerd_hosts_file%/*}"

# shellcheck disable=SC2329 # Invoked by the EXIT trap.
cleanup() {
  rm -f "${stargz_tarball}"
}
trap cleanup EXIT

if [[ ! -x /usr/local/bin/containerd-stargz-grpc || ! -x /usr/local/bin/ctr-remote ]]; then
  curl -fsSL --retry 5 --retry-delay 2 --retry-connrefused -o "${stargz_tarball}" "${stargz_url}"
  echo "${stargz_sha256}  ${stargz_tarball}" | sha256sum -c -
  tar -C /usr/local/bin -xzf "${stargz_tarball}" containerd-stargz-grpc ctr-remote
  chmod +x /usr/local/bin/containerd-stargz-grpc /usr/local/bin/ctr-remote
fi

mkdir -p /var/lib/containerd-stargz-grpc

mkdir -p "${containerd_certs_dir}"
chmod 0755 "${containerd_certs_dir}"
cat >"${containerd_hosts_file}" <<'EOF'
server = "https://registry-1.docker.io"

[host."http://127.0.0.1:4001"]
  capabilities = ["pull", "resolve"]
EOF

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
