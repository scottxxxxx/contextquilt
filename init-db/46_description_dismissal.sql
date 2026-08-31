-- A meeting's perception of a person can be WRONG, and until now there
-- was no way to say so.
--
-- The case that forced it (2026-08-31): a person the meetings described
-- as "an immigration attorney exploring automation of case intake" is
-- not an attorney at all. His STATED role already won the `title`
-- precedence rule and the card correctly showed "Stated, not inferred".
-- But the description series is served in full underneath it, and
-- `who_they_are` synthesises across both, so the summary still opened
-- with "Early meetings cast this person as an immigration attorney".
-- The row was unreachable: no status column, no API write path, and
-- chat corrections operate on context_patches and never arrive here.
--
-- NOT A DELETE, and the distinction is the whole design. That meeting
-- genuinely did describe him that way, and the row is a true record of
-- what was said. What is wrong is treating it as true OF THE PERSON.
-- Deleting it would destroy an accurate observation and, worse, make
-- "the user said this was wrong" indistinguishable from "this was never
-- observed" -- the same argument that shaped `shelve`, where a tombstone
-- would have been indistinguishable from decay.
--
-- So the row stays and carries its dismissal. The read side excludes it,
-- and because `who_they_are` regenerates whenever its input fingerprint
-- changes, the summary rewrites itself without the bad premise rather
-- than needing new synthesis logic.

ALTER TABLE entity_descriptions
    ADD COLUMN IF NOT EXISTS dismissed_at TIMESTAMP WITH TIME ZONE;

-- Which lane said so, on the same open vocabulary as `source`:
-- user_chat, user_card, correction. Ships the count AND the cause, so a
-- year from now "the user dismissed this from the card" and "a chat
-- correction superseded it" are still distinguishable.
ALTER TABLE entity_descriptions
    ADD COLUMN IF NOT EXISTS dismissed_source TEXT;

-- The user's own words when they corrected rather than merely dismissed.
-- Nullable: "this is inaccurate" carries no text, "correct this" does,
-- and the difference between them is worth keeping.
ALTER TABLE entity_descriptions
    ADD COLUMN IF NOT EXISTS dismissed_note TEXT;

-- The declared patch that replaced this perception, when the correction
-- produced one. The `replaces` edge already links patch to patch; this
-- is the same link across the type boundary, so a reader can get from a
-- dismissed description to the fact that superseded it.
ALTER TABLE entity_descriptions
    ADD COLUMN IF NOT EXISTS superseded_by_patch_id UUID;

-- Every read of the series filters on this, so it is the whole access
-- pattern rather than a reporting convenience.
CREATE INDEX IF NOT EXISTS idx_entity_descriptions_live
    ON entity_descriptions (user_id, entity_id, first_observed_at DESC)
    WHERE dismissed_at IS NULL;

COMMENT ON COLUMN entity_descriptions.dismissed_at IS
    'When the user said this perception was wrong. The row is retained: '
    'the meeting did say it, and a tombstone would make a dismissal '
    'indistinguishable from never having been observed.';
