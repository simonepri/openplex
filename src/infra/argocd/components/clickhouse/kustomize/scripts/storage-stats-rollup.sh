#!/usr/bin/env bash
# Rebuilds the storage_stats serving cache from durable manifests or raw inventories.

# shellcheck disable=SC2310
set -euo pipefail

: "${CLICKHOUSE_HOST:?CLICKHOUSE_HOST is required}"
: "${CLICKHOUSE_PORT:?CLICKHOUSE_PORT is required}"
: "${CLICKHOUSE_USERNAME:?CLICKHOUSE_USERNAME is required}"
: "${INVENTORY_DATE_EXPR:=yesterday}"
: "${INVENTORY_ENABLED:=false}"
: "${INVENTORY_MAX_PREFIX_DEPTH:=2048}"
: "${INVENTORY_MAX_THREADS:=2}"
: "${INVENTORY_COMPACTION_PARTITIONS:=64}"
: "${INVENTORY_ROW_FILTER:=1}"
: "${INVENTORY_MANIFEST_NAME:=manifest.json}"
: "${INVENTORY_MANIFEST_CHECKSUM_NAME:=manifest.checksum}"
: "${INVENTORY_MANIFEST_FILE_KEY:=file}"
: "${INVENTORY_MANIFEST_FILTER:=1}"

if ! [[ ${INVENTORY_MAX_PREFIX_DEPTH} =~ ^[0-9]+$ ]] || ((INVENTORY_MAX_PREFIX_DEPTH < 1 || INVENTORY_MAX_PREFIX_DEPTH > 2048)); then
  echo "INVENTORY_MAX_PREFIX_DEPTH must be between 1 and 2048" >&2
  exit 1
fi
if ! [[ ${INVENTORY_MAX_THREADS} =~ ^[0-9]+$ ]] || ((INVENTORY_MAX_THREADS < 1 || INVENTORY_MAX_THREADS > 64)); then
  echo "INVENTORY_MAX_THREADS must be between 1 and 64" >&2
  exit 1
fi
if ! [[ ${INVENTORY_COMPACTION_PARTITIONS} =~ ^[0-9]+$ ]] || ((INVENTORY_COMPACTION_PARTITIONS < 1 || INVENTORY_COMPACTION_PARTITIONS > 256)); then
  echo "INVENTORY_COMPACTION_PARTITIONS must be between 1 and 256" >&2
  exit 1
fi

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

local_config="${tmp_dir}/local-config.xml"
clickhouse_tmp_path="${tmp_dir}/clickhouse-tmp"
mkdir -p "${clickhouse_tmp_path}"
cat >"${local_config}" <<XML
<clickhouse>
  <tmp_path>${clickhouse_tmp_path}/</tmp_path>
  <profiles>
    <default>
      <max_threads>${INVENTORY_MAX_THREADS}</max_threads>
      <input_format_parquet_max_block_size>2048</input_format_parquet_max_block_size>
      <input_format_parquet_memory_high_watermark>536870912</input_format_parquet_memory_high_watermark>
      <max_bytes_before_external_group_by>1073741824</max_bytes_before_external_group_by>
    </default>
  </profiles>
  <s3>
    <use_environment_credentials>true</use_environment_credentials>
  </s3>
</clickhouse>
XML

