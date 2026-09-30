#!/usr/bin/env bash
# Rebuilds the storage_stats serving cache from durable manifests or raw inventories.

# shellcheck disable=SC2310
set -euo pipefail

: "${CLICKHOUSE_HOST:?CLICKHOUSE_HOST is required}"
: "${CLICKHOUSE_PORT:?CLICKHOUSE_PORT is required}"
: "${CLICKHOUSE_USERNAME:?CLICKHOUSE_USERNAME is required}"
: "${INVENTORY_DATE_EXPR:=yesterday}"
: "${STATS_MANIFEST_SCHEMA:=1}"
: "${INVENTORY_ENABLED:=true}"
: "${INVENTORY_PREFIX_DEPTH:=1}"
: "${INVENTORY_ROW_FILTER:=1}"
: "${INVENTORY_MANIFEST_NAME:=manifest.json}"
: "${INVENTORY_MANIFEST_CHECKSUM_NAME:=manifest.checksum}"
: "${INVENTORY_MANIFEST_FILE_KEY:=file}"
: "${INVENTORY_MANIFEST_FILTER:=1}"

client() {
  clickhouse-client --host "${CLICKHOUSE_HOST}" --port "${CLICKHOUSE_PORT}" \
    --user "${CLICKHOUSE_USERNAME}" --async_insert=0 "$@"
}
for attempt in $(seq 1 60); do
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
report_date="$(date -u -d "${INVENTORY_DATE_EXPR}" +%F)"
report_year="${report_date%%-*}"
report_month_and_day="${report_date#*-}"
report_month="${report_month_and_day%%-*}"
report_day="${report_month_and_day#*-}"
stats_secret_dir=/var/run/secrets/storage-stats-gateway
inventory_table_function="${INVENTORY_TABLE_FUNCTION:-}"
[[ -n ${inventory_table_function} ]] || inventory_table_function="<empty>"
case "${inventory_table_function}" in
  s3) ;;
  *)
    echo "unsupported inventory table function: ${inventory_table_function}" >&2
    exit 1
    ;;
esac
tmp_dir="$(mktemp -d /tmp/storage-stats.XXXXXX)"
chmod 700 "${tmp_dir}"
cleanup() {
  rm -rf "${tmp_dir}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

run_local_query() {
  local query="$1"
  local query_file
  local status
  query_file="$(mktemp "${tmp_dir}/local-query.XXXXXX")"
  chmod 600 "${query_file}"
  printf '%s\n' "${query}" >"${query_file}"
  if clickhouse-local --queries-file "${query_file}"; then
    status=0
  else
    status=$?
  fi
  rm -f "${query_file}"
  return "${status}"
}

run_client_query() {
  local query="$1"
  shift
  local query_file
  local status
  query_file="$(mktemp "${tmp_dir}/client-query.XXXXXX")"
  chmod 600 "${query_file}"
  printf '%s\n' "${query}" >"${query_file}"
  if client --queries-file "${query_file}" "$@"; then
    status=0
  else
    status=$?
  fi
  rm -f "${query_file}"
  return "${status}"
}

sql_escape() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\'/\\\'}"
  printf '%s' "${value}"
}

