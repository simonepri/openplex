#!/bin/sh
# Installs the histogramQuantile executable UDF binary into ClickHouse user scripts.

set -eu

mkdir -p /var/lib/clickhouse/user_scripts

if [ -f /var/lib/clickhouse/user_scripts/histogramQuantile ]; then
  exit 0
fi

if [ -f /preloaded/histogram-quantile.tar.gz ]; then
  tar -xzf /preloaded/histogram-quantile.tar.gz -C /tmp
  mv /tmp/histogram-quantile /var/lib/clickhouse/user_scripts/histogramQuantile
  chmod +x /var/lib/clickhouse/user_scripts/histogramQuantile
  exit 0
fi

version="v0.0.1"
node_os="$(uname -s | tr '[:upper:]' '[:lower:]')"
node_arch="$(uname -m | sed s/aarch64/arm64/ | sed s/x86_64/amd64/)"
case "${node_arch}" in
  amd64) archive_sha256="33997073eb6d82b7be4f27f9fc8ec9e28a395e0f20674d5d54bb2fa28a75488d" ;;
  arm64) archive_sha256="e5605ebffa82a450ebbcdf6cf19dad546e1e40d52dbf3e03cfd2d2d5b7394211" ;;
  *)
    echo "unsupported architecture: ${node_arch}" >&2
    exit 1
    ;;
esac

cd /tmp
wget -T 5 -O histogram-quantile.tar.gz "https://github.com/SigNoz/signoz/releases/download/histogram-quantile%2F${version}/histogram-quantile_${node_os}_${node_arch}.tar.gz"
printf '%s  %s\n' "${archive_sha256}" histogram-quantile.tar.gz | sha256sum -c -
tar -xzf histogram-quantile.tar.gz
chmod +x histogram-quantile
mv histogram-quantile /var/lib/clickhouse/user_scripts/histogramQuantile
