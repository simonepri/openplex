#!/usr/bin/env sh
# Installs reviewed Headlamp plugin set into shared volume before the main container starts.

# shellcheck shell=sh
set -e
extract_plugin() {
  destination="$1"
  archive="$2"
  checksum="$3"

  mkdir -p "${destination}"
  printf '%s  %s\n' "${checksum}" "${archive}" | sha256sum -cs
  tar -xzf "${archive}" -C "${destination}" --strip-components=1
  test -s "${destination}/main.js"
  test -s "${destination}/package.json"
}

install_remote_plugin() {
  destination="$1"
  url="$2"
  checksum="$3"
  archive="${destination}/plugin.tar.gz"

  mkdir -p "${destination}"
  curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
    --connect-timeout 10 --max-time 60 --retry 3 --retry-max-time 120 \
    --output "${archive}" "${url}"
  extract_plugin "${destination}" "${archive}" "${checksum}"
  rm "${archive}"
}

install_configured_plugin() {
  plugin_name="$1"
  destination="$2"
  url="$3"
  checksum="$4"

  install_remote_plugin "${destination}" "${url}" "${checksum}"
  if [ -f "/plugin-config/${plugin_name}.json" ]; then
    cp "/plugin-config/${plugin_name}.json" "${destination}/config.json"
  fi
}

install_configured_plugin \
  signoz-observability-links \
  /plugins/signoz \
  https://github.com/simonepri/signoz-headlamp-plugin/releases/download/v0.1.3/signoz-headlamp-plugin-0.1.3.tar.gz \
  89b66cb022330e3739ce9b507ea37f86f30d45d459ba2b5bcf46cd3d6bc92dec
install_configured_plugin \
  parca-profile-links \
  /plugins/parca \
  https://github.com/simonepri/parca-headlamp-plugin/releases/download/v0.1.3/parca-headlamp-plugin-0.1.3.tar.gz \
  4fb023d2a6c3ea5ff70114753a5cb0850bbccb2a2141e23b32062f8ca1207dfe

install_remote_plugin \
  /plugins/catalog/cert-manager \
  https://github.com/headlamp-k8s/plugins/releases/download/cert-manager-0.1.1/headlamp-k8s-cert-manager-0.1.1.tar.gz \
  2b06caae0a207e2c30ce0d21bc4d672ae0a71a2cffc23a4585199110533032ab
install_remote_plugin \
  /plugins/catalog/karpenter \
  https://github.com/headlamp-k8s/plugins/releases/download/karpenter-0.2.0/headlamp-k8s-karpenter-0.2.0.tar.gz \
  5c00da78e573cb1263c6e27bc1430c0ccede50556e215ffa46f3750debaf731b
install_remote_plugin \
  /plugins/catalog/keda \
  https://github.com/headlamp-k8s/plugins/releases/download/keda-0.1.2/headlamp-k8s-keda-0.1.2.tar.gz \
  979996aa0d0efdbc852baf6977e178c955264e346fea2c82df94b2d58ef57501
extract_plugin \
  /plugins/catalog/kueue \
  /embedded-plugins/headlamp-k8s-kueue-0.1.0-alpha.tar.gz \
  232bca3f18712d8f0113930adf230088f709c4ba08f9988acce8bb025fa790df
install_remote_plugin \
  /plugins/catalog/kyverno \
  https://github.com/headlamp-k8s/plugins/releases/download/kyverno-0.1.0/headlamp-k8s-kyverno-0.1.0.tar.gz \
  eb6d99eed8e2e4187e5d5e007c116b9e10224cd403d83f8aa87780fafcf6e059
install_remote_plugin \
  /plugins/catalog/opencost \
  https://github.com/headlamp-k8s/plugins/releases/download/opencost-0.1.3/headlamp-k8s-opencost-0.1.3.tar.gz \
  cb3e67aa5bb8cc4869037b9024f58c95acfc1bc23ab6cbc80aa36e0c37ec21bd
install_remote_plugin \
  /plugins/catalog/trivy \
  https://github.com/kubebeam/trivy-headlamp-plugin/releases/download/v0.3.2/trivy-headlamp-plugin-v0.3.2.tar.gz \
  00b6ab4e49a99fe47ac99824779179f2540fb9c58d619a4100ddb4e9feee9efe
