-- Longitudinal patch observations: the time-series history behind a
-- "longitudinal" patch (e.g. Tech Rehearsal skill ratings over successive
-- practice runs).
--
-- Most patches are point-in-time facts: a re-observation is dedup-merged
-- into the existing row (see the worker's trigram/semantic dedup). For a
-- longitudinal patch that is wrong — "conflict question: Weak" on Monday and
-- "conflict question: Strong" on Friday are not the same fact to overwrite,
-- they are two points on a trajectory, and the trajectory is the value.
--
-- Model (docs/architecture/12-structured-ingest-and-longitudinal-patches.md):
--   - the context_patches row is the stable SERIES IDENTITY and holds only
--     the LATEST observation in `value`, keeping the recall hot path
--     unchanged and byte-stable.
--   - this table holds every observed point. Trend / Review reads it; the
--     hot path never touches it.
--
-- Series identity is CQ-derived: the worker trigram-matches an incoming
-- observation's descriptor field against active same-type series for the
-- subject (scoped to the same rehearsal via project_id) and appends; on no
-- match it opens a new series. Additive table — ShoulderSurf never writes it.

CREATE TABLE IF NOT EXISTS patch_observations (
    observation_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patch_id        UUID NOT NULL REFERENCES context_patches(patch_id) ON DELETE CASCADE,
    observed_at     TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    value           JSONB NOT NULL,            -- the point-in-time observation
    origin_id       TEXT,                      -- which run/session produced it
    origin_type     TEXT,
    source_app      UUID REFERENCES applications(app_id) ON DELETE CASCADE
);

-- Series read pattern: all points for a patch, in time order.
CREATE INDEX IF NOT EXISTS idx_patch_obs_series
    ON patch_observations (patch_id, observed_at);
