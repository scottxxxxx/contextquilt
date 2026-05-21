-- Migration 21: critical-failure alerting.
--
-- Operator-facing email alerts for system-level outages, distinct from
-- per-user query failures. Mirrors GhostPour's alerting schema
-- (cloudzap PR #194) so the two stacks describe the same thing and a
-- future cross-stack dashboard view is possible. SQL dialect adapted
-- from GP's SQLite (TEXT timestamps, INTEGER booleans, TEXT JSON) to
-- CQ's Postgres (TIMESTAMPTZ, BOOLEAN, JSONB, UUID PKs).

-- Recipient list. One row per opted-in operator address. `categories`
-- is a JSON array of category strings the recipient wants notifications
-- for; NULL or empty array means "all categories".
CREATE TABLE IF NOT EXISTS alert_recipients (
    id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    email         TEXT         NOT NULL UNIQUE,
    display_name  TEXT,
    active        BOOLEAN      NOT NULL DEFAULT TRUE,
    categories    JSONB,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Open-incident state plus immutable history. One row per
-- (category, subject) fingerprint while the incident is open
-- (resolved_at IS NULL); resolved rows accumulate as history. The
-- service auto-resolves opens that have gone quiet for 30 minutes
-- so the next failure with the same fingerprint opens a fresh row
-- and re-fires email.
CREATE TABLE IF NOT EXISTS alert_incidents (
    id                  UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    category            TEXT         NOT NULL,
    subject             TEXT         NOT NULL,
    fingerprint         TEXT         NOT NULL,
    first_seen_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_seen_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    trigger_count       INTEGER      NOT NULL DEFAULT 1,
    details_json        JSONB,
    email_sent_at       TIMESTAMPTZ,
    emailed_recipients  JSONB,
    resolved_at         TIMESTAMPTZ
);

-- Partial unique index doubles as the dedup mechanism. asyncpg's
-- INSERT can use ON CONFLICT (fingerprint) WHERE resolved_at IS NULL
-- DO NOTHING to handle concurrent report_incident races cleanly
-- (the existing-incident SELECT happens before INSERT, but two
-- requests racing past the SELECT both try to INSERT — the
-- partial unique catches the second and the caller treats it as
-- "incident already open" via a refetch).
CREATE UNIQUE INDEX IF NOT EXISTS idx_alert_incidents_open
    ON alert_incidents(fingerprint)
    WHERE resolved_at IS NULL;

-- Backs the dashboard history view (ORDER BY first_seen_at DESC).
CREATE INDEX IF NOT EXISTS idx_alert_incidents_created
    ON alert_incidents(first_seen_at DESC);
