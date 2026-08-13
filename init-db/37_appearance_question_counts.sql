-- Migration 37: per-appearance question counts (12a capture-side signals,
-- the sibling of migration 34's turn_count).
--
-- CQ can already say what a person owes and how their items closed. It
-- cannot say who the user actually presses for an answer, and the
-- interesting question about a run of meetings is whether those two line
-- up. That signal exists only in the transcript, and transcripts are
-- derive-then-discard, so every meeting that lands before this column
-- exists is permanently unmeasurable. Same constraint that put turn_count
-- here, and the same reason not to wait for the surface that reads it.
--
-- The two attribution grades are stored SEPARATELY and must never be
-- summed by a reader:
--
--   * explicit  = the question named its addressee as a vocative
--                 ("Marcus, can you get me that?"). High confidence.
--   * inferred  = the question ended a turn and somebody else spoke
--                 next, so they are taken to be the addressee. A
--                 heuristic that is wrong sometimes, kept in its own
--                 column so a client can trust the explicit one alone.
--                 Blend them once and nobody can un-blend them later.
--
-- `meeting_questions_by_user` is the denominator that travels with the
-- from_user counts: two questions out of three asked all meeting and two
-- out of forty are not the same observation, and a count served without
-- its denominator invites a ratio nobody can refuse to render.
--
-- NULL means UNKNOWN everywhere here (the row predates the metric, the
-- ingest carried no transcript, or no speaker label could be identified
-- as the user), never "was asked zero questions". Migration 31's rule.
-- There is deliberately NO backfill: the transcripts are gone.

ALTER TABLE person_appearances
    ADD COLUMN IF NOT EXISTS questions_asked INTEGER,
    ADD COLUMN IF NOT EXISTS questions_received_explicit INTEGER,
    ADD COLUMN IF NOT EXISTS questions_received_inferred INTEGER,
    ADD COLUMN IF NOT EXISTS questions_from_user_explicit INTEGER,
    ADD COLUMN IF NOT EXISTS questions_from_user_inferred INTEGER,
    ADD COLUMN IF NOT EXISTS meeting_questions_by_user INTEGER;
