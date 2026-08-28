-- 42: the three observed-behaviour signals that are EXACT from a
-- transcript. Doc 21 stage 2, first cut.
--
-- The third set of columns captured in the derive-then-discard pass that
-- already produces `turn_count` (migration 31) and the question columns
-- (migration 37), and it exists under the identical constraint: the
-- transcript is in hand exactly once, at ingest, and NONE of this can
-- ever be backfilled. Every meeting that landed before this shipped is
-- permanently unmeasurable on these three. That is the whole argument
-- for adding them before a surface reads them, which nothing does yet.
--
-- WHY ONLY THREE. The Memory Layer Spec lists six observed signals. The
-- other three (who assigned a follow-up and whether it was accepted, who
-- set or moved an agenda item, who deferred pending upstream input) need
-- semantics, and a model must identify them while code counts them
-- (doc 19.1). They are deliberately absent rather than approximated with
-- a keyword heuristic: a wrong signal accrues into the corpus exactly as
-- permanently as a right one, and this table is append-only history.
--
-- NULL IS UNKNOWN, FALSE IS OBSERVED. The distinction is the point and
-- it is the same one `turn_count` makes. A NULL here means no transcript
-- was parsed for this appearance (a structured-ingest lane, or a meeting
-- from before this shipped). A FALSE means the transcript WAS parsed and
-- this person did not do it. Anything that reads these must not collapse
-- the two, the way `capacities = '{}'` must not become "did not attend".
ALTER TABLE person_appearances
    -- Took the transcript's first turn. Awarded only when that turn
    -- belongs to a named speaker: if a diarization placeholder opened
    -- the room, nobody opened it as far as this record is concerned,
    -- because handing it to the first NAMED speaker would be a guess
    -- wearing an exact number's clothes.
    ADD COLUMN IF NOT EXISTS opened_meeting  BOOLEAN,
    -- Took the transcript's last turn, same rule.
    ADD COLUMN IF NOT EXISTS closed_meeting  BOOLEAN,
    -- Turns taken directly after ANOTHER speaker's turn that ended on a
    -- question. This is the same adjacency `question_attribution` grades
    -- as `inferred`, read from the answerer's side, so the two can never
    -- disagree about what an answer is.
    --
    -- NOT the spec's "terminal answerer of a question raised by someone
    -- outside their team": CQ has no team model, so that qualifier is
    -- unservable rather than merely hard, and "terminal" needs to know
    -- when a topic closed, which this parse cannot see. The column is
    -- named for the smaller claim it can actually support.
    ADD COLUMN IF NOT EXISTS answers_given   INTEGER;

COMMENT ON COLUMN person_appearances.opened_meeting IS
    'Took the first turn of the transcript. NULL = no transcript parsed; '
    'FALSE = parsed and they did not. Never backfillable.';
COMMENT ON COLUMN person_appearances.closed_meeting IS
    'Took the last turn of the transcript. NULL = no transcript parsed; '
    'FALSE = parsed and they did not. Never backfillable.';
COMMENT ON COLUMN person_appearances.answers_given IS
    'Turns taken directly after another speaker trailing question. Same '
    'adjacency as questions_received_inferred, from the answerer side. '
    'NOT terminal-answerer and NOT team-scoped. Never backfillable.';
