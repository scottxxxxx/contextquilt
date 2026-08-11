-- Migration 34: per-appearance turn counts (12a capture-side signals).
--
-- The design-12a audit's sharpest finding: transcripts are not retained,
-- so any signal not captured at ingest is lost forever. Turn counts are
-- the raw material for both the "what moves him" briefing lens and the
-- capacity-gate turn-count refinement the Vijay merge proved right
-- (41 turns vs 1 turn in one meeting = one human with a truncated
-- label, not two people).
--
-- NULL means UNKNOWN (the row predates the metric, or the person was
-- not a speaker in this meeting), never "spoke zero turns". The same
-- empty-means-unknown rule capacities established (migration 31).
-- There is deliberately NO backfill: the transcripts are gone, and a
-- guessed number would be worse than an honest null.

ALTER TABLE person_appearances ADD COLUMN IF NOT EXISTS turn_count INTEGER;
