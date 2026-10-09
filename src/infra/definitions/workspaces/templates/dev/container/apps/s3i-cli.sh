#!/usr/bin/env bash
# shellcheck disable=SC2310,SC2312
# Queries multi-cell S3 Parquet metadata manifests using DuckDB without remote list API calls.

set -euo pipefail

usage() {
  printf '%s\n' \
    "Usage: s3i <command> [arguments]" \
    "" \
    "Commands:" \
    "  find [pattern] [flags]   Find objects matching glob/substring pattern" \
    "  ls [path]                List directory contents or cell buckets" \
    "  du [path]                Summarize disk usage and object count" \
    '  sql "<query>"            Run arbitrary SQL query against the objects inventory' \
    "" \
    "Flags for 'find':" \
    "  --cell <cell>            Filter by specific cell (e.g., eaws-lh1, egcp-lh1)" \
    "  --min-size <size>        Filter by minimum size (e.g., 10M, 1G)" \
    "  --max-size <size>        Filter by maximum size (e.g., 500M)" \
    "  --limit <n>              Limit number of results (default: 50)" \
    "  --format <table|json>    Output format (default: table)" \
    "" \
    "Examples:" \
    '  s3i find "*.safetensors"' \
    '  s3i find "*checkpoint*" --min-size 1G --cell eaws-lh1' \
    "  s3i ls eaws-lh1/scratch/models" \
    "  s3i du eaws-lh1/scratch" \
    '  s3i sql "SELECT cell, storage_class, count(*), format_bytes(sum(size)) FROM objects GROUP BY 1, 2"'
  exit 1
}

# Find all Parquet metadata shards
get_parquet_sources() {
  local target_cell="${1:-*}"

  if [[ -n ${S3_TEST_PARQUET_GLOB:-} ]]; then
    printf '%s\n' "${S3_TEST_PARQUET_GLOB}"
    return 0
  fi

  local search_paths=(
    "/fs/s3/${target_cell}/meta/inventory/**/*.parquet"
    "/fs/s3/${target_cell}/meta/dt=latest/*.parquet"
    "/fs/s3/${target_cell}/meta/**/*.parquet"
  )

  local old_globstar
  shopt -q globstar && old_globstar=1 || old_globstar=0
  shopt -s globstar

  local p
  for p in "${search_paths[@]}"; do
    if compgen -G "${p}" >/dev/null; then
      ((old_globstar)) || shopt -u globstar
      printf '%s\n' "${p}"
      return 0
    fi
  done

  ((old_globstar)) || shopt -u globstar

  return 1
}

