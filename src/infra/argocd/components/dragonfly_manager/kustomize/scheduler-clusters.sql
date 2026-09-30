-- Reconciles fleet-owned Dragonfly scheduler cluster IDs after Manager migration for the ctrl bootstrap Job.

\set ON_ERROR_STOP on
BEGIN;

CREATE TEMP TABLE desired_scheduler_clusters (
  id bigint PRIMARY KEY CHECK (id > 0),
  name text UNIQUE NOT NULL CHECK (name <> '')
);
INSERT INTO desired_scheduler_clusters
SELECT id, name FROM jsonb_to_recordset(:'desired_clusters'::jsonb) AS desired(id bigint, name text);

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM desired_scheduler_clusters desired
    JOIN scheduler_cluster actual ON actual.name = desired.name
    WHERE desired.id = 1 AND desired.name = 'cell-eaws-lh1' AND actual.id <> 1
  ) THEN
    RAISE EXCEPTION 'Dragonfly scheduler cluster canonical local name is already assigned';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM desired_scheduler_clusters desired
    JOIN scheduler_cluster actual ON actual.id = desired.id
    WHERE desired.id = 1
      AND desired.name = 'cell-eaws-lh1'
      AND actual.name NOT IN ('cluster-1', 'cell-eaws-lh1')
  ) THEN
    RAISE EXCEPTION 'Dragonfly scheduler cluster ID 1 has an unexpected durable name';
  END IF;
END;
$$;

UPDATE scheduler_cluster
SET name = 'cell-eaws-lh1'
WHERE id = 1
  AND name = 'cluster-1'
  AND EXISTS (
    SELECT 1 FROM desired_scheduler_clusters
    WHERE id = 1 AND name = 'cell-eaws-lh1'
  );

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM desired_scheduler_clusters desired
    JOIN scheduler_cluster actual ON actual.id = desired.id OR actual.name = desired.name
    WHERE actual.id <> desired.id OR actual.name <> desired.name
  ) THEN
    RAISE EXCEPTION 'Dragonfly scheduler cluster ID/name drift';
  END IF;
END;
$$;

INSERT INTO scheduler_cluster (
  created_at, updated_at, name, bio, config, client_config,
  seed_client_config, scopes, is_default, id
)
SELECT
  now(), now(), name, '',
  '{"candidate_parent_limit":3,"filter_parent_limit":15,"job_rate_limit":10}'::jsonb,
  '{"load_limit":200}'::jsonb, '{}'::jsonb, '{}'::jsonb, false, id
FROM desired_scheduler_clusters
ON CONFLICT (id) DO NOTHING;

SELECT setval(
  pg_get_serial_sequence('scheduler_cluster', 'id'),
  GREATEST((SELECT max(id) FROM scheduler_cluster), 1),
  true
);
COMMIT;
