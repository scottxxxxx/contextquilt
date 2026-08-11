-- The ego link: which person entity IS the submitting user.
--
-- The 13b orbit graph excludes the ego's edges, and nothing in the
-- schema could answer "which node is the ego": capacities describe HOW
-- someone entered a meeting record (patch ownership, speaker, mention),
-- not WHO the app user is, and 80+ entities carry `ownership` on prod.
--
-- The stamp lives on the entity row, mirroring the suppression pattern
-- (33): a timestamp plus a source. At most ONE self entity per user,
-- enforced by a partial unique index; the write path keeps the first
-- stamp and logs a conflict rather than moving it, because a moving ego
-- would silently re-shape every graph read.
--
-- self_source vocabulary (open, like suppressed_source):
--   you_marker  - the (you)-labeled speaker resolved to this entity at
--                 ingest (exact, go-forward)
--   backfill    - scripts/backfill_self_entities.py coverage heuristic
--                 (historical, one-time)

ALTER TABLE entities ADD COLUMN IF NOT EXISTS self_at TIMESTAMPTZ;
ALTER TABLE entities ADD COLUMN IF NOT EXISTS self_source TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uniq_entities_self_per_user
    ON entities (user_id) WHERE self_at IS NOT NULL;
