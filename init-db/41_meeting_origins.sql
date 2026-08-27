-- 41: meeting_origins — the date a meeting actually happened.
--
-- RULED BY SCOTT 2026-08-27, stage 0 of doc 21 (the Memory Layer Spec /
-- Role Evolution sheet).
--
-- WHAT WAS TRUE UNTIL NOW, and why it was not an oversight. CQ persisted
-- no meeting date at all. One arrives at ingest as `payload.timestamp`,
-- is spent building the `Meeting date:` line that lets the model resolve
-- "by Friday" into an ISO date, and is dropped on the floor. Every
-- timestamp that survived was an INGEST clock: `created_at`,
-- `last_seen_at`, `last_observed_at`, the dates on evidence rows. That
-- was deliberate and it was right, because a chart keyed on those is a
-- chart of WHEN THE IMPORTER RAN, and a bulk import draws a cliff where
-- nothing happened. It is why the trajectory lens splits its windows by
-- MEETING SEQUENCE and ships `origin_id` for the client to lay on a time
-- axis, and why doc 19 warns about writing a timestamp at ingest.
--
-- WHAT CHANGES, AND WHAT DOES NOT. This table does not make ingest
-- clocks trustworthy and does not retroactively date anything. It
-- records ONE fact CQ was already told and chose to forget: the day this
-- meeting happened, as the app reported it. Nothing existing reads it.
-- The rule it enables is narrow and stays stated: a surface may bucket
-- by month ONLY from `meeting_date`, never from a `created_at`, and a
-- meeting with no row here HAS NO DATE rather than a guessable one.
--
-- ABSENCE IS HONEST. A row is written only when the app sent a
-- timestamp, so a missing row means "not told", never "undated because
-- it was old". Anything counting meetings must treat a missing row as
-- unknown and say so, the same way an uncounted meeting is an empty slot
-- on the sparkline rather than a zero.
--
-- THE DATE MAY BE CORRECTED. A re-ingest is the same observation
-- arriving twice (doc 19.4), and a backdated import is the app telling
-- us the date it should have had. So a later non-null timestamp updates
-- the date, `first_seen_at` never moves, and `updated_at` records that
-- it happened. A re-ingest that sends no timestamp never erases one.
--
-- NO app_id COLUMN, by doc 18: each app has its own subject space and
-- there are no per-app read filters. The key is (user_id, origin_id),
-- which is how every other origin-scoped surface addresses a meeting.
CREATE TABLE IF NOT EXISTS meeting_origins (
    user_id        TEXT NOT NULL,
    origin_id      TEXT NOT NULL,
    origin_type    TEXT,
    -- The day the meeting happened, in the app's own reckoning. A DATE
    -- and not a timestamptz on purpose: what the app reports is a
    -- calendar day in the user's life, the bucket boundary the spec asks
    -- for is a calendar month, and carrying a time of day would invite
    -- somebody to do timezone arithmetic on a number that never had that
    -- precision.
    meeting_date   DATE NOT NULL,
    first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, origin_id)
);

-- The only access pattern that exists yet: every dated meeting for one
-- person's window, oldest first.
CREATE INDEX IF NOT EXISTS idx_meeting_origins_user_date
    ON meeting_origins (user_id, meeting_date);

COMMENT ON TABLE meeting_origins IS
    'The day a meeting happened, as the app reported it at ingest. The '
    'ONLY non-ingest clock in the database. A missing row means CQ was '
    'never told, never that the meeting is undated. Bucket by month from '
    'here and from nowhere else.';
