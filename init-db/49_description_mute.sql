-- "Do not ask again": a person whose inferred descriptions keep coming
-- back after the user has said they are wrong.
--
-- THE GAP THIS CLOSES. A dismissal (migration 46) marks the perceptions
-- that exist TODAY. The write path keeps observing, deliberately: the
-- meetings really do describe the person, and suppressing the
-- observation is a bigger claim than correcting the record. So a
-- reworded perception CONFIRMS the dismissed row and stays hidden
-- (`described_as.classify_observation` compares by trigram), but a
-- genuinely different sentence appends a fresh LIVE row, and
-- `entities.description` is overwritten by every meeting that describes
-- the person at all. The user who said "this is wrong" sees a new
-- inferred description on the card next week and has to say it again.
--
-- Scott, 2026-09-14, asked for the obvious affordance: a way to say it
-- once. This is the column behind it.
--
-- THE MUTE IS NOT A NEW SUPPRESSION MECHANISM, and that is the whole
-- design. A second mechanism would mean two rules about what the card
-- shows, on five read sites (recall's entity header, the person list,
-- the person detail's frozen sentence, its series, and the
-- who_they_are inputs), each of which already filters on
-- `dismissed_at IS NULL`. Five carriers of one rule is the shape this
-- codebase keeps paying for.
--
-- Instead the mute changes the WRITE path, in two places:
--
--   1. a new observation for a muted entity is inserted ALREADY
--      DISMISSED, stamped `dismissed_source = 'mute'` so a year from now
--      "the user dismissed this one" and "this arrived under a mute" are
--      still distinguishable (migration 46's own rule about carrying the
--      cause), and
--   2. `entities.description` stops being overwritten while muted, so
--      the frozen column keeps the text the read side already knows to
--      suppress.
--
-- Every existing reader is then correct with no change at all.
--
-- THE ROW IS STILL WRITTEN. The meeting did describe them that way, and
-- the record of what was said is the thing this table exists for. A mute
-- hides, it does not blind: unmute and the history is all there,
-- including everything observed while it was on.

ALTER TABLE entities
    ADD COLUMN IF NOT EXISTS descriptions_muted_at TIMESTAMP WITH TIME ZONE;

-- Which affordance said so, open vocabulary alongside `dismissed_source`:
-- user_card, user_chat, admin.
ALTER TABLE entities
    ADD COLUMN IF NOT EXISTS descriptions_muted_source TEXT;

-- The write path checks this per entity on every described-as
-- observation, so it is a lookup on the ingest path and wants the index.
CREATE INDEX IF NOT EXISTS idx_entities_descriptions_muted
    ON entities (user_id, entity_id)
    WHERE descriptions_muted_at IS NOT NULL;

COMMENT ON COLUMN entities.descriptions_muted_at IS
    'When the user said to stop bringing inferred descriptions back for '
    'this person. Observations are still recorded; they arrive already '
    'dismissed with dismissed_source = ''mute'', and entities.description '
    'stops being overwritten. Cleared by the undismiss route.';
