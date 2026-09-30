#!/bin/sh
# Publishes completion manifests and prunes expired ClickHouse backup chains.

set -eu
: "${RETENTION_PROVIDER:?RETENTION_PROVIDER is required}"
: "${BACKUP_BUCKET:?BACKUP_BUCKET is required}"
: "${BACKUP_ROOT:?BACKUP_ROOT is required}"
: "${BACKUP_MANIFEST_PREFIX:?BACKUP_MANIFEST_PREFIX is required}"
: "${BACKUP_KEEP_CHAINS:?BACKUP_KEEP_CHAINS is required}"
: "${BACKUP_LOGS:?BACKUP_LOGS is required}"
: "${BACKUP_METRICS:?BACKUP_METRICS is required}"
: "${BACKUP_TRACES:?BACKUP_TRACES is required}"

list_objects() {
  case "${RETENTION_PROVIDER}" in
    aws)
      aws s3api list-objects-v2 --bucket "${BACKUP_BUCKET}" \
        --prefix "${BACKUP_ROOT}/$1/" --query 'Contents[].Key' \
        --output text | tr '\t' '\n'
      ;;
    gcp)
      gcloud storage ls --recursive \
        "gs://${BACKUP_BUCKET}/${BACKUP_ROOT}/$1/**" 2>/dev/null \
        | sed -n "s#^gs://${BACKUP_BUCKET}/##p"
      ;;
    *)
      echo "unsupported retention provider: ${RETENTION_PROVIDER}" >&2
      return 1
      ;;
  esac
}

delete_object() {
  case "${RETENTION_PROVIDER}" in
    aws) aws s3api delete-object --bucket "${BACKUP_BUCKET}" --key "$1" >/dev/null ;;
    gcp) gcloud storage rm "gs://${BACKUP_BUCKET}/$1" >/dev/null ;;
    *)
      echo "unsupported retention provider: ${RETENTION_PROVIDER}" >&2
      return 1
      ;;
  esac
}

list_manifest_objects() {
  prefix="$1"
  case "${RETENTION_PROVIDER}" in
    aws)
      aws s3api list-objects-v2 --bucket "${BACKUP_BUCKET}" \
        --prefix "${prefix}" --query 'Contents[].[Key,Size]' \
        --output text
      ;;
    gcp)
      gcloud storage ls --long --recursive \
        "gs://${BACKUP_BUCKET}/${prefix}**" 2>/dev/null \
        | awk -v root="gs://${BACKUP_BUCKET}/" \
          '$1 ~ /^[0-9]+$/ && index($3, root) == 1 {sub(root, "", $3); print $3 "\t" $1}'
      ;;
    *)
      echo "unsupported retention provider: ${RETENTION_PROVIDER}" >&2
      return 1
      ;;
  esac
}

read_manifest() {
  key="$1"
  case "${RETENTION_PROVIDER}" in
    aws) aws s3 cp "s3://${BACKUP_BUCKET}/${key}" - --only-show-errors ;;
    gcp) gcloud storage cat "gs://${BACKUP_BUCKET}/${key}" ;;
    *)
      echo "unsupported retention provider: ${RETENTION_PROVIDER}" >&2
      return 1
      ;;
  esac
}

create_manifest() {
  key="$1"
  case "${RETENTION_PROVIDER}" in
    aws)
      aws s3api put-object --bucket "${BACKUP_BUCKET}" --key "${key}" \
        --body /tmp/clickhouse-manifest.json \
        --content-type application/json --if-none-match '*' >/dev/null
      ;;
    gcp)
      gcloud storage cp /tmp/clickhouse-manifest.json \
        "gs://${BACKUP_BUCKET}/${key}" --if-generation-match=0 \
        --content-type=application/json --quiet
      ;;
    *)
      echo "unsupported retention provider: ${RETENTION_PROVIDER}" >&2
      return 1
      ;;
  esac
}