run_local_query() {
  local query="$1"
  local query_file
  local status
  query_file="$(mktemp "${tmp_dir}/local-query.XXXXXX")"
  chmod 600 "${query_file}"
  printf '%s\n' "${query}" >"${query_file}"
  rm -rf "${clickhouse_tmp_path}"
  mkdir -p "${clickhouse_tmp_path}"
  if clickhouse-local --config-file "${local_config}" --queries-file "${query_file}"; then
    status=0
  else
    status=$?
  fi
  rm -rf "${clickhouse_tmp_path}"
  mkdir -p "${clickhouse_tmp_path}"
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

# Prints the leading s3() credential arguments for a source: the gateway key
# pair by default, nothing when the source reads AWS S3 with the pod's
# environment credentials (EKS Pod Identity).
source_credentials() {
  local credential_source="$1"
  local cell="$2"
  local access_key secret_key access_key_sql secret_key_sql
  case "${credential_source}" in
    environment) ;;
    "")
      access_key="$(cat "${stats_secret_dir}/access-key-id")"
      secret_key="$(cat "${stats_secret_dir}/secret-access-key")"
      access_key_sql="$(sql_escape "${access_key}")"
      secret_key_sql="$(sql_escape "${secret_key}")"
      printf "'%s', '%s', " "${access_key_sql}" "${secret_key_sql}"
      ;;
    *)
      echo "${cell}: unsupported credential source ${credential_source}" >&2
      return 1
      ;;
  esac
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
  local metadata_sources="$7"
  local source_tmp_dir direct_partitions_dir ancestor_partitions_dir batch_rows_file source batch_count
  source_tmp_dir="$(mktemp -d "${tmp_dir}/source.XXXXXX")"
  direct_partitions_dir="${source_tmp_dir}/direct-partitions"
  ancestor_partitions_dir="${source_tmp_dir}/ancestor-partitions"
  mkdir -p "${direct_partitions_dir}" "${ancestor_partitions_dir}"
  chmod 700 "${direct_partitions_dir}" "${ancestor_partitions_dir}"
  local -A direct_partition_compacted_size=()
  local -A ancestor_partition_compacted_size=()
  local source_count batch_total batch_number processed_object_count batch_object_count
  local -a batch_object_counts=()
  source_count="$(awk 'NF { count++ } END { print count + 0 }' <<<"${sources}")"
  batch_total=$(((source_count + 3) / 4))
  batch_number=0
  processed_object_count=0

  count_metadata_batch() {
    local queries="$1"
    run_local_query "SELECT sum(num_rows) FROM (${queries}) FORMAT TSVRaw"
  }

  local metadata_batch_sources="" metadata_source metadata_batch_count=0 metadata_batch_number=0 total_object_count=0
  while IFS= read -r metadata_source; do
    [[ -n ${metadata_source} ]] || continue
    if [[ -n ${metadata_batch_sources} ]]; then
      metadata_batch_sources+=$'\nUNION ALL\n'
    fi
    metadata_batch_sources+="${metadata_source}"
    metadata_batch_count=$((metadata_batch_count + 1))
    if ((metadata_batch_count == 4)); then
      metadata_batch_number=$((metadata_batch_number + 1))
      echo "progress phase=count_metadata source=${cell}/${storage_class} batch=${metadata_batch_number}/${batch_total}" >&2
      batch_object_count="$(count_metadata_batch "${metadata_batch_sources}")"
      batch_object_counts[metadata_batch_number]="${batch_object_count}"
      total_object_count=$((total_object_count + batch_object_count))
      metadata_batch_sources=""
      metadata_batch_count=0
    fi
  done <<<"${metadata_sources}"
  if ((metadata_batch_count > 0)); then
    metadata_batch_number=$((metadata_batch_number + 1))
    echo "progress phase=count_metadata source=${cell}/${storage_class} batch=${metadata_batch_number}/${batch_total}" >&2
    batch_object_count="$(count_metadata_batch "${metadata_batch_sources}")"
    batch_object_counts[metadata_batch_number]="${batch_object_count}"
    total_object_count=$((total_object_count + batch_object_count))
  fi
  echo "progress phase=aggregate source=${cell}/${storage_class} shards=${source_count} batches=${batch_total} objects=${total_object_count}" >&2

  log_processed_progress() {
    local completed_batch="$1"
    local progress_percent=100
    if ((total_object_count > 0)); then
      progress_percent=$((processed_object_count * 100 / total_object_count))
    fi
    echo "progress phase=aggregate_complete source=${cell}/${storage_class} batch=${completed_batch}/${batch_total} objects=${processed_object_count}/${total_object_count} percent=${progress_percent}" >&2
  }

  partition_rows() {
    local input_file="$1"
    local output_dir="$2"
    awk -F '\t' -v output_dir="${output_dir}" -v partition_count="${INVENTORY_COMPACTION_PARTITIONS}" '
      {
        if ($1 !~ /^[0-9]+$/ || $1 >= partition_count) {
          exit 1
        }
        partition = $1 + 0
        row = $0
        sub(/^[^\t]*\t/, "", row)
        path = sprintf("%s/%03d.tsv", output_dir, partition)
        print row >> path
        open_paths[path] = 1
      }
      END {
        for (path in open_paths) close(path)
      }
    ' "${input_file}"
  }

  compact_direct_partition() {
    local input_file="$1"
    local output_file="$2"
    [[ -s ${input_file} ]] || return 0
    run_local_query "
      SELECT
        day,
        cell,
        storage_class,
        team,
        root_prefix,
        root_depth,
        folder_prefix,
        folder_depth,
        sum(object_count) AS object_count,
        sum(total_bytes) AS total_bytes,
        max(max_last_modified) AS max_last_modified,
        now() AS ingested_at
      FROM file('${input_file}', 'TSV', 'day Date, cell String, storage_class String, team String, root_prefix String, root_depth UInt16, folder_prefix String, folder_depth UInt16, object_count UInt64, total_bytes UInt64, max_last_modified DateTime64(3), ingested_at DateTime')
      GROUP BY day, cell, storage_class, team, root_prefix, root_depth, folder_prefix, folder_depth
      SETTINGS max_threads = 1, max_block_size = 2048, max_bytes_before_external_group_by = 268435456
      FORMAT TSV
    " >"${output_file}"
  }

  compact_direct_partitions() {
    local compact_all="${1:-false}"
    local partition input_file output_file size threshold
    for ((partition = 0; partition < INVENTORY_COMPACTION_PARTITIONS; partition++)); do
      input_file="${direct_partitions_dir}/$(printf '%03d' "${partition}").tsv"
      [[ -s ${input_file} ]] || continue
      size="$(stat -c '%s' "${input_file}")"
      threshold=1073741824
      if [[ -n ${direct_partition_compacted_size[${partition}]:-} ]]; then
        threshold=$((direct_partition_compacted_size[${partition}] + 1073741824))
      fi
      if [[ ${compact_all} != true ]] && ((size < threshold)); then
        continue
      fi
      echo "progress phase=compact_direct source=${cell}/${storage_class} partition=$((partition + 1))/${INVENTORY_COMPACTION_PARTITIONS}" >&2
      output_file="$(mktemp "${source_tmp_dir}/direct-compacted.XXXXXX")"
      chmod 600 "${output_file}"
      compact_direct_partition "${input_file}" "${output_file}"
      mv "${output_file}" "${input_file}"
      direct_partition_compacted_size[${partition}]="$(stat -c '%s' "${input_file}")"
    done
  }

  aggregate_batch() {
    local output_file="$1"
    echo "progress phase=aggregate_start source=${cell}/${storage_class} batch=${batch_number}/${batch_total} shards=${batch_count}" >&2
    run_local_query "
    WITH if(
      empty('${key_prefix_to_strip}'),
      object_key,
      substring(object_key, length('${key_prefix_to_strip}') + 1)
    ) AS physical_key,
    multiIf(
      startsWith(physical_key, concat('global/', '${storage_class}', '/')),
      substring(physical_key, length(concat('global/', '${storage_class}', '/')) + 1),
      startsWith(physical_key, concat('${storage_class}', '/')),
      substring(physical_key, length(concat('${storage_class}', '/')) + 1),
      physical_key
    ) AS relative_key,
    trimRight(relative_key, '/') AS normalized_relative_key,
    if(empty(normalized_relative_key), [], splitByChar('/', normalized_relative_key)) AS rel_segments,
    concat(
      's3/',
      replaceRegexpOne('${cell}', '^(ctrl-|cell-)', ''),
      '/',
      if(
        empty(source_virtual_prefix),
        if('${logical_namespace}' = 'global', concat('global/', '${storage_class}'), '${storage_class}'),
        source_virtual_prefix
      )
    ) AS root_prefix,
    length(splitByChar('/', root_prefix)) - 2 AS root_path_depth,
    if(
      empty(normalized_relative_key),
      0,
      if(endsWith(relative_key, '/'), length(rel_segments), length(rel_segments) - 1)
    ) AS relative_directory_depth,
    least(
      toUInt32(${INVENTORY_MAX_PREFIX_DEPTH}),
      toUInt32(root_path_depth + relative_directory_depth)
    ) AS max_directory_depth,
    concat(
      root_prefix,
      if(empty(normalized_relative_key), '', concat('/', normalized_relative_key))
    ) AS full_virtual_path,
    splitByChar('/', full_virtual_path) AS full_virtual_segments
    SELECT
      modulo(cityHash64(root_prefix, folder_prefix), ${INVENTORY_COMPACTION_PARTITIONS}) AS partition_id,
      toDate('${effective_date}') AS day,
      '${cell}' AS cell,
      '${storage_class}' AS storage_class,
      if(
        NOT empty(source_virtual_prefix),
        '',
        if('${logical_namespace}' = 'global', 'global', arrayElement(rel_segments, 1))
      ) AS team,
      root_prefix,
      toUInt16(root_path_depth) AS root_depth,
      arrayStringConcat(arraySlice(full_virtual_segments, 1, max_directory_depth + 2), '/') AS folder_prefix,
      toUInt16(max_directory_depth) AS folder_depth,
      count() AS object_count,
      sum(object_size) AS total_bytes,
      max(object_last_modified) AS max_last_modified,
      now() AS ingested_at
    FROM (
      ${batch_sources}
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
    GROUP BY partition_id, storage_class, team, root_prefix, root_path_depth, folder_depth, folder_prefix
    SETTINGS max_threads = ${INVENTORY_MAX_THREADS}, input_format_parquet_max_block_size = 2048, input_format_parquet_memory_high_watermark = 536870912, max_bytes_before_external_group_by = 536870912
    FORMAT TSV
    " >"${output_file}"
  }

  rollup_direct_folders() {
    local input_file="$1"
    local output_file="$2"
    run_local_query "
      SELECT
        modulo(cityHash64(prefix), ${INVENTORY_COMPACTION_PARTITIONS}) AS partition_id,
        day,
        cell,
        storage_class,
        team,
        prefix,
        prefix_depth,
        sum(object_count) AS object_count,
        sum(total_bytes) AS total_bytes,
        max(max_last_modified) AS max_last_modified,
        now() AS ingested_at
      FROM (
        SELECT
          day,
          cell,
          storage_class,
          team,
          arrayStringConcat(arraySlice(splitByChar('/', folder_prefix), 1, prefix_depth + 2), '/') AS prefix,
          toUInt16(prefix_depth) AS prefix_depth,
          object_count,
          total_bytes,
          max_last_modified
        FROM file('${input_file}', 'TSV', 'day Date, cell String, storage_class String, team String, root_prefix String, root_depth UInt16, folder_prefix String, folder_depth UInt16, object_count UInt64, total_bytes UInt64, max_last_modified DateTime64(3), ingested_at DateTime')
        ARRAY JOIN range(toUInt64(root_depth), toUInt64(folder_depth) + 1) AS prefix_depth
      )
      GROUP BY partition_id, day, cell, storage_class, team, prefix_depth, prefix
      SETTINGS max_threads = ${INVENTORY_MAX_THREADS}, max_block_size = 2048, max_bytes_before_external_group_by = 536870912
      FORMAT TSV
    " >"${output_file}"
  }

  compact_ancestor_partition() {
    local input_file="$1"
    local output_file="$2"
    [[ -s ${input_file} ]] || return 0
    run_local_query "
      SELECT
        day,
        cell,
        storage_class,
        team,
        prefix,
        prefix_depth,
        sum(object_count) AS object_count,
        sum(total_bytes) AS total_bytes,
        max(max_last_modified) AS max_last_modified,
        now() AS ingested_at
      FROM file('${input_file}', 'TSV', 'day Date, cell String, storage_class String, team String, prefix String, prefix_depth UInt16, object_count UInt64, total_bytes UInt64, max_last_modified DateTime64(3), ingested_at DateTime')
      GROUP BY day, cell, storage_class, team, prefix, prefix_depth
      SETTINGS max_threads = 1, max_block_size = 2048, max_bytes_before_external_group_by = 268435456
      FORMAT TSV
    " >"${output_file}"
  }

  compact_ancestor_partitions() {
    local compact_all="${1:-false}"
    local partition input_file output_file size threshold
    for ((partition = 0; partition < INVENTORY_COMPACTION_PARTITIONS; partition++)); do
      input_file="${ancestor_partitions_dir}/$(printf '%03d' "${partition}").tsv"
      [[ -s ${input_file} ]] || continue
      size="$(stat -c '%s' "${input_file}")"
      threshold=1073741824
      if [[ -n ${ancestor_partition_compacted_size[${partition}]:-} ]]; then
        threshold=$((ancestor_partition_compacted_size[${partition}] + 1073741824))
      fi
      if [[ ${compact_all} != true ]] && ((size < threshold)); then
        continue
      fi
      echo "progress phase=compact_ancestors source=${cell}/${storage_class} partition=$((partition + 1))/${INVENTORY_COMPACTION_PARTITIONS}" >&2
      output_file="$(mktemp "${source_tmp_dir}/ancestor-compacted.XXXXXX")"
      chmod 600 "${output_file}"
      compact_ancestor_partition "${input_file}" "${output_file}"
      mv "${output_file}" "${input_file}"
      ancestor_partition_compacted_size[${partition}]="$(stat -c '%s' "${input_file}")"
    done
  }

  batch_sources=""
  batch_count=0
  batch_rows_file="$(mktemp "${source_tmp_dir}/rollup-batch.XXXXXX")"
  chmod 600 "${batch_rows_file}"
  while IFS= read -r source; do
    [[ -n ${source} ]] || continue
    if [[ -n ${batch_sources} ]]; then
      batch_sources+=$'\nUNION ALL\n'
    fi
    batch_sources+="${source}"
    batch_count=$((batch_count + 1))
    if ((batch_count == 4)); then
      batch_number=$((batch_number + 1))
      aggregate_batch "${batch_rows_file}"
      partition_rows "${batch_rows_file}" "${direct_partitions_dir}"
      processed_object_count=$((processed_object_count + batch_object_counts[batch_number]))
      log_processed_progress "${batch_number}"
      : >"${batch_rows_file}"
      batch_sources=""
      batch_count=0
      compact_direct_partitions
    fi
  done <<<"${sources}"
  if ((batch_count > 0)); then
    batch_number=$((batch_number + 1))
    aggregate_batch "${batch_rows_file}"
    partition_rows "${batch_rows_file}" "${direct_partitions_dir}"
    processed_object_count=$((processed_object_count + batch_object_counts[batch_number]))
    log_processed_progress "${batch_number}"
  fi

  if compgen -G "${direct_partitions_dir}/*.tsv" >/dev/null; then
    compact_direct_partitions true
    local partition direct_file ancestor_batch_file
    ancestor_batch_file="$(mktemp "${source_tmp_dir}/ancestor-batch.XXXXXX")"
    chmod 600 "${ancestor_batch_file}"
    for ((partition = 0; partition < INVENTORY_COMPACTION_PARTITIONS; partition++)); do
      direct_file="${direct_partitions_dir}/$(printf '%03d' "${partition}").tsv"
      [[ -s ${direct_file} ]] || continue
      echo "progress phase=expand_ancestors source=${cell}/${storage_class} partition=$((partition + 1))/${INVENTORY_COMPACTION_PARTITIONS}" >&2
      rollup_direct_folders "${direct_file}" "${ancestor_batch_file}"
      partition_rows "${ancestor_batch_file}" "${ancestor_partitions_dir}"
      : >"${ancestor_batch_file}"
      rm -f "${direct_file}"
      compact_ancestor_partitions
    done
    compact_ancestor_partitions true
    run_client_query "ALTER TABLE storage_stats.storage_prefix_daily_local ON CLUSTER '{cluster}' DELETE WHERE day = toDate('${effective_date}') AND cell = '${cell}' AND storage_class = '${storage_class}' SETTINGS mutations_sync = 2"
    for ((partition = 0; partition < INVENTORY_COMPACTION_PARTITIONS; partition++)); do
      direct_file="${ancestor_partitions_dir}/$(printf '%03d' "${partition}").tsv"
      [[ -s ${direct_file} ]] || continue
      echo "progress phase=insert source=${cell}/${storage_class} partition=$((partition + 1))/${INVENTORY_COMPACTION_PARTITIONS}" >&2
      client --query "INSERT INTO storage_stats.storage_prefix_daily (day, cell, storage_class, team, prefix, prefix_depth, object_count, total_bytes, max_last_modified, ingested_at) FORMAT TSV" <"${direct_file}"
    done
    echo "ingested ${cell} inventory rollup for ${effective_date}"
  else
    run_client_query "ALTER TABLE storage_stats.storage_prefix_daily_local ON CLUSTER '{cluster}' DELETE WHERE day = toDate('${effective_date}') AND cell = '${cell}' AND storage_class = '${storage_class}' SETTINGS mutations_sync = 2"
    echo "${cell}: completed inventory contains no objects"
  fi
  rm -rf "${source_tmp_dir}"
}

