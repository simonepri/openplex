#!/usr/bin/env bash
# Rebuilds the storage_stats serving cache from durable manifests or raw inventories.

# shellcheck disable=SC2310
set -euo pipefail

: "${CLICKHOUSE_HOST:?CLICKHOUSE_HOST is required}"
: "${CLICKHOUSE_PORT:?CLICKHOUSE_PORT is required}"
: "${CLICKHOUSE_USERNAME:?CLICKHOUSE_USERNAME is required}"
: "${INVENTORY_DATE_EXPR:=yesterday}"
: "${INVENTORY_ENABLED:=false}"
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
manifest_dates=()
for i in 0 1 2; do
  manifest_dates+=("$(date -u -d "${INVENTORY_DATE_EXPR} - ${i} day" +%F)")
done
manifest_date_pattern="{$(
  IFS=,
  echo "${manifest_dates[*]}"
)}"
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

if [[ ${INVENTORY_ENABLED} != "true" ]]; then
  echo "schema ensured; inventory ingestion disabled"
  exit 0
fi

ingest_sources() {
  local cell="$1"
  local storage_class="$2"
  local key_prefix_to_strip="$3"
  local logical_namespace="$4"
  local sources="$5"
  local effective_date="${6:-${report_date}}"
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
      toDate('${effective_date}') AS day,
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
      AND (
        '${storage_class}' != 'meta'
        OR (NOT startsWith(physical_key, 'inventory/') AND NOT startsWith(physical_key, 'meta/inventory/'))
      )
    GROUP BY storage_class, team, prefix
    FORMAT TSV
  ")"
  run_client_query "ALTER TABLE storage_stats.storage_prefix_daily_local ON CLUSTER '{cluster}' DELETE WHERE day = toDate('${effective_date}') AND cell = '${cell}' AND storage_class = '${storage_class}' SETTINGS mutations_sync = 2"
  if [[ -n ${rows} ]]; then
    printf '%s\n' "${rows}" | client --query "INSERT INTO storage_stats.storage_prefix_daily (day, cell, storage_class, team, prefix, object_count, total_bytes, max_last_modified, ingested_at) FORMAT TSV"
    echo "ingested ${cell} inventory rollup for ${effective_date}"
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
declare -A cell_dates=()
if [[ -n ${INVENTORY_MANIFEST_SOURCES:-} ]]; then
  while IFS='|' read -r cell source_provider _virtual_name storage_class key_prefix_to_strip logical_namespace manifest_url; do
    [[ -n ${cell} ]] || continue
    [[ -n ${manifest_url} ]] || {
      echo "${cell}: manifest URL is required" >&2
      exit 1
    }
    if [[ -z ${source_provider} ]]; then
      case "${cell}" in
        cell-aws* | *-aws-*) source_provider="aws" ;;
        cell-eaws* | *-floci-* | local-*) source_provider="floci" ;;
        cell-gcp* | *-gcp-*) source_provider="gcp" ;;
        *) source_provider="floci" ;;
      esac
    fi
    source_key="${cell}|${storage_class}"
    cell_prefixes[${source_key}]="${key_prefix_to_strip}"
    cell_namespaces[${source_key}]="${logical_namespace}"
    access_key="$(cat "${stats_secret_dir}/access-key-id")"
    secret_key="$(cat "${stats_secret_dir}/secret-access-key")"
    access_key_sql="$(sql_escape "${access_key}")"
    secret_key_sql="$(sql_escape "${secret_key}")"
    credentials="'${access_key_sql}', '${secret_key_sql}', "
    manifest_url="${manifest_url//\{date\}T\*/${manifest_date_pattern}T*}"
    manifest_url="${manifest_url//\{date\}\/${INVENTORY_MANIFEST_NAME}/${manifest_date_pattern}T*\/${INVENTORY_MANIFEST_NAME}}"
    manifest_url="${manifest_url//\{date\}\/manifest.json/${manifest_date_pattern}T*\/manifest.json}"
    manifest_url="${manifest_url//\{date\}/${manifest_date_pattern}}"
    manifest_url="${manifest_url//\{year\}/*}"
    manifest_url="${manifest_url//\{month\}/*}"
    manifest_url="${manifest_url//\{day\}/*}"
    manifest_url="${manifest_url//\/${report_date}\/${INVENTORY_MANIFEST_NAME}/\/${manifest_date_pattern}T*\/${INVENTORY_MANIFEST_NAME}}"
    manifest_url="${manifest_url//\/${report_date}\/manifest.json/\/${manifest_date_pattern}T*\/manifest.json}"
    case "${INVENTORY_TABLE_FUNCTION}" in
      s3)
        case "${source_provider}" in
          aws | floci) ;;
          gcp)
            echo "${cell}: GCS Storage Insights manifest ingestion is not yet supported in this rollup script" >&2
            exit 1
            ;;
          *)
            echo "${cell}: unsupported manifest provider ${source_provider}" >&2
            exit 1
            ;;
        esac
        manifest_name="${INVENTORY_MANIFEST_NAME}"
        checksum_name="${INVENTORY_MANIFEST_CHECKSUM_NAME}"
        manifest_file_key="${INVENTORY_MANIFEST_FILE_KEY}"
        manifest_filter="${INVENTORY_MANIFEST_FILTER}"
        manifest_source="s3('${manifest_url}', ${credentials}'RawBLOB', 'json String')"
        manifest_checksum_source="s3('${manifest_url%"${manifest_name}"}${checksum_name}', ${credentials}'LineAsString', 'checksum String')"
        url_without_scheme="${manifest_url#*://}"
        manifest_scheme="${manifest_url%%://*}"
        manifest_authority="${url_without_scheme%%/*}"
        path_after_authority="${url_without_scheme#*/}"
        destination_bucket="${path_after_authority%%/*}"
        ;;
      *)
        echo "${cell}: manifest ingestion is unsupported for ${INVENTORY_TABLE_FUNCTION}" >&2
        exit 1
        ;;
    esac
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
    manifest_stats="$(run_local_query "
      ${latest_manifest_cte}
      SELECT
        count(),
        coalesce(sum(length(JSONExtractArrayRaw(json, 'files'))), 0),
        coalesce(extract(any(m._path), '([0-9]{4}-[0-9]{2}-[0-9]{2})'), '${report_date}')
      FROM latest
      FORMAT TSVRaw
    ")"
    manifest_count=0
    manifest_file_count=0
    manifest_day="${report_date}"
    read -r manifest_count manifest_file_count manifest_day <<<"${manifest_stats}"
    [[ -n ${manifest_day} ]] || manifest_day="${report_date}"
    cell_dates[${source_key}]="${manifest_day}"
    if [[ ${manifest_count:-0} -eq 0 ]]; then
      echo "${cell}: no completed manifest with a valid completion signal" >&2
      exit 1
    fi
    if [[ ${manifest_file_count:-0} -eq 0 ]]; then
      echo "${cell}: completed manifest contains no objects"
      append_source "${cell}" "${storage_class}" "SELECT '' AS object_key, toInt64(0) AS object_size, toDateTime64(0, 3, 'UTC') AS object_last_modified WHERE 0"
      continue
    fi
    manifest_keys="$(run_local_query "
      ${latest_manifest_cte}
      SELECT JSONExtractString(file, '${manifest_file_key}')
      FROM latest
      ARRAY JOIN JSONExtractArrayRaw(json, 'files') AS file
      WHERE ${manifest_filter}
      FORMAT TSVRaw
    ")"
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
      clean_shard_path="${shard_path#/}"
      if [[ ${clean_shard_path} == "${destination_bucket}/"* ]]; then
        full_shard_path="${clean_shard_path}"
      else
        full_shard_path="${destination_bucket}/${clean_shard_path}"
      fi
      shard_url="${manifest_scheme}://${manifest_authority}/${full_shard_path}"
      case "${source_provider}" in
        aws | floci) source="SELECT key AS object_key, size AS object_size, last_modified_date AS object_last_modified FROM s3('${shard_url}', ${credentials}'Parquet', 'key String, size Int64, last_modified_date DateTime64(3)')" ;;
        gcp) source="SELECT name AS object_key, size AS object_size, parseDateTime64BestEffort(updated, 3, 'UTC') AS object_last_modified FROM s3('${shard_url}', ${credentials}'Parquet', 'name String, size Int64, updated String')" ;;
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
  while IFS='|' read -r cell source_provider _virtual_name storage_class key_prefix_to_strip logical_namespace url_template; do
    [[ -n ${cell} ]] || continue
    [[ -n ${url_template} ]] || {
      echo "${cell}: inventory URL template is required" >&2
      exit 1
    }
    if [[ -z ${source_provider} ]]; then
      case "${cell}" in
        cell-aws* | *-aws-*) source_provider="aws" ;;
        cell-eaws* | *-floci-* | local-*) source_provider="floci" ;;
        cell-gcp* | *-gcp-*) source_provider="gcp" ;;
        *) source_provider="floci" ;;
      esac
    fi
    access_key="$(cat "${stats_secret_dir}/access-key-id")"
    secret_key="$(cat "${stats_secret_dir}/secret-access-key")"
    access_key_sql="$(sql_escape "${access_key}")"
    secret_key_sql="$(sql_escape "${secret_key}")"
    credentials="'${access_key_sql}', '${secret_key_sql}', "
    url="${url_template//\{date\}/${report_date}}"
    url="${url//\{year\}/${report_year}}"
    url="${url//\{month\}/${report_month}}"
    url="${url//\{day\}/${report_day}}"
    case "${source_provider}" in
      aws | floci) source="SELECT key AS object_key, size AS object_size, last_modified_date AS object_last_modified FROM s3('${url}', ${credentials}'Parquet', 'key String, size Int64, last_modified_date DateTime64(3)')" ;;
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
  ingest_sources "${cell}" "${storage_class}" "${cell_prefixes[${source_key}]}" "${cell_namespaces[${source_key}]}" "${cell_sources[${source_key}]}" "${cell_dates[${source_key}]:-${report_date}}"
done
