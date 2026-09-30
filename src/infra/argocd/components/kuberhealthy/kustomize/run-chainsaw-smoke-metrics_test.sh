#!/bin/sh
# Unit tests for run-chainsaw-smoke-metrics.sh APIService condition parsing and error handling.

set -eu

script_dir="$(cd "$(dirname "$0")" && pwd)"
smoke_metrics_script="${script_dir}/run-chainsaw-smoke-metrics.sh"
if [ ! -f "${smoke_metrics_script}" ]; then
  smoke_metrics_script="$(find . -name run-chainsaw-smoke-metrics.sh | head -n 1)"
fi

test_tmp="$(mktemp -d)"
fake_bin="${test_tmp}/bin"
mkdir -p "${fake_bin}"

cleanup() {
  rm -rf "${test_tmp}"
}
trap cleanup EXIT

# Create a mock wget that simulates kube-apiserver responses based on TEST_APISERVER_MODE
cat >"${fake_bin}/wget" <<'EOF'
#!/bin/sh
set -eu

# Intercept POST to kuberhealthy reporting URL
for arg in "$@"; do
  case "${arg}" in
    *externalCheckStatus*)
      exit 0
      ;;
  esac
done

url="$(printf '%s\n' "$@" | grep 'https://kubernetes.default.svc.cluster.local' || true)"

case "${url}" in
  *apiservices/v1beta1.metrics.k8s.io*)
    case "${TEST_APISERVER_MODE:-available_true}" in
      available_true)
        cat << 'RESP'
{
  "kind": "APIService",
  "apiVersion": "apiregistration.k8s.io/v1",
  "status": {
    "conditions": [
      {
        "type": "Available",
        "status": "True",
        "reason": "Passed",
        "message": "all checks passed"
      }
    ]
  }
}
RESP
        ;;
      available_false_with_other_true)
        cat << 'RESP'
{
  "kind": "APIService",
  "apiVersion": "apiregistration.k8s.io/v1",
  "status": {
    "conditions": [
      {
        "type": "Available",
        "status": "False",
        "reason": "FailedDiscoveryCheck",
        "message": "failing or missing response from metrics server"
      },
      {
        "type": "CustomCondition",
        "status": "True",
        "reason": "Passed"
      }
    ]
  }
}
RESP
        ;;
      available_missing)
        cat << 'RESP'
{
  "kind": "APIService",
  "apiVersion": "apiregistration.k8s.io/v1",
  "status": {
    "conditions": [
      {
        "type": "SomeOtherCondition",
        "status": "True",
        "reason": "Passed"
      }
    ]
  }
}
RESP
        ;;
      probe_failed)
        exit 1
        ;;
      *)
        exit 1
        ;;
    esac
    ;;
  *apis/metrics.k8s.io/v1beta1/nodes*)
    cat << 'RESP'
{
  "kind": "NodeMetricsList",
  "apiVersion": "metrics.k8s.io/v1beta1",
  "items": [
    {
      "metadata": {"name": "node-1"},
      "usage": {"cpu": "100m", "memory": "256Mi"}
    }
  ]
}
RESP
    ;;
  *)
    exit 0
    ;;
esac
EOF

chmod +x "${fake_bin}/wget"

run_smoke_test() {
  mode="$1"
  output_file="${test_tmp}/output_${mode}.log"
  ret=0
  PATH="${fake_bin}:${PATH}" KH_SA_TOKEN="mock-token" TEST_APISERVER_MODE="${mode}" \
    sh "${smoke_metrics_script}" >"${output_file}" 2>&1 || ret=$?
  echo "${ret}"
}

# Test 1: Available=True should succeed
ret="$(run_smoke_test "available_true")"
if [ "${ret}" -ne 0 ]; then
  printf "Test 1 failed: expected exit 0 when Available=True, got %s\n" "${ret}" >&2
  cat "${test_tmp}/output_available_true.log" >&2
  exit 1
fi
printf "Test 1 passed: Available=True succeeds\n"

# Test 2: Available=False (with another condition True) should assert failure
ret="$(run_smoke_test "available_false_with_other_true")"
if [ "${ret}" -eq 0 ]; then
  printf "Test 2 failed: expected failure when Available=False, but got exit 0\n" >&2
  exit 1
fi
if ! grep -q "Kubernetes metrics APIService v1beta1.metrics.k8s.io is not Available" "${test_tmp}/output_available_false_with_other_true.log"; then
  printf "Test 2 failed: missing expected error message in output\n" >&2
  cat "${test_tmp}/output_available_false_with_other_true.log" >&2
  exit 1
fi
printf "Test 2 passed: Available=False asserts failure\n"

# Test 3: Available condition missing should assert failure
ret="$(run_smoke_test "available_missing")"
if [ "${ret}" -eq 0 ]; then
  printf "Test 3 failed: expected failure when Available condition is missing, but got exit 0\n" >&2
  exit 1
fi
printf "Test 3 passed: missing Available condition asserts failure\n"

# Test 4: Probe failed should assert failure
ret="$(run_smoke_test "probe_failed")"
if [ "${ret}" -eq 0 ]; then
  printf "Test 4 failed: expected failure when probe fails, but got exit 0\n" >&2
  exit 1
fi
printf "Test 4 passed: probe failure asserts failure\n"

printf "All run-chainsaw-smoke-metrics tests passed successfully!\n"
