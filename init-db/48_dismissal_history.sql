-- An UN-dismissal destroyed the fact that a dismissal ever happened,
-- which is the exact thing migration 46 exists to prevent.
--
-- Migration 46 argued the case in its own header and got it right for
-- the dismissal: the row is RETAINED and carries its stamp, because
-- deleting it "would make 'the user said this was wrong'
-- indistinguishable from 'this was never observed'". Then DELETE on the
-- route set dismissed_at, dismissed_source and dismissed_note back to
-- NULL, leaving a row byte-identical to one nobody had ever objected
-- to. The rule was applied to the verb and not to its inverse.
--
-- THE RECEIPT, and it is not hypothetical. On 2026-09-12 this cost a
-- wrong conclusion that travelled. The account showed zero dismissals
-- in entity_descriptions, and that was read as "the dismissal path has
-- never run in production" -- written into a handoff, and repeated to
-- two other teams as fact. It had run. Bifrost's edge log holds
-- 08-31 16:52:56Z POST dismiss on entity 63b6895b, 200 on both hops,
-- and 09-01 01:33:06Z DELETE on the same entity, 200 on both hops. A
-- dismissal and its undo NET TO ZERO, and a net zero is indistinguishable
-- from an absence. GhostPour named the shape while we were comparing
-- notes: WE WERE COUNTING STATE AND READING IT AS HISTORY. A state
-- count can only ever answer "what is true now".
--
-- The user's typed words were the worst of it. 46's own comment on
-- dismissed_note says the difference between "this is inaccurate" and
-- "correct this, because <reason>" "is worth keeping". The undo
-- overwrote that text with NULL, permanently, with no other copy on the
-- row.
--
-- THE FIX IS THE SIBLING ROUTE'S, ALREADY SHIPPED. /uncomplete faced
-- the identical question for completions and answered it in its
-- docstring: the original completion is preserved as
-- value.prior_completed_at / prior_completion_source /
-- prior_completion_evidence alongside uncompleted_at, "so 'completed
-- then reopened' is never indistinguishable from 'never completed'".
-- One rule, two carriers, and only one of them was holding it. This
-- migration gives the dismissal the same columns.
--
-- DELIBERATELY ADDITIVE, AND THE READ SIDE DOES NOT MOVE. Every reader
-- filters on `dismissed_at IS NULL` or `IS NOT NULL`, and the partial
-- index in 46 is defined on the same predicate. The undo still clears
-- the live stamps, so "is this row live right now" is answered by
-- exactly the column it was answered by yesterday. Nothing here changes
-- what is served; it changes only what survives.

ALTER TABLE entity_descriptions
    ADD COLUMN IF NOT EXISTS prior_dismissed_at TIMESTAMP WITH TIME ZONE;

ALTER TABLE entity_descriptions
    ADD COLUMN IF NOT EXISTS prior_dismissed_source TEXT;

-- The user's own words, carried across the undo rather than erased.
ALTER TABLE entity_descriptions
    ADD COLUMN IF NOT EXISTS prior_dismissed_note TEXT;

ALTER TABLE entity_descriptions
    ADD COLUMN IF NOT EXISTS undismissed_at TIMESTAMP WITH TIME ZONE;

-- MONOTONIC, and never decremented by the undo. prior_* alone keeps the
-- most recent dismissal, which answers "did this ever happen" forever
-- but collapses a user who dismissed, restored and dismissed again into
-- one event. Same reasoning as value.restatement_count on the ledger,
-- where the capped list holds the detail and the counter holds the
-- truth about how many there were.
ALTER TABLE entity_descriptions
    ADD COLUMN IF NOT EXISTS dismissal_count INTEGER NOT NULL DEFAULT 0;

-- Backfill the counter for rows already carrying a live dismissal, so
-- an existing dismissal is not reported as having happened zero times.
-- Rows whose dismissal was already undone before this migration cannot
-- be recovered: their stamps were overwritten with NULL and no copy
-- exists. Entity 63b6895b is that case and stays at zero, which is the
-- honest value rather than a guess.
UPDATE entity_descriptions
   SET dismissal_count = 1
 WHERE dismissed_at IS NOT NULL
   AND dismissal_count = 0;

-- "Has this path ever run", answerable without reading current state.
-- The question that started this could not be answered from the table
-- at all; now it is an index scan.
CREATE INDEX IF NOT EXISTS idx_entity_descriptions_ever_dismissed
    ON entity_descriptions (user_id, entity_id)
    WHERE dismissal_count > 0;

COMMENT ON COLUMN entity_descriptions.dismissal_count IS
    'How many times a user has dismissed this perception, monotonic and '
    'never decremented by an undo. Counts EVENTS, not state: a dismissal '
    'followed by an undo nets to zero in dismissed_at and is otherwise '
    'indistinguishable from never having happened.';

COMMENT ON COLUMN entity_descriptions.prior_dismissed_note IS
    'The user''s own words from the most recent dismissal, kept when the '
    'dismissal is undone. Before migration 48 the undo overwrote '
    'dismissed_note with NULL and the text was gone permanently.';