append_source() {
  local cell="$1"
  local storage_class="$2"
  local source="$3"
  local virtual_prefix="$4"
  local metadata_source="$5"
  local source_key="${cell}|${storage_class}"
  case "${cell}" in
    *"'"* | *$'\n'*)
      echo "${cell}: cell name contains unsupported quoting" >&2
      exit 1
      ;;
    *) ;;
  esac
  case "${virtual_prefix}" in
    *"'"* | *$'\n'* | *'|'*)
      echo "${cell}: virtual prefix contains unsupported characters" >&2
      exit 1
      ;;
    *) ;;
  esac
  source="SELECT *, '${virtual_prefix}' AS source_virtual_prefix FROM (${source})"
  if [[ -n ${cell_sources[${source_key}]:-} ]]; then
    cell_sources[${source_key}]+=$'\n'
  fi
  cell_sources[${source_key}]+="${source}"
  if [[ -n ${cell_source_metadata[${source_key}]:-} ]]; then
    cell_source_metadata[${source_key}]+=$'\n'
  fi
  cell_source_metadata[${source_key}]+="${metadata_source}"
}

declare -A cell_sources=()
declare -A cell_source_metadata=()
declare -A cell_prefixes=()
declare -A cell_namespaces=()
declare -A cell_dates=()
missing_manifests=()
if [[ -n ${INVENTORY_MANIFEST_SOURCES:-} ]]; then
  while IFS='|' read -r cell source_provider credential_source storage_class key_prefix_to_strip logical_namespace virtual_prefix manifest_url; do
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
    credentials="$(source_credentials "${credential_source}" "${cell}")"
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
          aws | floci | gcp) ;;
          *)
            echo "${cell}: unsupported manifest provider ${source_provider}" >&2
            exit 1
            ;;
        esac
        manifest_source="s3('${manifest_url}', ${credentials}'RawBLOB', 'json String')"
        url_without_scheme="${manifest_url#*://}"
        manifest_scheme="${manifest_url%%://*}"
        manifest_authority="${url_without_scheme%%/*}"
        path_after_authority="${url_without_scheme#*/}"
        destination_bucket="${path_after_authority%%/*}"
        case "${source_provider}" in
          aws | floci)
            manifest_name="${INVENTORY_MANIFEST_NAME}"
            checksum_name="${INVENTORY_MANIFEST_CHECKSUM_NAME}"
            manifest_file_array="files"
            manifest_checksum_source="s3('${manifest_url%"${manifest_name}"}${checksum_name}', ${credentials}'LineAsString', 'checksum String')"
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
            ;;
          gcp)
            manifest_file_array="report_shards_file_names"
            latest_manifest_cte="
              WITH manifests AS (
                SELECT _path, json
                FROM ${manifest_source}
                WHERE JSONHas(json, 'report_shards_file_names')
              ),
              latest AS (
                SELECT _path, json, '' AS checksum
                FROM manifests
                ORDER BY _path DESC
                LIMIT 1
              )
            "
            ;;
          *)
            echo "${cell}: unsupported manifest provider ${source_provider}" >&2
            exit 1
            ;;
        esac
        ;;
      *)
        echo "${cell}: manifest ingestion is unsupported for ${INVENTORY_TABLE_FUNCTION}" >&2
        exit 1
        ;;
    esac
    manifest_error_file="${tmp_dir}/manifest-stats.stderr"
    if manifest_stats="$(run_local_query "
      ${latest_manifest_cte}
      SELECT
        count(),
        coalesce(sum(length(JSONExtractArrayRaw(json, '${manifest_file_array}'))), 0),
        coalesce(extract(any(_path), '([0-9]{4}-[0-9]{2}-[0-9]{2})'), '${report_date}')
      FROM latest
      FORMAT TSVRaw
    " 2>"${manifest_error_file}")"; then
      :
    else
      if grep -Eqi 'NoSuchBucket|NoSuchKey|404 Not Found' "${manifest_error_file}"; then
        echo "${cell}/${storage_class}: inventory report destination is not available yet" >&2
        missing_manifests+=("${cell}/${storage_class}")
        continue
      fi
      cat "${manifest_error_file}" >&2
      exit 1
    fi
    manifest_count=0
    manifest_file_count=0
    manifest_day="${report_date}"
    read -r manifest_count manifest_file_count manifest_day <<<"${manifest_stats}"
    [[ -n ${manifest_day} ]] || manifest_day="${report_date}"
    cell_dates[${source_key}]="${manifest_day}"
    if [[ ${manifest_count:-0} -eq 0 ]]; then
      echo "${cell}/${storage_class}: no completed manifest with a valid completion signal" >&2
      missing_manifests+=("${cell}/${storage_class}")
      continue
    fi
    if [[ ${manifest_file_count:-0} -eq 0 ]]; then
      echo "${cell}: completed manifest contains no objects"
      append_source "${cell}" "${storage_class}" "SELECT '' AS object_key, toInt64(0) AS object_size, toDateTime64(0, 3, 'UTC') AS object_last_modified WHERE 0" "" "SELECT toUInt64(0) AS num_rows"
      continue
    fi
    case "${source_provider}" in
      aws | floci)
        manifest_keys="$(run_local_query "
          ${latest_manifest_cte}
          SELECT JSONExtractString(file, '${INVENTORY_MANIFEST_FILE_KEY}')
          FROM latest
          ARRAY JOIN JSONExtractArrayRaw(json, '${manifest_file_array}') AS file
          WHERE ${INVENTORY_MANIFEST_FILTER}
          FORMAT TSVRaw
        ")"
        ;;
      gcp)
        manifest_keys="$(run_local_query "
          ${latest_manifest_cte}
          SELECT shard
          FROM latest
          ARRAY JOIN JSONExtract(json, '${manifest_file_array}', 'Array(String)') AS shard
          FORMAT TSVRaw
        ")"
        ;;
      *)
        echo "${cell}: unsupported manifest provider ${source_provider}" >&2
        exit 1
        ;;
    esac
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
      elif [[ ${source_provider} == "gcp" ]]; then
        report_path="${path_after_authority%/*}"
        full_shard_path="${report_path}/${clean_shard_path}"
      else
        full_shard_path="${destination_bucket}/${clean_shard_path}"
      fi
      shard_url="${manifest_scheme}://${manifest_authority}/${full_shard_path}"
      case "${source_provider}" in
        aws | floci)
          source="SELECT key AS object_key, size AS object_size, last_modified_date AS object_last_modified FROM s3('${shard_url}', ${credentials}'Parquet', 'key String, size Int64, last_modified_date DateTime64(3)')"
          metadata_source="SELECT num_rows FROM s3('${shard_url}', ${credentials}'ParquetMetadata')"
          ;;
        gcp)
          source="SELECT name AS object_key, size AS object_size, updated AS object_last_modified FROM s3('${shard_url}', ${credentials}'Parquet', 'name String, size Int64, updated DateTime64(3)')"
          metadata_source="SELECT num_rows FROM s3('${shard_url}', ${credentials}'ParquetMetadata')"
          ;;
        *)
          echo "${cell}: unsupported shard provider ${source_provider}" >&2
          exit 1
          ;;
      esac
      append_source "${cell}" "${storage_class}" "${source}" "${virtual_prefix}" "${metadata_source}"
    done <<<"${manifest_keys}"
  done <<<"${INVENTORY_MANIFEST_SOURCES}"