replay_stats() {
  local cell="$1"
  local credential_name="$2"
  local manifest_url_template="$3"
  local access_key
  local access_key_sql
  local gateway_base
  local manifest_rows
  local manifest_schema_display
  local manifest_url
  local secret_key
  local secret_key_sql
  local shard_key
  local shard_key_sql
  local shard_size
  local shard_url
  local shard_url_sql
  local sources=""
  access_key="$(cat "${stats_secret_dir}/${credential_name}-access-key-id")"
  secret_key="$(cat "${stats_secret_dir}/${credential_name}-secret-access-key")"
  access_key_sql="$(sql_escape "${access_key}")"
  secret_key_sql="$(sql_escape "${secret_key}")"
  manifest_url="${manifest_url_template//\{date\}/${report_date}}"
  gateway_base="${manifest_url%/manifests/stats/*}"
  manifest_rows="$(run_local_query "
    SELECT
      JSONExtractUInt(json, 'schema'),
      arraySort(JSONExtractKeys(json)) = ['objects', 'schema'],
      JSONExtractString(file, 'key'),
      JSONExtractUInt(file, 'size'),
      has(JSONExtractKeys(file), 'checksum'),
      if(has(JSONExtractKeys(file), 'checksum'), toString(JSONType(file, 'checksum')), '-'),
      if(has(JSONExtractKeys(file), 'checksum'), if(empty(JSONExtractString(file, 'checksum', 'algorithm')), '-', JSONExtractString(file, 'checksum', 'algorithm')), '-'),
      if(has(JSONExtractKeys(file), 'checksum'), if(empty(JSONExtractString(file, 'checksum', 'value')), '-', JSONExtractString(file, 'checksum', 'value')), '-'),
      arraySort(JSONExtractKeys(file)) IN (['key', 'size'], ['checksum', 'key', 'size'])
    FROM s3('${manifest_url}', '${access_key_sql}', '${secret_key_sql}', 'RawBLOB', 'json String')
    ARRAY JOIN JSONExtractArrayRaw(json, 'objects') AS file
    FORMAT TSVRaw
  ")" || return
  [[ -n ${manifest_rows} ]] || return
  while IFS=$'\t' read -r manifest_schema manifest_shape_valid shard_key shard_size checksum_present checksum_type checksum_algorithm checksum_value object_shape_valid; do
    [[ ${manifest_shape_valid} == 1 ]] || {
      echo "${cell}: stats manifest has unknown or missing fields" >&2
      return 1
    }
    [[ ${object_shape_valid} == 1 ]] || {
      echo "${cell}: stats manifest object has unknown or missing fields" >&2
      return 1
    }
    manifest_schema_display="${manifest_schema:-}"
    [[ -n ${manifest_schema_display} ]] || manifest_schema_display="<empty>"
    [[ ${manifest_schema} == "${STATS_MANIFEST_SCHEMA}" ]] || {
      echo "${cell}: unsupported stats manifest schema ${manifest_schema_display}" >&2
      return 1
    }
    case "${shard_key}" in
      "meta/stats/dt=${report_date}/"*.parquet) ;;
      *)
        echo "${cell}: stats manifest key escapes its day partition: ${shard_key}" >&2
        return 1
        ;;
    esac
    case "${shard_size}" in
      '' | *[!0-9]*)
        echo "${cell}: stats shard has invalid size: ${shard_key}" >&2
        return 1
        ;;
      *) ;;
    esac
    [[ ${shard_size} -gt 0 ]] || {
      echo "${cell}: stats shard is empty: ${shard_key}" >&2
      return 1
    }
    shard_url="${gateway_base}/${shard_key#meta/}"
    shard_url_sql="$(sql_escape "${shard_url}")"
    shard_key_sql="$(sql_escape "${shard_key}")"
    actual_size="$(run_local_query "
      SELECT length(data)
      FROM s3('${shard_url_sql}', '${access_key_sql}', '${secret_key_sql}', 'RawBLOB', 'data String')
      FORMAT TSVRaw
    ")" || return
    [[ ${actual_size} == "${shard_size}" ]] || {
      echo "${cell}: stats shard size mismatch for ${shard_key_sql}" >&2
      return 1
    }
    if [[ ${checksum_present} == 1 ]]; then
      [[ ${checksum_type} == Object ]] || return 1
      case "${checksum_algorithm}:${checksum_value}" in
        sha256:???????????????????????????????????????????????????????????????? | md5:???????????????????????????????? | crc32c:????????) ;;
        *)
          echo "${cell}: malformed optional checksum for ${shard_key}" >&2
          return 1
          ;;
      esac
      case "${checksum_value}" in
        *[!0-9a-f]*)
          echo "${cell}: checksum is not lowercase hex for ${shard_key}" >&2
          return 1
          ;;
        *) ;;
      esac
      echo "${cell}: stats shard checksum declared for alerting: ${shard_key}"
    fi
    if [[ -n ${sources} ]]; then sources+=$'\nUNION ALL\n'; fi
    sources+="SELECT * FROM s3('${shard_url_sql}', '${access_key_sql}', '${secret_key_sql}', 'Parquet', 'day Date, cell String, storage_class String, team String, prefix String, object_count UInt64, total_bytes UInt64, max_last_modified DateTime64(3, \'UTC\'), ingested_at DateTime')"
  done <<<"${manifest_rows}"
  run_client_query "SELECT count() FROM (${sources}) WHERE day = toDate('${report_date}') AND cell = {cell:String}" \
    --param_cell="${cell}" >/dev/null || return
  run_client_query "ALTER TABLE storage_stats.storage_prefix_daily_local ON CLUSTER '{cluster}' DELETE WHERE day = toDate('${report_date}') AND cell = {cell:String} SETTINGS mutations_sync = 2" \
    --param_cell="${cell}" || return
  run_client_query "
    INSERT INTO storage_stats.storage_prefix_daily
      (day, cell, storage_class, team, prefix, object_count, total_bytes, max_last_modified, ingested_at)
    SELECT
      day, cell, storage_class, team, prefix, object_count, total_bytes, max_last_modified, ingested_at
    FROM (${sources})
    WHERE day = toDate('${report_date}') AND cell = {cell:String}
  " --param_cell="${cell}" || return
  echo "replayed ${cell} durable storage stats for ${report_date}"
}

