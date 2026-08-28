-- 44: what the profile pass has already TRIED and failed, so it stops
-- paying for the same failure and stops promising the user a card that
-- is not coming.
--
-- FOUND BY WATCHING THE FIRST CYCLE after #341 rather than by reading
-- the code that had just been written. #341 argued the catch-up work was
-- bounded, "about 240 calls once", because a card is only written for a
-- person on a lens they do not already have. That is true of SUCCESSES
-- only. A decline logs at debug and returns; a rejected card writes
-- nothing either. The idempotency gate counts lens STAMPS, so it never
-- sees a failure, and those people are retried EVERY CYCLE FOREVER.
-- Raising the budget therefore raised a recurring cost while the commit
-- message claimed it was a one-time one.
--
-- It is not hypothetical: Ragu failed `stated_role_dropped` and Pallavi
-- Kandanu failed `opens_with_name` on every cycle across 08-26, 08-27
-- and 08-28, the same defect each time, and neither has a card. The
-- in-cycle retry already runs once and the model makes the same mistake
-- twice.
--
-- AND IT IS WORSE THAN A COST BUG, which ShoulderSurf found by asking
-- whether a decline is visible to them. It is: such a person is served
-- `pending_pattern`, which doc 16 defines as "re-checked each cycle"
-- with waiting-helps TRUE, and the client renders "Nothing stands out
-- yet. This fills in as more comes in about {name}." For these people
-- that invites an action that cannot work, forever. A card that is
-- merely empty costs nothing; one that asks for effort in exchange for
-- nothing is different in kind. Scott ruled both halves: stop retrying,
-- and stop promising.
--
-- WHY THE RETRY RULE IS EVIDENCE, NOT A TIMER. A person who fails on a
-- given corpus will fail on that same corpus tomorrow; nothing about the
-- clock changes the input. So the row records how much evidence existed
-- at the last attempt, counted in source patches, and the pass tries
-- again only when that has GROWN.
-- Same instinct as the who_they_are fingerprint gate: spend a call when
-- the inputs move, never on a schedule.
--
-- WHY `lens` CAN HOLD A SENTINEL. A REJECTION knows its lens, because
-- the model already chose one and the parse then refused the card. A
-- DECLINE does not: the model looked at the person and produced nothing
-- at all, so there is no lens to attribute it to. That is recorded
-- against the pass instead, and the readiness surface reads it as "every
-- lens still pending for this person is stalled", which is what a
-- decline actually means.
--
-- NO app_id column, per doc 18.
CREATE TABLE IF NOT EXISTS person_lens_attempts (
    user_id              TEXT NOT NULL,
    entity_id            UUID NOT NULL,
    -- A lens id, or the pass sentinel when the model declined without
    -- naming one. See above.
    lens                 TEXT NOT NULL,
    attempts             INTEGER NOT NULL DEFAULT 0,
    -- How much evidence existed when we last tried. The retry gate
    -- compares against this rather than against a clock.
    --
    -- It counts SOURCE PATCHES, not meetings, and the name says so. The
    -- first draft called it meetings_at_attempt while storing a patch
    -- count, which is the same defect three of us spent 2026-08-27 and
    -- 08-28 finding in other people's code: a name that does not hold
    -- what it says. Patches are also the better measure here, because
    -- they are what the pass actually reads, and a meeting that adds no
    -- observations about this person is not new evidence about them.
    evidence_at_attempt  INTEGER,
    -- 'declined' (the model produced nothing) or 'rejected' (it produced
    -- something the parse refused). Kept apart because they are
    -- different failures and only one of them is a format problem.
    last_outcome         TEXT,
    -- The parse defect, for diagnosis. INTERNAL: never served. A defect
    -- name is a fact about our own prompt, not about the colleague, and
    -- nothing on the person surface should render it.
    last_defect          TEXT,
    last_attempt_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, entity_id, lens)
);

CREATE INDEX IF NOT EXISTS idx_person_lens_attempts_user
    ON person_lens_attempts (user_id, entity_id);

COMMENT ON TABLE person_lens_attempts IS
    'Failed profile-pass attempts per person per lens, so the pass stops '
    'retrying a deterministic failure every cycle and the readiness '
    'surface can stop promising a card that is not coming. Retry is '
    'gated on evidence growing, never on elapsed time.';
