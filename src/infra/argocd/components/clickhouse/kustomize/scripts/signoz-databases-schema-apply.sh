#!/usr/bin/env bash
# Creates SigNoz telemetry databases and applies system log retention migrations.

set -eu
shopt -s inherit_errexit

: "${CLICKHOUSE_HOST:?CLICKHOUSE_HOST is required}"
: "${CLICKHOUSE_PORT:?CLICKHOUSE_PORT is required}"
: "${CLICKHOUSE_USERNAME:?CLICKHOUSE_USERNAME is required}"

client() {
  clickhouse-client --host "${CLICKHOUSE_HOST}" --port "${CLICKHOUSE_PORT}" \
    --user "${CLICKHOUSE_USERNAME}" --max_query_size 10485760 \
    --max_execution_time 300 "$@"
}
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
client --queries-file /etc/signoz-databases/schema.sql
echo "SigNoz telemetry databases ensured"

recalculate_system_log_ttl() (
  local table=$1 partitions partition merge_bytes restore_merges pending attempt
  merge_bytes=$(client --param_table="${table}" --query "
    SELECT extract(engine_full, 'max_bytes_to_merge_at_max_space_in_pool = ([0-9]+)')
    FROM system.tables WHERE database = 'system' AND name = {table:String}
    FORMAT TSVRaw")
  restore_merges="ALTER TABLE system.${table} RESET SETTING max_bytes_to_merge_at_max_space_in_pool"
  if [[ -n ${merge_bytes} ]] && [[ ${merge_bytes} != 0 ]]; then
    restore_merges="ALTER TABLE system.${table} MODIFY SETTING max_bytes_to_merge_at_max_space_in_pool = ${merge_bytes}"
  fi
  partitions=$(system_log_partitions_without_ttl "${table}")
  if [[ -z ${partitions} ]]; then
    # A terminated hook may leave its temporary pause after metadata was repaired.
    [[ ${merge_bytes} != 0 ]] || client --query "${restore_merges}"
    exit 0
  fi
  trap 'client --query "$restore_merges"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM

  # Regular merges are selected before mutations; failed merges can starve repair.
  client --query "ALTER TABLE system.${table} MODIFY SETTING max_bytes_to_merge_at_max_space_in_pool = 0"
  for attempt in $(seq 1 120); do
    pending=$(client --param_table="${table}" --query "
      SELECT count() FROM system.mutations
      WHERE database = 'system' AND table = {table:String} AND NOT is_done")
    [[ ${pending} != 0 ]] || break
    if [[ ${attempt} -eq 120 ]]; then
      echo "Pending mutations did not complete for system.${table} within 10 minutes" >&2
      exit 1
    fi
    sleep 5
  done
  partitions=$(system_log_partitions_without_ttl "${table}")
  while IFS= read -r partition; do
    [[ -n ${partition} ]] || continue
    client --param_partition="${partition}" --query "
      ALTER TABLE system.${table} MATERIALIZE TTL IN PARTITION ID {partition:String}
        SETTINGS mutations_sync = 1"
  done <<<"${partitions}"
)

system_log_partitions_without_ttl() {
  client --param_table="$1" --query "
    SELECT DISTINCT partition_id FROM system.parts
    WHERE database = 'system' AND table = {table:String} AND active
      AND delete_ttl_info_max = 0
    ORDER BY partition_id
    FORMAT TSVRaw"
}

# LINT.IfChange(clickhouse-system-log-retention)
reconcile_system_logs() {
  local tables table retention_days vertical_rows settings current_ttl target_ttl
  # Schema changes preserve old tables with replica-local numeric suffixes.
  tables=$(client --query "
    SELECT name FROM system.tables
    WHERE database = 'system'
      AND match(name, '^(asynchronous_metric|background_schedule_pool|error|metric|processors_profile|query|query_metric|query_views|text|trace)_log(_[0-9]+)?$')
    ORDER BY name
    FORMAT TSVRaw")
  while IFS= read -r table; do
    [[ -n ${table} ]] || continue
    retention_days=3
    vertical_rows=0
    case "${table}" in
      asynchronous_metric_log* | metric_log*) vertical_rows=1 ;;
      error_log* | query_views_log*) retention_days=7 ;;
      query_log*)
        retention_days=7
        vertical_rows=10000
        ;;
      *) ;;
    esac
    current_ttl=$(client --param_table="${table}" --query "
      SELECT extract(engine_full, 'TTL ([^ ]+ \\+ [^ ]+)')
      FROM system.tables WHERE database = 'system' AND name = {table:String}
      FORMAT TSVRaw")
    target_ttl="event_date + toIntervalDay(${retention_days})"
    if [[ ${current_ttl} != "${target_ttl}" ]]; then
      settings="ttl_only_drop_parts = 1, materialize_ttl_recalculate_only = 1"
      if [[ ${vertical_rows} -gt 0 ]]; then
        settings+=", enable_vertical_merge_algorithm = 1,
          vertical_merge_algorithm_min_rows_to_activate = ${vertical_rows},
          vertical_merge_algorithm_min_bytes_to_activate = 1,
          vertical_merge_algorithm_min_columns_to_activate = 10"
      fi
      client --multiquery --query "
        ALTER TABLE system.${table} MODIFY SETTING ${settings};
        ALTER TABLE system.${table} MODIFY TTL event_date + INTERVAL ${retention_days} DAY
          SETTINGS materialize_ttl_after_modify = 0;"
    fi
    recalculate_system_log_ttl "${table}"
  done <<<"${tables}"
}
# LINT.ThenChange(//src/infra/argocd/components/clickhouse/kustomize/clickhouse.yaml:clickhouse-system-log-retention)