publish_completion_manifest() {
  database="$1"
  backup_name="$2"
  completed_at="$3"
  prefix="${BACKUP_ROOT}/${database}/${backup_name}/"
  manifest_key="${BACKUP_MANIFEST_PREFIX}/${backup_name}.json"
  list_manifest_objects "${prefix}" >/tmp/clickhouse-objects.tsv
  test -s /tmp/clickhouse-objects.tsv || {
    echo "completed ClickHouse backup has no objects: ${prefix}" >&2
    return 1
  }
  export BACKUP_CELL BACKUP_NAME="${backup_name}" COMPLETED_AT="${completed_at}" \
    OBJECT_PREFIX="${prefix}"
  python3 - <<'PY' >/tmp/clickhouse-manifest.json
import json
import os
import pathlib

maximum = 5 * 1024**3
objects = []
for line in pathlib.Path("/tmp/clickhouse-objects.tsv").read_text().splitlines():
    key, raw_size = line.rsplit("\t", 1)
    size = int(raw_size)
    if not key.startswith(os.environ["OBJECT_PREFIX"]):
        raise SystemExit(f"listed object escaped completed backup: {key}")
    if size > maximum:
        raise SystemExit(f"archive-eligible object exceeds 5 GiB: {key}")
    objects.append({"key": key, "size": size})
print(json.dumps({
    "cell": os.environ["BACKUP_CELL"],
    "completedAt": os.environ["COMPLETED_AT"],
    "kind": "telemetry",
    "objects": objects,
    "producer": "clickhouse",
    "schema": 1,
    "sourceCompletion": {
        "name": os.environ["BACKUP_NAME"],
        "phase": "completed",
        "uid": os.environ["BACKUP_NAME"],
    },
}, separators=(",", ":"), sort_keys=True))
PY
  # shellcheck disable=SC2310
  if read_manifest "${manifest_key}" >/tmp/existing-manifest.json 2>/dev/null; then
    cmp /tmp/clickhouse-manifest.json /tmp/existing-manifest.json
    return
  fi
  # shellcheck disable=SC2310
  if create_manifest "${manifest_key}"; then
    return
  fi
  read_manifest "${manifest_key}" >/tmp/existing-manifest.json
  cmp /tmp/clickhouse-manifest.json /tmp/existing-manifest.json
}

case "${BACKUP_KEEP_CHAINS}" in
  "" | 0 | *[!0-9]*)
    echo "BACKUP_KEEP_CHAINS must be a positive integer" >&2
    exit 1
    ;;
  *) ;;
esac
booleans="${BACKUP_LOGS} ${BACKUP_METRICS} ${BACKUP_TRACES}"
for value in ${booleans}; do
  case "${value}" in
    true | false) ;;
    *)
      echo "backup signal switches must be true or false" >&2
      exit 1
      ;;
  esac
done

while IFS='|' read -r database backup_name completed_at; do
  [ -n "${database}" ] || continue
  publish_completion_manifest "${database}" "${backup_name}" "${completed_at}"
done </tmp/clickhouse-completed

signals="signoz_logs:${BACKUP_LOGS} signoz_metrics:${BACKUP_METRICS} signoz_traces:${BACKUP_TRACES}"
for signal in ${signals}; do
  database="${signal%%:*}"
  enabled="${signal#*:}"
  [ "${enabled}" = true ] || continue
  objects="$(list_objects "${database}")"
  full_dates="$(printf '%s\n' "${objects}" | sed -n "s#^${BACKUP_ROOT}/${database}/${database}-\([0-9]\{8\}\)-full/.*#\1#p" | sort -u)"
  chain_count="$(printf '%s\n' "${full_dates}" | sed '/^$/d' | wc -l | tr -d ' ')"
  remove_count="$((chain_count - BACKUP_KEEP_CHAINS))"
  [ "${remove_count}" -gt 0 ] || continue
  old_dates="$(printf '%s\n' "${full_dates}" | head -n "${remove_count}")"
  printf '%s\n' "${objects}" | while IFS= read -r object; do
    [ -n "${object}" ] || continue
    for base_date in ${old_dates}; do
      case "${object}" in
        "${BACKUP_ROOT}/${database}/${database}-${base_date}-full/"* | "${BACKUP_ROOT}/${database}/${database}-"????????"-inc-of-${base_date}/"*)
          delete_object "${object}"
          break
          ;;
        *) ;;
      esac
    done
  done
done
