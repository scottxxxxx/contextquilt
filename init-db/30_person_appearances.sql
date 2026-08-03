-- person_appearances: which meetings a person actually showed up in.
--
-- The People design (docs/architecture/16-people.md) wants "9 meetings,
-- last 3d ago" on a person and "5 meetings together" per project. CQ
-- cannot answer either today:
--
--   * `person` patches are user-scoped by design, so origin_id and
--     project_id are forced NULL on them (src/worker.py, the
--     project_scoped_types gate). That is correct and should not change:
--     a person is not a fact about one meeting.
--   * entities.mention_count counts EXTRACTIONS, not distinct meetings,
--     and re-observation increments it from any lane.
--   * entities.metadata is written as `metadata || $2::jsonb`, so only
--     the most recent ingestion's origin_id survives the merge. The
--     history is overwritten, not accumulated.
--
-- So the appearance has to be recorded as its own row. Written by the
-- worker's store_entities on the cold path; the recall hot path never
-- reads this table.
--
-- Deliberately NOT here: a per-appearance `confirmed` flag. An earlier
-- draft of doc 16 specced one to back "six mentions you confirmed, five
-- still assumed", but nothing in CQ produces a per-meeting confirmation
-- signal: voice matching is ShoulderSurf's, and identification_source /
-- user_attribution_hint describe the USER's identity, not a third
-- party's. A column no writer populates would read as "zero confirmed"
-- and quietly become a lie. The read surface reports that split as
-- untracked instead.
--
-- Primary key is (user_id, entity_id, origin_id) so a person mentioned
-- five times in one meeting is one appearance, which is what "9 meetings"
-- means. Re-observation bumps last_seen_at.

CREATE TABLE IF NOT EXISTS person_appearances (
    user_id       TEXT NOT NULL,
    entity_id     UUID NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    origin_id     TEXT NOT NULL,
    origin_type   TEXT NOT NULL,
    project_id    TEXT,
    first_seen_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    last_seen_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, entity_id, origin_id)
);

-- The person detail read: this person's meetings, newest first.
CREATE INDEX IF NOT EXISTS idx_person_appearances_recent
    ON person_appearances (user_id, entity_id, last_seen_at DESC);

-- "Where does this person show up": per-project rollup.
CREATE INDEX IF NOT EXISTS idx_person_appearances_project
    ON person_appearances (user_id, entity_id, project_id)
    WHERE project_id IS NOT NULL;

-- "Who was in this meeting": the reverse lookup, used by the meeting view
-- and by backfills replaying the ingest stream.
CREATE INDEX IF NOT EXISTS idx_person_appearances_origin
    ON person_appearances (user_id, origin_id);
