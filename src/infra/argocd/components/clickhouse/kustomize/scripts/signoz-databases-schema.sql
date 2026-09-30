-- Idempotent SigNoz telemetry database schema applied by the schema sync hook.

-- LINT.IfChange(signoz-telemetry-database-contract)
  CREATE DATABASE IF NOT EXISTS signoz_traces ON CLUSTER '{cluster}';
  CREATE DATABASE IF NOT EXISTS signoz_metrics ON CLUSTER '{cluster}';
  CREATE DATABASE IF NOT EXISTS signoz_logs ON CLUSTER '{cluster}';
  CREATE DATABASE IF NOT EXISTS signoz_meter ON CLUSTER '{cluster}';
-- LINT.ThenChange(//src/infra/argocd/components/signoz/helm/values.yaml:signoz-telemetry-database-contract)

-- SigNoz telemetry MergeTree tables tiered storage and retention contract.
-- Tables specify storage_policy = 'hot_to_cold_policy' and transition aged
-- records to the 'cold' storage volume after 7 days before eventual deletion.
-- The schema apply hook (signoz-databases-schema-apply.sh) dynamically reconciles
-- these storage policies and TTL expressions across every cluster replica.
--
-- DDL schema contracts:
--
-- signoz_logs.logs_v2:
--   TTL toDateTime(timestamp / 1000000000) + INTERVAL 7 DAY TO VOLUME 'cold',
--       toDateTime(timestamp / 1000000000) + INTERVAL 30 DAY DELETE
--   SETTINGS storage_policy = 'hot_to_cold_policy';
--
-- signoz_traces.signoz_index_v2:
--   TTL toDateTime(timestamp) + INTERVAL 7 DAY TO VOLUME 'cold',
--       toDateTime(timestamp) + INTERVAL 30 DAY DELETE
--   SETTINGS storage_policy = 'hot_to_cold_policy';
--
-- signoz_traces.signoz_spans:
--   TTL toDateTime(timestamp) + INTERVAL 7 DAY TO VOLUME 'cold',
--       toDateTime(timestamp) + INTERVAL 30 DAY DELETE
--   SETTINGS storage_policy = 'hot_to_cold_policy';
--
-- signoz_metrics.samples_v4:
--   TTL toDateTime(unix_milli / 1000) + INTERVAL 7 DAY TO VOLUME 'cold',
--       toDateTime(unix_milli / 1000) + INTERVAL 90 DAY DELETE
--   SETTINGS storage_policy = 'hot_to_cold_policy';
--
-- signoz_metrics.time_series_v4:
--   TTL timestamp + INTERVAL 7 DAY TO VOLUME 'cold',
--       timestamp + INTERVAL 90 DAY DELETE
--   SETTINGS storage_policy = 'hot_to_cold_policy';

