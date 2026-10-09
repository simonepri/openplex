#!/usr/bin/env bash
# Runs provider-neutral native ClickHouse backup step during scheduled runs.

set -eu
shopt -s inherit_errexit

: "${BACKUP_ENGINE:?BACKUP_ENGINE is required}"
: "${CLICKHOUSE_HOST:?CLICKHOUSE_HOST is required}"
: "${CLICKHOUSE_PORT:?CLICKHOUSE_PORT is required}"
: "${CLICKHOUSE_USERNAME:?CLICKHOUSE_USERNAME is required}"
: "${BACKUP_CLUSTER:?BACKUP_CLUSTER is required}"
: "${BACKUP_LOGS:?BACKUP_LOGS is required}"
: "${BACKUP_METRICS:?BACKUP_METRICS is required}"
: "${BACKUP_TRACES:?BACKUP_TRACES is required}"

backup_target() {
  case "${BACKUP_ENGINE}" in
    S3) printf "S3(clickhouse_backup, '%s')" "$1" ;;
    *)
      echo "unsupported native backup engine: ${BACKUP_ENGINE}" >&2
      return 1
      ;;
  esac
}

client() {
  clickhouse-client --host "${CLICKHOUSE_HOST}" --port "${CLICKHOUSE_PORT}" \
    --user "${CLICKHOUSE_USERNAME}" "$@"
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
# Projected HMAC include files rotate independently of the
# server Pod. Reload on every replica before resolving the
# named collection so the next backup uses the current key.
client --query "SYSTEM RELOAD CONFIG ON CLUSTER \`${BACKUP_CLUSTER}\`"
day_of_week="$(date -u +%u)"
date_stamp="$(date -u +%Y%m%d)"
days_since_sunday="$((day_of_week % 7))"
base_date="$(date -u -d "${days_since_sunday} days ago" +%Y%m%d)"
: >/tmp/clickhouse-completed

signals=(
  "logs|signoz_logs|${BACKUP_LOGS}"
  "metrics|signoz_metrics|${BACKUP_METRICS}"
  "traces|signoz_traces|${BACKUP_TRACES}"
)
for signal in "${signals[@]}"; do
  IFS='|' read -r signal_name database enabled <<<"${signal}"
  case "${enabled}" in
    true) ;;
    false) continue ;;
    *)
      echo "BACKUP_${signal_name^^} must be true or false" >&2
      exit 1
      ;;
  esac

  if [[ ${day_of_week} -eq 7 ]]; then
    backup_name="${database}-${date_stamp}-full"
    destination="$(backup_target "${database}/${backup_name}")"
    query="BACKUP DATABASE \`${database}\` ON CLUSTER \`${BACKUP_CLUSTER}\` TO ${destination}"
  else
    backup_name="${database}-${date_stamp}-inc-of-${base_date}"
    destination="$(backup_target "${database}/${backup_name}")"
    base="$(backup_target "${database}/${database}-${base_date}-full")"
    query="BACKUP DATABASE \`${database}\` ON CLUSTER \`${BACKUP_CLUSTER}\` TO ${destination} SETTINGS base_backup = ${base}"
  fi

  # shellcheck disable=SC2310
  if ! error="$(client --query "${query}" 2>&1 >/dev/null)"; then
    if [[ ${day_of_week} -ne 7 && ${error} == *BACKUP_NOT_FOUND* ]]; then
      # The week's Sunday full backup is missing, so start the chain now under its name.
      backup_name="${database}-${base_date}-full"
      destination="$(backup_target "${database}/${backup_name}")"
      client --query "BACKUP DATABASE \`${database}\` ON CLUSTER \`${BACKUP_CLUSTER}\` TO ${destination}"
    elif [[ ${error} != *BACKUP_ALREADY_EXISTS* ]]; then
      # BACKUP_ALREADY_EXISTS means an earlier attempt of this Job completed the backup.
      echo "${error}" >&2
      exit 1
    fi
  fi
  completed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '%s|%s|%s\n' "${database}" "${backup_name}" "${completed_at}" >>/tmp/clickhouse-completed
done