if [[ -n ${STATS_SOURCES:-} ]]; then
  source_replay_failed=false
  while IFS='|' read -r cell credential_name url_template; do
    [[ -n ${cell} ]] || continue
    [[ -n ${credential_name} ]] || {
      echo "${cell}: credential name is required" >&2
      exit 1
    }
    [[ -n ${url_template} ]] || {
      echo "${cell}: stats URL is required" >&2
      exit 1
    }
    if ! replay_stats "${cell}" "${credential_name}" "${url_template}"; then
      run_client_query "ALTER TABLE storage_stats.storage_prefix_daily_local ON CLUSTER '{cluster}' DELETE WHERE day = toDate('${report_date}') AND cell = {cell:String} SETTINGS mutations_sync = 2" \
        --param_cell="${cell}"
      echo "${cell}: durable stats are unavailable for ${report_date}; serving-cache partition is absent" >&2
      source_replay_failed=true
    fi
  done <<<"${STATS_SOURCES}"
  if [[ ${source_replay_failed} == true ]]; then
    exit 1
  fi
  exit 0
fi
if [[ ${INVENTORY_ENABLED} != "true" ]]; then
  echo "schema ensured; inventory ingestion disabled: ${INVENTORY_BLOCK_REASON:-provider profile is disabled}"
  exit 0
fi
if [[ -n ${INVENTORY_BLOCK_REASON:-} ]]; then
  echo "inventory ingestion is enabled without clearing its block reason: ${INVENTORY_BLOCK_REASON}" >&2
  exit 1
fi

