-- ============================================================
-- 31: person_appearances.capacities
-- ============================================================
--
-- Records HOW a person appeared in a meeting, not just THAT they did.
--
-- Why
-- ---
-- Migration 30 stores one row per (person, meeting) and nothing about the
-- capacity in which they turned up. That makes co-appearance useless as an
-- identity signal: asking "did these two name variants both appear in this
-- meeting" cannot distinguish two people speaking from ONE person whose
-- full name somebody said out loud.
--
-- Measured on prod 2026-08-03. Of the 16 candidate name-variant pairs, the
-- four that co-occur (Sukumar / Sukumar Gurugubelli, Scott / Scott (you),
-- Xhoi / Xhoi (Joy), Pallavi / Pallavi Vijay) all look like the SAME
-- person. A co-occurrence veto without capacity would have blocked every
-- true merge it saw, four for four. With capacity it correctly stays
-- silent on all four, because in no case are both variants speakers.
--
-- The second and larger payoff: the `mentions` tier is currently held back
-- because it inflates meeting counts, a number a user reads as attendance.
-- With capacity recorded, mentions can be stored and the READ filters to
-- attendance, so the consumer decides what "9 meetings" means instead of
-- CQ deciding by withholding data.
--
-- Shape
-- -----
-- An array, not a scalar. One person can be owner AND speaker AND mentioned
-- in a single meeting. A scalar forces strongest-wins, which is exactly
-- what the current rows structurally encode (the backfill's later tiers
-- only add keys earlier tiers missed) and exactly what breaks the gate.
--
-- The primary key stays (user_id, entity_id, origin_id). Widening it to
-- include capacity would multiply rows per person-meeting and break every
-- read that assumes one row each, including meeting_count and the person
-- detail meetings list. An array leaves all of those correct untouched.
--
-- Vocabulary: 'ownership' | 'speaker' | 'mention'.
--
-- Reading it
-- ----------
-- ABSENCE IS NOT EVIDENCE OF ABSENCE. Speaker and mention capacities are
-- derived from retained transcripts, and retention is bounded with no
-- settled MAXLEN on the ingest stream. Rows written before this migration,
-- and rows for meetings whose transcript has aged out, will carry less
-- than the truth. Any veto built on this must fire on positive evidence
-- that BOTH parties spoke, never on the absence of a record.
--
-- Additive and defaulted, so existing rows stay valid and no read that
-- ignores the column changes behavior.

ALTER TABLE person_appearances
    ADD COLUMN IF NOT EXISTS capacities TEXT[] NOT NULL DEFAULT '{}';

COMMENT ON COLUMN person_appearances.capacities IS
    'How this person appeared in this meeting: ownership | speaker | mention. '
    'Empty or partial means unknown, never "not in that capacity". See '
    'the migration 31 header on retention.';

-- Supports the containment predicate the identity gate uses:
--   WHERE capacities @> ARRAY['speaker']
CREATE INDEX IF NOT EXISTS idx_person_appearances_capacities
    ON person_appearances USING GIN (capacities);
