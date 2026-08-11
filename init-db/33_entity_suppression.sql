-- Migration 33: entity suppression ("not a person", boundary piece 3).
--
-- Removing Delete Memory from person rows (the People/Memory boundary
-- move) removed the only affordance a user had against ASR garbage in
-- their roster ("Horm Hel"). None of the identity verbs handles "this
-- was never a person": rename fixes a name, merge folds duplicates,
-- keep-separate records a negative pair, confirm vouches. Suppression
-- is the missing verb.
--
-- The SUPPRESSED ROW IS THE NEGATIVE RECORD. The entity survives,
-- marked, so the next transcript's diarization emitting the same
-- surface form exact-matches the suppressed row instead of minting the
-- garbage again (the same durable-no lesson entity_separations
-- taught). Serving-side, a suppressed entity is excluded from
-- /v1/people, the recall entity index, and appearance recording; its
-- person patch archives with archive_cause 'not_a_person'.
--
-- Reversible by design: ASR garbage and a real person with an
-- unfortunate transcription can collide, and an unfixable wrong answer
-- is worse than a reversible one.

ALTER TABLE entities ADD COLUMN IF NOT EXISTS suppressed_at TIMESTAMPTZ;
ALTER TABLE entities ADD COLUMN IF NOT EXISTS suppressed_source TEXT;

-- The people-list tombstone query scans for recently suppressed rows;
-- partial index keeps it cheap and the common (unsuppressed) case free.
CREATE INDEX IF NOT EXISTS idx_entities_suppressed
    ON entities (user_id, suppressed_at)
    WHERE suppressed_at IS NOT NULL;