fi
if [[ -n ${INVENTORY_SOURCES:-} ]]; then
  while IFS='|' read -r cell source_provider credential_source storage_class key_prefix_to_strip logical_namespace url_template; do
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
    credentials="$(source_credentials "${credential_source}" "${cell}")"
    url="${url_template//\{date\}/${report_date}}"
    url="${url//\{year\}/${report_year}}"
    url="${url//\{month\}/${report_month}}"
    url="${url//\{day\}/${report_day}}"
    case "${source_provider}" in
      aws | floci)
        source="SELECT key AS object_key, size AS object_size, last_modified_date AS object_last_modified FROM s3('${url}', ${credentials}'Parquet', 'key String, size Int64, last_modified_date DateTime64(3)')"
        metadata_source="SELECT num_rows FROM s3('${url}', ${credentials}'ParquetMetadata')"
        ;;
      gcp)
        # TODO(simonepri): parse GCS Storage Insights reports (manifest format differs from S3 Inventory); the report
        # config in src/infra/terraform/components/storage/gcp/main.tf must first write to inventory/<bucket>/
        # instead of the shared inventory/ destination_path.
        echo "${cell}: GCS Storage Insights inventory ingestion is not implemented" >&2
        exit 1
        ;;
      *)
        echo "${cell}: unsupported source provider ${source_provider}" >&2
        exit 1
        ;;
    esac
    source_key="${cell}|${storage_class}"
    cell_prefixes[${source_key}]="${key_prefix_to_strip}"
    cell_namespaces[${source_key}]="${logical_namespace}"
    append_source "${cell}" "${storage_class}" "${source}" "" "${metadata_source}"
  done <<<"${INVENTORY_SOURCES}"
fi
for source_key in "${!cell_sources[@]}"; do
  IFS='|' read -r cell storage_class <<<"${source_key}"
  ingest_sources "${cell}" "${storage_class}" "${cell_prefixes[${source_key}]}" "${cell_namespaces[${source_key}]}" "${cell_sources[${source_key}]}" "${cell_dates[${source_key}]:-${report_date}}" "${cell_source_metadata[${source_key}]}"
done
if [[ ${#missing_manifests[@]} -gt 0 ]]; then
  printf 'storage inventory rollup is incomplete; missing reports: %s\n' "$(
    IFS=', '
    echo "${missing_manifests[*]}"
  )" >&2
  exit 75
fi
