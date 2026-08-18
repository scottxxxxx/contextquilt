-- How a person has been DESCRIBED over time, as a series rather than a
-- single overwritten value.
--
-- Brian noticed Suresh's description change between meetings: one day it
-- read "scrum master", because he had introduced himself that way to a
-- new joiner, and the next meeting it read something else. His response
-- was not "which is right" or "stop the flapping". It was: I want every
-- iteration historically of who you thought this person was, because my
-- hats kept changing and that shifted perception, and that is
-- organizational health and a watchpoint.
--
-- Until now `store_entities._reobserve` did:
--     description = COALESCE(NULLIF($1, ''), description)
-- so every meeting OVERWROTE it and the series was destroyed as it was
-- created. The "scrum master" reading Brian saw is already gone.
--
-- This is the trajectory-not-duplicate rule arriving from outside. We
-- already hold it: the manifest key `collapse_duplicates: false` exists
-- because two observations of one behavior are a trajectory, not a
-- duplicate, and a collapse keeps one origin_id so it destroys a
-- receipt. We applied that to `behavior` and never to descriptions.
--
-- ONE ROW PER DISTINCT DESCRIPTION, not per observation. A person
-- described the same way across forty meetings is one perception
-- confirmed forty times, not forty iterations; `observation_count`
-- carries the confirmations. A paraphrase is not a new perception
-- either, so the write path compares by trigram similarity the way the
-- patch dedup does, and only a genuinely different description appends.
--
-- The stored text is NEVER rewritten once appended. Same discipline as
-- `value.restatements` on the ledger: the row is a receipt, and a later
-- rephrasing that overwrote it would destroy the thing this table
-- exists to keep.

CREATE TABLE IF NOT EXISTS entity_descriptions (
    description_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id            TEXT NOT NULL,
    entity_id          UUID NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,

    -- The description as observed. Immutable after insert.
    description        TEXT NOT NULL,

    -- Which meeting first produced this perception, so a client can open
    -- the receipt. Nullable: chat and structured lanes reach the sink
    -- without an origin.
    first_origin_id    TEXT,
    first_origin_type  TEXT,

    -- The meeting that most recently confirmed it. Doubles as the
    -- idempotency key: a re-ingest is the same observation arriving
    -- twice (doc 19.4), so a repeat of this origin must not inflate
    -- observation_count.
    last_origin_id     TEXT,

    first_observed_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    last_observed_at   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    observation_count  INTEGER NOT NULL DEFAULT 1,

    -- Which lane observed it, on the same open vocabulary as elsewhere
    -- (meeting_summary, chat, structured, declared).
    source             TEXT
);

-- The read: this person's series, newest perception first. Also the
-- write path's "what did we last think" lookup.
CREATE INDEX IF NOT EXISTS idx_entity_descriptions_series
    ON entity_descriptions (user_id, entity_id, first_observed_at DESC);

-- The rollup a People list would need to show a changed indicator
-- without fetching every series.
CREATE INDEX IF NOT EXISTS idx_entity_descriptions_recent
    ON entity_descriptions (user_id, last_observed_at DESC);