ingest_sources() {
  local cell="$1"
  local storage_class="$2"
  local key_prefix_to_strip="$3"
  local logical_namespace="$4"
  local sources="$5"
  local rows
  rows="$(run_local_query "
    WITH if(
      empty('${key_prefix_to_strip}'),
      object_key,
      substring(object_key, length('${key_prefix_to_strip}') + 1)
    ) AS physical_key,
    if(startsWith(physical_key, 'global/'), 'global', '${cell}') AS scope,
    multiIf(
      startsWith(physical_key, concat('global/', '${storage_class}', '/')),
      substring(physical_key, length(concat('global/', '${storage_class}', '/')) + 1),
      startsWith(physical_key, concat('${storage_class}', '/')),
      substring(physical_key, length(concat('${storage_class}', '/')) + 1),
      physical_key
    ) AS relative_key,
    splitByChar('/', relative_key) AS rel_segments
    SELECT
      toDate('${report_date}') AS day,
      '${cell}' AS cell,
      '${storage_class}' AS storage_class,
      arrayElement(rel_segments, 1) AS team,
      concat(
        's3/',
        scope,
        '/',
        '${storage_class}',
        '/',
        arrayStringConcat(
          arraySlice(
            rel_segments,
            1,
            if(
              arrayElement(rel_segments, 2) = 'workspaces',
              3,
              if(
                scope = 'global' AND '${storage_class}' = 'home',
                toUInt32(${INVENTORY_PREFIX_DEPTH}) + 1,
                toUInt32(${INVENTORY_PREFIX_DEPTH})
              )
            )
          ),
          '/'
        )
      ) AS prefix,
      count() AS object_count,
      sum(object_size) AS total_bytes,
      max(object_last_modified) AS max_last_modified,
      now() AS ingested_at
    FROM (
      ${sources}
    )
    WHERE ${INVENTORY_ROW_FILTER}
      AND (empty('${key_prefix_to_strip}') OR startsWith(object_key, '${key_prefix_to_strip}'))
      AND (
        '${storage_class}' != 'home'
        OR match(physical_key, '^home/[^/]+/.+')
        OR match(physical_key, '^global/home/[^/]+/[^/]+/.+')
      )
    GROUP BY storage_class, team, prefix
    FORMAT TSV
  ")"
  run_client_query "ALTER TABLE storage_stats.storage_prefix_daily_local ON CLUSTER '{cluster}' DELETE WHERE day = toDate('${report_date}') AND cell = '${cell}' AND storage_class = '${storage_class}' SETTINGS mutations_sync = 2"
  if [[ -n ${rows} ]]; then
    printf '%s\n' "${rows}" | client --query "INSERT INTO storage_stats.storage_prefix_daily (day, cell, storage_class, team, prefix, object_count, total_bytes, max_last_modified, ingested_at) FORMAT TSV"
    echo "ingested ${cell} inventory rollup for ${report_date}"
  else
    echo "${cell}: completed inventory contains no objects"
  fi
}

append_source() {
  local cell="$1"
  local storage_class="$2"
  local source="$3"
  local source_key="${cell}|${storage_class}"
  case "${cell}" in
    *"'"* | *$'\n'*)
      echo "${cell}: cell name contains unsupported quoting" >&2
      exit 1
      ;;
    *) ;;
  esac
  if [[ -n ${cell_sources[${source_key}]:-} ]]; then
    cell_sources[${source_key}]+=$'\nUNION ALL\n'
  fi
  cell_sources[${source_key}]+="${source}"
}

