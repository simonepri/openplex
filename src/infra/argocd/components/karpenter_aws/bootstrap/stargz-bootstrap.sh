#!/usr/bin/env bash
# Installs and verifies pinned AWS eStargz snapshotter before nodeadm starts containerd and kubelet.

set -euo pipefail

readonly stargz_version="v0.18.2"
readonly stargz_sha256="515a3c3af0012f192ace31fb79e910597977c77227e976680aeaaef6e9ae50a9"
readonly stargz_url="https://github.com/containerd/stargz-snapshotter/releases/download/${stargz_version}/stargz-snapshotter-${stargz_version}-linux-amd64.tar.gz"
readonly install_dir="${INSTALL_DIR:-/usr/local/bin}"
readonly tmp_dir="${TMP_DIR:-/tmp}"
readonly stargz_tarball="${tmp_dir}/stargz.tgz"
readonly stargz_socket="${STARGZ_SOCKET:-/run/containerd-stargz-grpc/containerd-stargz-grpc.sock}"
readonly stargz_root="${STARGZ_ROOT:-/var/lib/containerd-stargz-grpc}"

# shellcheck disable=SC2329 # Invoked by the EXIT trap.
cleanup() {
  rm -f "${stargz_tarball}"
}
trap cleanup EXIT

if [[ ! -x "${install_dir}/containerd-stargz-grpc" || ! -x "${install_dir}/ctr-remote" ]]; then
  curl -fsSL --retry 5 --retry-delay 2 --retry-connrefused -o "${stargz_tarball}" "${stargz_url}"
  echo "${stargz_sha256}  ${stargz_tarball}" | sha256sum -c -
  tar -C "${install_dir}" -xzf "${stargz_tarball}" containerd-stargz-grpc ctr-remote
  chmod +x "${install_dir}/containerd-stargz-grpc" "${install_dir}/ctr-remote"
fi

mkdir -p "${stargz_root}"

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
