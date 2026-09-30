-- Idempotent storage_stats serving-cache schema applied by the schema hook and rollup.

-- LINT.IfChange(storage-stats-rollup-contract)
  CREATE DATABASE IF NOT EXISTS storage_stats ON CLUSTER '{cluster}';

  -- One row per (cell, storage_class, team, prefix, day). Prefix follows
  -- canonical virtual hierarchy: s3/<scope>/<storage_class>/... where scope
  -- is 'global' or the cell identifier.
  -- Re-ingesting a day inserts a newer version and ReplacingMergeTree
  -- collapses duplicates on merge, so readers query with FINAL.
  CREATE TABLE IF NOT EXISTS storage_stats.storage_prefix_daily_local ON CLUSTER '{cluster}'
  (
      day Date,
      cell LowCardinality(String),
      storage_class LowCardinality(String),
      team LowCardinality(String),
      prefix String,
      object_count UInt64,
      total_bytes UInt64,
      max_last_modified DateTime64(3, 'UTC'),
      ingested_at DateTime
  )
  ENGINE = ReplicatedReplacingMergeTree(ingested_at)
  PARTITION BY toYYYYMM(day)
  ORDER BY (cell, storage_class, team, prefix, day);

  ALTER TABLE storage_stats.storage_prefix_daily_local ON CLUSTER '{cluster}'
    ADD COLUMN IF NOT EXISTS max_last_modified DateTime64(3, 'UTC') AFTER total_bytes;

  CREATE TABLE IF NOT EXISTS storage_stats.storage_prefix_daily ON CLUSTER '{cluster}'
  AS storage_stats.storage_prefix_daily_local
  ENGINE = Distributed('{cluster}', 'storage_stats', 'storage_prefix_daily_local', cityHash64(cell, storage_class, prefix));

  ALTER TABLE storage_stats.storage_prefix_daily ON CLUSTER '{cluster}'
    ADD COLUMN IF NOT EXISTS max_last_modified DateTime64(3, 'UTC') AFTER total_bytes;
-- LINT.ThenChange(//src/infra/definitions/observability/dashboards/object-storage.dashboard.yaml:storage-stats-rollup-readers)
