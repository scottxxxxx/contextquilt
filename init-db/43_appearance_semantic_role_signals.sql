-- 43: the three observed-behaviour signals that need SEMANTICS, plus
-- the sixth one migration 42 left behind. Doc 21 stage 2, second cut.
--
-- Migration 42 captured the three signals that are exact from a
-- transcript and said in its own comment why the others were absent:
-- they need a reader to understand what was said, and a keyword
-- heuristic would have written wrong signals into an append-only
-- corpus exactly as permanently as right ones. They arrive here the way
-- that comment said they would, through a model that IDENTIFIES while
-- code COUNTS (doc 19.1), in its own call on the `behavior_extraction`
-- pattern (`services/role_semantics.py`).
--
-- Same one pass, same never-backfillable constraint as `turn_count`
-- (31), the question columns (37) and the exact signals (42). Every
-- meeting that landed before this shipped is permanently unmeasurable
-- on all four. Nothing reads them yet.
--
-- HOW THE ATTRIBUTION WORKS, because it is the reason these are
-- trustworthy enough to store. The model returns POINTERS, never names
-- and never numbers: a turn index, and for a follow up the name of the
-- person the work was handed to. WHO did it is read off that turn's own
-- speaker label, from the same `transcript_turns` parse that produced
-- `turn_count`. A model that mis-attributes a line therefore cannot
-- move a signal onto the wrong person, and a citation to a turn that
-- does not exist is dropped rather than counted.
--
-- NULL IS UNKNOWN, AND A ZERO HERE IS WEAKER THAN MIGRATION 42's FALSE.
-- NULL means the semantic pass did not run for this appearance (no
-- transcript, an app whose lane never calls it, a meeting from before
-- this shipped, or a failed call). A zero means the pass RAN and the
-- model identified none of this for this person, which is a judgment
-- and can be a miss. Nothing reading these may promote a zero into
-- "they never do this", and nothing may collapse it with NULL either.
ALTER TABLE person_appearances
    -- Handed a piece of work to ANOTHER named participant, to be done
    -- after the meeting. Never a self-assignment: that is a commitment
    -- and the main extraction owns it.
    ADD COLUMN IF NOT EXISTS follow_ups_assigned  INTEGER,
    -- Of those, the ones the assignee agreed to in the room. A SUBSET of
    -- follow_ups_assigned, so the two are never summed. THE GAP BETWEEN
    -- THEM IS NOT A REFUSAL COUNT: silence, "I will look at it", and a
    -- flat no all land outside `accepted`, and only an explicit yes is
    -- inside it. A surface that subtracts one from the other is
    -- asserting something nobody observed.
    ADD COLUMN IF NOT EXISTS follow_ups_accepted  INTEGER,
    -- Set or changed what the meeting was going to talk about. An
    -- explicit steer, not a topic drifting. Deduplicated on the turn:
    -- one turn cannot set the agenda twice.
    ADD COLUMN IF NOT EXISTS agenda_moves         INTEGER,
    -- Said their own item cannot move until somebody or something
    -- outside their control supplies an input. Not "I have not got to
    -- it", which has no upstream in it, and not blocking somebody else.
    ADD COLUMN IF NOT EXISTS upstream_deferrals   INTEGER,
    -- The spec's sixth signal, the one migration 42's docstring dropped
    -- by counting opened and closed separately. `turn_count` is a bare
    -- count with no split; these two are the split.
    --
    -- directive_turns + responsive_turns IS NOT turn_count and must
    -- never be treated as it. A turn the model could not clearly grade
    -- is left unclassified on purpose, because guessing at an ambiguous
    -- turn is how a share becomes fiction. The remainder is
    -- "unclassified", never a third behaviour.
    ADD COLUMN IF NOT EXISTS directive_turns      INTEGER,
    ADD COLUMN IF NOT EXISTS responsive_turns     INTEGER;

COMMENT ON COLUMN person_appearances.follow_ups_assigned IS
    'Work handed to another named participant for after the meeting. '
    'Model identifies the turn, code reads the assigner off the turn. '
    'NULL = no semantic pass; 0 = pass ran and identified none. '
    'Never backfillable.';
COMMENT ON COLUMN person_appearances.follow_ups_accepted IS
    'Subset of follow_ups_assigned the assignee explicitly agreed to in '
    'the room. The gap between the two is NOT refusals. Never '
    'backfillable.';
COMMENT ON COLUMN person_appearances.agenda_moves IS
    'Turns that set or changed what the meeting was about. Deduplicated '
    'per turn. NULL = no semantic pass; 0 = identified none. Never '
    'backfillable.';
COMMENT ON COLUMN person_appearances.upstream_deferrals IS
    'Turns where the speaker said their own item waits on an input '
    'outside their control. NULL = no semantic pass; 0 = identified '
    'none. Never backfillable.';
COMMENT ON COLUMN person_appearances.directive_turns IS
    'Turns graded directive (steered what happens next). With '
    'responsive_turns this is the spec''s sixth signal. The two do NOT '
    'sum to turn_count: unclear turns stay unclassified. Never '
    'backfillable.';
COMMENT ON COLUMN person_appearances.responsive_turns IS
    'Turns graded responsive (answered, reported, supplied what was '
    'asked for). See directive_turns. Never backfillable.';
