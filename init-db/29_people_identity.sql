-- People identity write-back: the durable record of who the user says
-- is who.
--
-- ShoulderSurf's People feature (docs/architecture/16-people.md) asks the
-- user identity questions CQ cannot answer on its own: "Is this the same
-- person?" with Same person / Keep separate, "confirm this person is
-- real", "create a person from a name in a report". Today none of those
-- answers have anywhere to land. entity_aliases.source already
-- anticipates the value 'app' but nothing writes it, and rename-speaker /
-- reassign-speaker mean different things (rename an entity; move patches
-- between diarization labels), neither of which is "these two entities
-- are one human".
--
-- Without this, SS either keeps identity locally (a split brain with CQ's
-- entity graph, same failure class as the ABM project-id split one layer
-- down) or re-asks a question the user already answered.
--
-- Three additions:
--
-- 1. entities.confirmed_at / confirmation_source: a person CQ inferred
--    from a transcript versus one a human vouched for. The design renders
--    these differently ("Named in 2 transcripts, not confirmed").
--
-- 2. entities.merged_into / merged_at: merge is a forward pointer, not a
--    delete. Deleting the losing row would cascade its relationships away
--    (relationships.from/to_entity_id are ON DELETE CASCADE) and would
--    break the entity_ids clients already hold: SS passes to_person_id to
--    POST /v1/quilt/{u}/reassign-speaker today. A merged row stays
--    readable and resolves forward to its canonical, so a stale client id
--    self-heals instead of 404ing.
--
-- 3. entity_separations, the NEGATIVE answer. "Keep separate" has to be
--    as durable as "Same person", otherwise the merge endpoint and
--    scripts/backfill_entity_aliases.py will re-merge a pair the user
--    already pulled apart, and any future merge-proposal read will nag
--    the user with a question they have answered.
--
--    Scope note: the worker's live alias heuristic (store_entities step 3)
--    is NOT the threat this guards. That path can only attach a new
--    surface form to an existing entity or promote an entity to a fuller
--    incoming name; an incoming name that already IS an entity is caught
--    by the exact-match step first, so it cannot fuse two existing rows.
--    The real consumers are the merge endpoint, the backfill, and
--    proposals.

ALTER TABLE entities ADD COLUMN IF NOT EXISTS confirmed_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE entities ADD COLUMN IF NOT EXISTS confirmation_source TEXT;

ALTER TABLE entities ADD COLUMN IF NOT EXISTS merged_into UUID
    REFERENCES entities(entity_id) ON DELETE SET NULL;
ALTER TABLE entities ADD COLUMN IF NOT EXISTS merged_at TIMESTAMP WITH TIME ZONE;

-- Active-entity reads (the People list, merge validation) filter on this.
CREATE INDEX IF NOT EXISTS idx_entities_active
    ON entities (user_id, entity_type)
    WHERE merged_into IS NULL;

-- Forward-resolution lookups: "everything that merged into X".
CREATE INDEX IF NOT EXISTS idx_entities_merged_into
    ON entities (merged_into)
    WHERE merged_into IS NOT NULL;

-- Pairs the user has explicitly said are NOT the same person.
--
-- The pair is unordered: separating (A, B) must also block (B, A). The
-- write path canonicalises to (lo, hi) by UUID text ordering so the
-- primary key does that enforcement instead of application code
-- remembering to check both directions.
CREATE TABLE IF NOT EXISTS entity_separations (
    user_id      TEXT NOT NULL,
    entity_id_lo UUID NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    entity_id_hi UUID NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    source       TEXT NOT NULL DEFAULT 'app',
    created_at   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, entity_id_lo, entity_id_hi),
    CONSTRAINT entity_separations_ordered CHECK (entity_id_lo < entity_id_hi)
);

-- "Is this entity separated from anything?" from either side.
CREATE INDEX IF NOT EXISTS idx_entity_separations_hi
    ON entity_separations (user_id, entity_id_hi);
