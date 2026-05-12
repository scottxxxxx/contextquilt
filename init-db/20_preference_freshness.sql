-- Preference / trait freshness model.
--
-- Self-typed patches about the (you) speaker — trait, preference,
-- goal, constraint — are "sticky" in the persistence model. Today
-- they live forever once observed, and recall surfaces an 18-month-
-- old preference with the same weight as one re-affirmed this week.
--
-- This migration adds a dedicated `last_observed_at` timestamp,
-- distinct from `updated_at` (any edit) and `patch_usage_metrics
-- .last_accessed_at` (recall hit). It is bumped only when the
-- worker's pg_trgm dedup path matches a fresh extraction to an
-- existing patch — i.e., the user re-affirmed this preference in
-- a new transcript.
--
-- Downstream:
--   - Decay worker uses last_observed_at as the staleness anchor
--     for trait/preference/goal/constraint with a long horizon
--     (default 540d, configurable per type via patch_type_registry
--     .default_ttl_days).
--   - Recall scorer uses last_observed_at for recency normalization
--     and applies a multiplicative staleness penalty for these
--     self-typed patches.
--
-- Backfill: existing rows get last_observed_at = updated_at
-- (falling back to created_at on the unlikely chance updated_at is
-- NULL). updated_at is closer to the true freshness signal than
-- created_at for already-deployed data: the worker dedup path has
-- been bumping updated_at on every re-observation for months, so
-- treating that as the historical proxy preserves the signal we'd
-- otherwise throw away. The slight overcount (admin edits also move
-- updated_at) is conservative — we'd rather keep a maybe-stale patch
-- than incorrectly archive a recently-re-affirmed one.

ALTER TABLE context_patches
    ADD COLUMN IF NOT EXISTS last_observed_at TIMESTAMP WITH TIME ZONE;

UPDATE context_patches
    SET last_observed_at = COALESCE(updated_at, created_at)
    WHERE last_observed_at IS NULL;

-- Partial index targets the four self-typed types the freshness
-- model applies to. Decay worker and recall scorer both filter on
-- patch_type IN (...) AND last_observed_at < threshold.
CREATE INDEX IF NOT EXISTS idx_patches_self_typed_freshness
    ON context_patches(patch_type, last_observed_at)
    WHERE patch_type IN ('trait', 'preference', 'goal', 'constraint')
      AND COALESCE(status, 'active') = 'active';