declare -A cell_sources=()
declare -A cell_prefixes=()
declare -A cell_namespaces=()
if [[ -n ${INVENTORY_MANIFEST_SOURCES:-} ]]; then
  while IFS='|' read -r cell source_provider virtual_name storage_class key_prefix_to_strip logical_namespace manifest_url; do
    [[ -n ${cell} ]] || continue
    [[ -n ${manifest_url} ]] || {
      echo "${cell}: manifest URL is required" >&2
      exit 1
    }
    source_key="${cell}|${storage_class}"
    cell_prefixes[${source_key}]="${key_prefix_to_strip}"
    cell_namespaces[${source_key}]="${logical_namespace}"
    access_key="${STATS_GATEWAY_ACCESS_KEY_ID:-}"
    secret_key="${STATS_GATEWAY_SECRET_ACCESS_KEY:-}"
    if [[ ${source_provider} != "floci" ]]; then
      access_key="$(cat "${stats_secret_dir}/${virtual_name}-access-key-id")"
      secret_key="$(cat "${stats_secret_dir}/${virtual_name}-secret-access-key")"
    fi
    access_key_sql="$(sql_escape "${access_key}")"
    secret_key_sql="$(sql_escape "${secret_key}")"
    credentials="'${access_key_sql}', '${secret_key_sql}', "
    manifest_url="${manifest_url//\{date\}/${report_date}}"
    manifest_url="${manifest_url//\{year\}/${report_year}}"
    manifest_url="${manifest_url//\{month\}/${report_month}}"
    manifest_url="${manifest_url//\{day\}/${report_day}}"
    case "${INVENTORY_TABLE_FUNCTION}" in
      s3)
        case "${source_provider}" in
          aws | floci | gcp) ;;
          *)
            echo "${cell}: unsupported manifest provider ${source_provider}" >&2
            exit 1
            ;;
        esac
        manifest_name="${INVENTORY_MANIFEST_NAME}"
        checksum_name="${INVENTORY_MANIFEST_CHECKSUM_NAME}"
        manifest_file_key="${INVENTORY_MANIFEST_FILE_KEY}"
        manifest_filter="${INVENTORY_MANIFEST_FILTER}"
        if [[ ${source_provider} == "gcp" ]]; then
          manifest_name="${manifest_url##*/}"
          manifest_file_key=""
          manifest_filter="1"
        fi
        manifest_array_field="files"
        [[ ${source_provider} == "gcp" ]] && manifest_array_field="report_shards_file_names"
        manifest_source="s3('${manifest_url}', ${credentials}'RawBLOB', 'json String')"
        manifest_checksum_source="s3('${manifest_url%"${manifest_name}"}${checksum_name}', ${credentials}'LineAsString', 'checksum String')"
        manifest_authority="${manifest_url#*://}"
        manifest_scheme="${manifest_url%%://*}"
        manifest_authority="${manifest_authority%%/*}"
        shard_url_prefix="${manifest_scheme}://${manifest_authority}"
        ;;
      *)
        echo "${cell}: manifest ingestion is unsupported for ${INVENTORY_TABLE_FUNCTION}" >&2
        exit 1
        ;;
    esac
    if [[ ${source_provider} == "gcp" ]]; then
      latest_manifest_cte="
        WITH latest AS (
          SELECT _path, json
          FROM ${manifest_source}
          ORDER BY _path DESC
          LIMIT 1
        )
      "
    else
      latest_manifest_cte="
        WITH manifests AS (
          SELECT _path, json
          FROM ${manifest_source}
        ),
        checksums AS (
          SELECT _path, checksum
          FROM ${manifest_checksum_source}
        ),
        latest AS (
          SELECT m._path, m.json, c.checksum
          FROM manifests AS m
          INNER JOIN checksums AS c
            ON replaceRegexpOne(m._path, '/[^/]+$', '/') =
               replaceRegexpOne(c._path, '/[^/]+$', '/')
          WHERE lower(trim(c.checksum)) = lower(hex(MD5(m.json)))
          ORDER BY m._path DESC
          LIMIT 1
        )
      "
    fi
    manifest_stats="$(run_local_query "
      ${latest_manifest_cte}
      SELECT count(), coalesce(sum(length(JSONExtractArrayRaw(json, '${manifest_array_field}'))), 0)
      FROM latest
      FORMAT TSVRaw
    ")"
    manifest_count=0
    manifest_file_count=0
    read -r manifest_count manifest_file_count <<<"${manifest_stats}"
    if [[ ${manifest_count:-0} -eq 0 ]]; then
      echo "${cell}: no completed manifest with a valid completion signal" >&2
      exit 1
    fi
    if [[ ${manifest_file_count:-0} -eq 0 ]]; then
      echo "${cell}: completed manifest contains no objects"
      append_source "${cell}" "${storage_class}" "SELECT '' AS object_key, toInt64(0) AS object_size, toDateTime64(0, 3, 'UTC') AS object_last_modified WHERE 0"
      continue
    fi
    if [[ ${source_provider} == "gcp" ]]; then
      manifest_keys="$(run_local_query "
        ${latest_manifest_cte}
        SELECT JSONExtract(file, 'String')
        FROM latest
        ARRAY JOIN JSONExtractArrayRaw(json, 'report_shards_file_names') AS file
        WHERE ${manifest_filter}
        FORMAT TSVRaw
      ")"
    else
      manifest_keys="$(run_local_query "
        ${latest_manifest_cte}
        SELECT JSONExtractString(file, '${manifest_file_key}')
        FROM latest
        ARRAY JOIN JSONExtractArrayRaw(json, 'files') AS file
        WHERE ${manifest_filter}
        FORMAT TSVRaw
      ")"
    fi
    [[ -n ${manifest_keys} ]] || {
      echo "${cell}: completed manifest has no Parquet shards" >&2
      exit 1
    }
    while IFS= read -r shard_path; do
      [[ -n ${shard_path} ]] || continue
      case "${shard_path}" in
        *"'"* | *$'\n'*)
          echo "${cell}: manifest shard path contains unsupported quoting" >&2
          exit 1
          ;;
        *) ;;
      esac
      case "${source_provider}" in
        aws | floci) source="SELECT key AS object_key, size AS object_size, last_modified_date AS object_last_modified FROM s3('${shard_url_prefix}/${shard_path}', ${credentials}'Parquet', 'key String, size Int64, last_modified_date DateTime64(3)')" ;;
        gcp) source="SELECT name AS object_key, size AS object_size, parseDateTime64BestEffort(updated, 3, 'UTC') AS object_last_modified FROM s3('${shard_url_prefix}/${shard_path}', ${credentials}'Parquet', 'name String, size Int64, updated String')" ;;
        *)
          echo "${cell}: unsupported shard provider ${source_provider}" >&2
          exit 1
          ;;
      esac
      append_source "${cell}" "${storage_class}" "${source}"
    done <<<"${manifest_keys}"
  done <<<"${INVENTORY_MANIFEST_SOURCES}"
