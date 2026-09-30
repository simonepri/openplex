#!/usr/bin/env bash
# Tests Chainsaw runner test discovery, parameter parsing, and empty suite selection rejection to defend test automation reliability.

# shellcheck disable=SC2312
set -euo pipefail

runner="${PWD}/${1:?runner required}"
chainsaw="${PWD}/${2:?chainsaw required}"
jq="${PWD}/${3:?jq required}"
workspace="$(mktemp -d)"
trap 'rm -rf "${workspace}"' EXIT
export BUILD_WORKSPACE_DIRECTORY="${workspace}"
suite_dir="${workspace}/src/infra/definitions/conformance"
mkdir -p "${suite_dir}/fixtures"
cat >"${suite_dir}/.chainsaw.yaml" <<'YAML'
apiVersion: chainsaw.kyverno.io/v1alpha1
kind: Configuration
metadata:
  name: runner-test
spec:
  parallel: 1
YAML
printf '{}\n' >"${suite_dir}/values.yaml"
cat >"${suite_dir}/fixtures/first.test.k8s.yaml" <<'YAML'
apiVersion: v1
kind: ConfigMap
metadata:
  name: fixture
YAML

for name in first second; do
  cat >"${suite_dir}/${name}.test.k8s.yaml" <<YAML
apiVersion: chainsaw.kyverno.io/v1alpha1
kind: Test
metadata:
  name: ${name}
spec:
  steps:
    - try:
        - command:
            entrypoint: sh
            args: [-c, 'printf "%s\\n" "${name}" >> executed']
YAML
done

bash "${runner}" "${chainsaw}" "${jq}" --no-cluster
test "$(sort "${suite_dir}/executed")" = "$(printf 'first\nsecond')"
rm "${suite_dir}/executed"

bash "${runner}" "${chainsaw}" "${jq}" --no-cluster --test-file second.test.k8s.yaml \
  --report-format JSON --report-path "${workspace}" --report-name selected
test "$(cat "${suite_dir}/executed")" = second
"${jq}" --exit-status '.tests | length == 1 and .[0].name == "second" and .[0].status == "passed"' "${workspace}/selected.json" >/dev/null
rm "${suite_dir}/executed"

for selection in '--test-file=missing.test.k8s.yaml' '--include-test-regex=does-not-exist' '--selector=missing=true' '--test-file='; do
  if bash "${runner}" "${chainsaw}" "${jq}" --no-cluster "${selection}"; then
    printf 'Runner accepted empty selection: %s\n' "${selection}" >&2
    exit 1
  fi
  test ! -e "${suite_dir}/executed"
done

rm "${suite_dir}"/*.test.k8s.yaml
if bash "${runner}" "${chainsaw}" "${jq}" --no-cluster; then
  printf 'Runner accepted a directory without suites.\n' >&2
  exit 1
fi
