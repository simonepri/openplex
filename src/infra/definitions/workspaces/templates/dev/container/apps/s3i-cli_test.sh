#!/usr/bin/env bash
# shellcheck disable=SC2016,SC2310,SC2312
# Tests s3i-cli DuckDB query generation and execution against sample Parquet manifests.

set -euo pipefail

subject="${1:-$(cd -- "$(dirname -- "$0")" && pwd)/s3i-cli.sh}"
test_root="$(mktemp -d "${TMPDIR:-/tmp}/s3i-cli-test.XXXXXX")"
cleanup() {
  rm -rf -- "${test_root}"
}
trap cleanup EXIT

mkdir -p "${test_root}/bin" "${test_root}/fs/s3/eaws-lh1/meta/stats/dt=latest"

printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  'printf "DUCKDB_CALLED: %s\n" "$*" >>"${TEST_ROOT}/duckdb.log"' \
  >"${test_root}/bin/duckdb"
chmod 0755 "${test_root}/bin/duckdb"

# Create a dummy parquet file
touch "${test_root}/fs/s3/eaws-lh1/meta/stats/dt=latest/shard-001.parquet"

export PATH="${test_root}/bin:${PATH}"
export TEST_ROOT="${test_root}"
export S3_TEST_PARQUET_GLOB="${test_root}/fs/s3/*/meta/stats/dt=latest/*.parquet"

# Test 1: s3i find
bash "${subject}" find "*.parquet" --limit 10
grep -F -- 'key ILIKE' "${test_root}/duckdb.log" >/dev/null
grep -F -- 'LIMIT 10' "${test_root}/duckdb.log" >/dev/null

# Test 1b: several filters join with AND
bash "${subject}" find "*.parquet" --min-size 1KB --max-size 1GB
grep -F -- "key ILIKE '%.parquet' AND size >= parse_bytes('1KB') AND size <= parse_bytes('1GB')" "${test_root}/duckdb.log" >/dev/null

# Test 2: s3i ls
bash "${subject}" ls eaws-lh1/scratch
grep -F -- 'WHERE key LIKE' "${test_root}/duckdb.log" >/dev/null

# Test 3: s3i du
bash "${subject}" du eaws-lh1
expected_agg="format_bytes(sum(size))"
grep -F -- "${expected_agg}" "${test_root}/duckdb.log" >/dev/null
grep -F -- 'GROUP BY cell, prefix' "${test_root}/duckdb.log" >/dev/null

# Test 4: s3i sql
bash "${subject}" sql "SELECT count(*) FROM objects;"
grep -F -- 'CREATE TEMP VIEW objects' "${test_root}/duckdb.log" >/dev/null

# Test 5: missing parquet sources exits 0 without error
unset S3_TEST_PARQUET_GLOB
empty_output="$(bash "${subject}" ls 2>&1)"
[[ -z ${empty_output} ]]

printf 'All s3i-cli tests passed.\n'
