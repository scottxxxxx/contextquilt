-- Client-minted idempotency key for hand-authored patches (SS action items).
--
-- WHY A COLUMN AND A UNIQUE INDEX RATHER THAN A LOOKUP-THEN-INSERT.
-- The failure this exists to stop is a user tapping Add, seeing nothing
-- because the network stalled, and tapping again. That is two requests
-- IN FLIGHT AT ONCE, so a SELECT followed by an INSERT has a window
-- between them where both find nothing and both insert. The window is
-- exactly the condition the feature is for, so closing it in application
-- code would be closing it everywhere except where it happens. The
-- unique index makes the second insert impossible rather than unlikely.
--
-- Nullable and unconstrained when absent: every patch the extractor
-- writes has no client_id, and a partial index leaves those rows alone.
--
-- GLOBAL uniqueness, not per subject. The value is a client-minted UUID,
-- so a collision across users is a client bug rather than a legitimate
-- state, and the create path checks that the row it found belongs to the
-- caller's subject before returning it. Scoping the index to the subject
-- would need a join the index cannot express (subjects live in
-- patch_subjects), and would silently hand back somebody else's row on a
-- collision instead of refusing.
ALTER TABLE context_patches ADD COLUMN IF NOT EXISTS client_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_context_patches_client_id
    ON context_patches (client_id)
    WHERE client_id IS NOT NULL;