reconcile_telemetry_tables() {
  local tables db table storage_policy engine_full time_expr retention_clause target_ttl existing_ttl extracted_time_expr
  tables=$(client --query "
    SELECT database, name FROM system.tables
    WHERE database IN ('signoz_logs', 'signoz_traces', 'signoz_metrics', 'signoz_meter')
      AND match(engine, 'MergeTree')
      AND name NOT LIKE '%schema_migrations%'
    ORDER BY database, name
    FORMAT TSVRaw")
  while IFS=$'\t' read -r db table; do
    [[ -n ${db} ]] && [[ -n ${table} ]] || continue
    storage_policy=$(client --param_db="${db}" --param_table="${table}" --query "
      SELECT storage_policy FROM system.tables
      WHERE database = {db:String} AND name = {table:String}
      FORMAT TSVRaw")
    if [[ ${storage_policy} != "hot_to_cold_policy" ]]; then
      client --query "
        ALTER TABLE ${db}.${table} MODIFY SETTING storage_policy = 'hot_to_cold_policy';"
    fi

    engine_full=$(client --param_db="${db}" --param_table="${table}" --query "
      SELECT engine_full FROM system.tables
      WHERE database = {db:String} AND name = {table:String}
      FORMAT TSVRaw")

    if [[ ${engine_full} =~ TO[[:space:]]+VOLUME[[:space:]]+\'cold\' ]]; then
      continue
    fi

    if [[ ! ${engine_full} =~ TTL[[:space:]]+ ]]; then
      continue
    fi

    time_expr=""
    retention_clause=""
    case "${db}" in
      signoz_logs)
        time_expr="toDateTime(timestamp / 1000000000)"
        retention_clause="toDateTime(timestamp / 1000000000) + INTERVAL 30 DAY DELETE"
        ;;
      signoz_traces)
        time_expr="toDateTime(timestamp)"
        retention_clause="toDateTime(timestamp) + INTERVAL 30 DAY DELETE"
        ;;
      signoz_metrics)
        if [[ ${table} == "samples_v4" ]]; then
          time_expr="toDateTime(unix_milli / 1000)"
          retention_clause="toDateTime(unix_milli / 1000) + INTERVAL 90 DAY DELETE"
        else
          time_expr="timestamp"
          retention_clause="timestamp + INTERVAL 90 DAY DELETE"
        fi
        ;;
      signoz_meter)
        time_expr="timestamp"
        retention_clause="timestamp + INTERVAL 30 DAY DELETE"
        ;;
      *) ;;
    esac

    if [[ ${engine_full} =~ TTL[[:space:]]+ ]]; then
      existing_ttl=$(client --param_db="${db}" --param_table="${table}" --query "
        SELECT extract(engine_full, 'TTL ([^;]+?)(?: SETTINGS|$)')
        FROM system.tables WHERE database = {db:String} AND name = {table:String}
        FORMAT TSVRaw")
      if [[ -n ${existing_ttl} ]]; then
        extracted_time_expr=$(client --param_db="${db}" --param_table="${table}" --query "
          SELECT trim(LEADING ' (' FROM extract(engine_full, 'TTL \\(?(.+?) \\+ (?:toInterval|INTERVAL)'))
          FROM system.tables WHERE database = {db:String} AND name = {table:String}
          FORMAT TSVRaw")
        if [[ -n ${extracted_time_expr} ]]; then
          time_expr="${extracted_time_expr}"
        fi
        retention_clause="${existing_ttl}"
        if [[ ! ${retention_clause} =~ DELETE ]]; then
          retention_clause="${retention_clause} DELETE"
        fi
      fi
    fi

    if [[ -n ${time_expr} ]] && [[ -n ${retention_clause} ]]; then
      target_ttl="${time_expr} + INTERVAL 7 DAY TO VOLUME 'cold', ${retention_clause}"
      client --query "
        ALTER TABLE ${db}.${table} MODIFY TTL ${target_ttl}
          SETTINGS materialize_ttl_after_modify = 0;"
    fi
  done <<<"${tables}"
}

replicas=$(client --query "
  SELECT DISTINCT host_name, port FROM system.clusters
  WHERE cluster = getMacro('cluster')
  ORDER BY host_name, port
  FORMAT TSVRaw")
while IFS=$'\t' read -r host port; do
  CLICKHOUSE_HOST="${host}" CLICKHOUSE_PORT="${port}" reconcile_system_logs
  CLICKHOUSE_HOST="${host}" CLICKHOUSE_PORT="${port}" reconcile_telemetry_tables
done <<<"${replicas}"
echo "ClickHouse system log retention ensured"
echo "SigNoz telemetry storage policy and TTL ensured"