fi
if [[ -n ${INVENTORY_SOURCES:-} ]]; then
  while IFS='|' read -r cell source_provider virtual_name storage_class key_prefix_to_strip logical_namespace url_template; do
    [[ -n ${cell} ]] || continue
    credentials=""
    [[ -n ${url_template} ]] || {
      echo "${cell}: inventory URL template is required" >&2
      exit 1
    }
    access_key="$(cat "${stats_secret_dir}/${virtual_name}-access-key-id")"
    secret_key="$(cat "${stats_secret_dir}/${virtual_name}-secret-access-key")"
    access_key_sql="$(sql_escape "${access_key}")"
    secret_key_sql="$(sql_escape "${secret_key}")"
    credentials="'${access_key_sql}', '${secret_key_sql}', "
    url="${url_template//\{date\}/${report_date}}"
    url="${url//\{year\}/${report_year}}"
    url="${url//\{month\}/${report_month}}"
    url="${url//\{day\}/${report_day}}"
    case "${source_provider}" in
      floci) source="SELECT key AS object_key, size AS object_size, last_modified_date AS object_last_modified FROM s3('${url}', ${credentials}'Parquet', 'key String, size Int64, last_modified_date DateTime64(3)')" ;;
      aws) source="SELECT key AS object_key, size AS object_size, last_modified_date AS object_last_modified FROM s3('${url}', ${credentials}'Parquet', 'key String, size Int64, last_modified_date DateTime64(3)')" ;;
      gcp) source="SELECT name AS object_key, size AS object_size, parseDateTime64BestEffort(updated, 3, 'UTC') AS object_last_modified FROM s3('${url}', ${credentials}'Parquet', 'name String, size Int64, updated String')" ;;
      *)
        echo "${cell}: unsupported source provider ${source_provider}" >&2
        exit 1
        ;;
    esac
    source_key="${cell}|${storage_class}"
    cell_prefixes[${source_key}]="${key_prefix_to_strip}"
    cell_namespaces[${source_key}]="${logical_namespace}"
    append_source "${cell}" "${storage_class}" "${source}"
  done <<<"${INVENTORY_SOURCES}"
fi
for source_key in "${!cell_sources[@]}"; do
  IFS='|' read -r cell storage_class <<<"${source_key}"
  ingest_sources "${cell}" "${storage_class}" "${cell_prefixes[${source_key}]}" "${cell_namespaces[${source_key}]}" "${cell_sources[${source_key}]}"
done
