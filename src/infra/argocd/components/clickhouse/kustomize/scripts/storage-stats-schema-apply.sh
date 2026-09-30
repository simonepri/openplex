#!/usr/bin/env bash
# Waits for ClickHouse readiness then applies the storage_stats rollup schema.

set -eu

: "${CLICKHOUSE_HOST:?CLICKHOUSE_HOST is required}"
: "${CLICKHOUSE_PORT:?CLICKHOUSE_PORT is required}"
: "${CLICKHOUSE_USERNAME:?CLICKHOUSE_USERNAME is required}"

client() {
  clickhouse-client --host "${CLICKHOUSE_HOST}" --port "${CLICKHOUSE_PORT}" \
    --user "${CLICKHOUSE_USERNAME}" "$@"
}
# First bring-up races ClickHouse's own reconciliation; wait for
# the server before submitting the ON CLUSTER DDL.
for attempt in $(seq 1 60); do
  # shellcheck disable=SC2310
  if client --query "SELECT 1" >/dev/null 2>&1; then
    break
  fi
  if [[ ${attempt} -eq 60 ]]; then
    echo "ClickHouse did not answer within 10 minutes" >&2
    exit 1
  fi
  sleep 10
done
client --queries-file /etc/storage-stats/schema.sql
echo "storage_stats rollup schema ensured"