run_find() {
  local pattern=""
  local cell="*"
  local min_size=""
  local max_size=""
  local limit=50
  local format="table"

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --cell)
        cell="$2"
        shift 2
        ;;
      --min-size)
        min_size="$2"
        shift 2
        ;;
      --max-size)
        max_size="$2"
        shift 2
        ;;
      --limit)
        limit="$2"
        shift 2
        ;;
      --format)
        format="$2"
        shift 2
        ;;
      -h | --help) usage ;;
      *)
        if [[ -z ${pattern} ]]; then
          pattern="$1"
          shift
        else
          printf 'Unknown argument: %s\n' "$1" >&2
          usage
        fi
        ;;
    esac
  done

  local parquet_path
  if ! parquet_path="$(get_parquet_sources "${cell}")"; then
    return 0
  fi

  local where_clauses=()
  if [[ -n ${pattern} ]]; then
    local sql_pattern="${pattern//\*/%}"
    sql_pattern="${sql_pattern//\?/_}"
    [[ ${sql_pattern} != *%* ]] && sql_pattern="%${sql_pattern}%"
    where_clauses+=("coalesce(COLUMNS('^(key|name)$')) ILIKE '${sql_pattern}'")
  fi

  if [[ -n ${min_size} ]]; then
    where_clauses+=("size >= parse_bytes('${min_size}')")
  fi
  if [[ -n ${max_size} ]]; then
    where_clauses+=("size <= parse_bytes('${max_size}')")
  fi

  local where_sql=""
  if [[ ${#where_clauses[@]} -gt 0 ]]; then
    where_sql="WHERE ${where_clauses[0]}"
    local clause
    for clause in "${where_clauses[@]:1}"; do
      where_sql+=" AND ${clause}"
    done
  fi

  local duckdb_cmd=(duckdb)
  if [[ ${format} == "json" ]]; then
    duckdb_cmd+=(-json)
  fi

  local query="
    SELECT 
      regexp_extract(filename, '/fs/s3/([^/]+)/meta', 1) AS cell,
      format_bytes(size) AS size,
      strftime(coalesce(COLUMNS('^(last_modified_date|updated)$')), '%Y-%m-%d %H:%M:%S') AS modified,
      coalesce(COLUMNS('^(storage_class|storageClass)$')) AS storage_class,
      coalesce(COLUMNS('^(key|name)$')) AS key
    FROM read_parquet('${parquet_path}', filename=true, union_by_name=true)
    ${where_sql}
    ORDER BY size DESC
    LIMIT ${limit};
  "

  "${duckdb_cmd[@]}" -c "${query}"
}

run_ls() {
  local target_path="${1:-}"
  local cell="*"
  local prefix=""

  if [[ -n ${target_path} ]]; then
    cell="${target_path%%/*}"
    prefix="${target_path#*/}"
    [[ ${prefix} == "${cell}" ]] && prefix=""
    [[ -n ${prefix} && ${prefix} != */ ]] && prefix="${prefix}/"
  fi

  local parquet_path
  if ! parquet_path="$(get_parquet_sources "${cell}")"; then
    return 0
  fi

  local query="
    SELECT 
      regexp_extract(filename, '/fs/s3/([^/]+)/meta', 1) AS cell,
      CASE WHEN coalesce(COLUMNS('^(key|name)$')) LIKE '${prefix}%/%' THEN
        concat('${prefix}', split_part(substr(coalesce(COLUMNS('^(key|name)$')), length('${prefix}') + 1), '/', 1), '/')
      ELSE coalesce(COLUMNS('^(key|name)$')) END AS item,
      CASE WHEN coalesce(COLUMNS('^(key|name)$')) LIKE '${prefix}%/%' THEN 'DIR' ELSE format_bytes(size) END AS size,
      strftime(max(coalesce(COLUMNS('^(last_modified_date|updated)$'))), '%Y-%m-%d %H:%M:%S') AS modified
    FROM read_parquet('${parquet_path}', filename=true, union_by_name=true)
    WHERE coalesce(COLUMNS('^(key|name)$')) LIKE '${prefix}%'
    GROUP BY cell, item, (CASE WHEN coalesce(COLUMNS('^(key|name)$')) LIKE '${prefix}%/%' THEN 'DIR' ELSE format_bytes(size) END)
    ORDER BY item ASC
    LIMIT 200;
  "

  duckdb -c "${query}"
}

run_du() {
  local target_path="${1:-}"
  local cell="*"
  local prefix=""

  if [[ -n ${target_path} ]]; then
    cell="${target_path%%/*}"
    prefix="${target_path#*/}"
    [[ ${prefix} == "${cell}" ]] && prefix=""
    [[ -n ${prefix} && ${prefix} != */ ]] && prefix="${prefix}/"
  fi

  local parquet_path
  if ! parquet_path="$(get_parquet_sources "${cell}")"; then
    return 0
  fi

  local query="
    SELECT 
      regexp_extract(filename, '/fs/s3/([^/]+)/meta', 1) AS cell,
      CASE WHEN coalesce(COLUMNS('^(key|name)$')) LIKE '${prefix}%/%' THEN
        concat('${prefix}', split_part(substr(coalesce(COLUMNS('^(key|name)$')), length('${prefix}') + 1), '/', 1), '/')
      ELSE coalesce(COLUMNS('^(key|name)$')) END AS prefix,
      count(*) AS object_count,
      format_bytes(sum(size)) AS total_size
    FROM read_parquet('${parquet_path}', filename=true, union_by_name=true)
    WHERE coalesce(COLUMNS('^(key|name)$')) LIKE '${prefix}%'
    GROUP BY cell, prefix
    ORDER BY sum(size) DESC
    LIMIT 200;
  "

  duckdb -c "${query}"
}

run_sql() {
  local custom_query="${1:?SQL query is required}"
  local parquet_path
  if ! parquet_path="$(get_parquet_sources "*")"; then
    return 0
  fi

  local query="
    CREATE TEMP VIEW objects AS
      SELECT
        regexp_extract(filename, '/fs/s3/([^/]+)/meta', 1) AS cell,
        coalesce(COLUMNS('^(key|name)$')) AS key,
        size,
        coalesce(COLUMNS('^(last_modified_date|updated)$')) AS last_modified,
        coalesce(COLUMNS('^(storage_class|storageClass)$')) AS storage_class
      FROM read_parquet('${parquet_path}', filename=true, union_by_name=true);
    ${custom_query}
  "

  duckdb -c "${query}"
}

main() {
  [[ $# -ge 1 ]] || usage

  local cmd="$1"
  shift

  case "${cmd}" in
    find) run_find "$@" ;;
    ls) run_ls "$@" ;;
    du) run_du "$@" ;;
    sql) run_sql "$@" ;;
    -h | --help) usage ;;
    *)
      printf 'Unknown command: %s\n' "${cmd}" >&2
      usage
      ;;
  esac
}

main "$@"
