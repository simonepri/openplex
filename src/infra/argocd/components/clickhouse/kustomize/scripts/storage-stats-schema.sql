-- Idempotent storage_stats serving-cache schema applied by the schema hook and rollup.

-- LINT.IfChange(storage-stats-rollup-contract)
  CREATE DATABASE IF NOT EXISTS storage_stats ON CLUSTER '{cluster}';

  -- One row per (cell, storage_class, team, prefix, day). Each object rolls
  -- into every parent folder, and prefix_depth counts folders below
  -- s3/<cell> in the canonical virtual hierarchy.
  -- Re-ingesting a day inserts a newer version and ReplacingMergeTree
  -- collapses duplicates on merge, so readers query with FINAL.
  CREATE TABLE IF NOT EXISTS storage_stats.storage_prefix_daily_local ON CLUSTER '{cluster}'
  (
      day Date,
      cell LowCardinality(String),
      storage_class LowCardinality(String),
      team LowCardinality(String),
      prefix String,
      prefix_depth UInt16 DEFAULT 0,
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

  ALTER TABLE storage_stats.storage_prefix_daily_local ON CLUSTER '{cluster}'
    ADD COLUMN IF NOT EXISTS prefix_depth UInt16 DEFAULT 0 AFTER prefix;

  ALTER TABLE storage_stats.storage_prefix_daily_local ON CLUSTER '{cluster}'
    MODIFY COLUMN prefix_depth UInt16 DEFAULT 0;

  CREATE TABLE IF NOT EXISTS storage_stats.storage_prefix_daily ON CLUSTER '{cluster}'
  AS storage_stats.storage_prefix_daily_local
  ENGINE = Distributed('{cluster}', 'storage_stats', 'storage_prefix_daily_local', cityHash64(cell, storage_class, prefix));

  ALTER TABLE storage_stats.storage_prefix_daily ON CLUSTER '{cluster}'
    ADD COLUMN IF NOT EXISTS max_last_modified DateTime64(3, 'UTC') AFTER total_bytes;

  ALTER TABLE storage_stats.storage_prefix_daily ON CLUSTER '{cluster}'
    ADD COLUMN IF NOT EXISTS prefix_depth UInt16 DEFAULT 0 AFTER prefix;

  ALTER TABLE storage_stats.storage_prefix_daily ON CLUSTER '{cluster}'
    MODIFY COLUMN prefix_depth UInt16 DEFAULT 0;
-- LINT.ThenChange(//src/infra/definitions/observability/dashboards/object-storage.dashboard.yaml:storage-stats-rollup-readers)
