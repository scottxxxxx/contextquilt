-- Alignment Layer, phase 1: the record (design e6ee7ae8, 2026-08-23).
--
-- One object, the alignment event, authored by CQ after a meeting and
-- rendered three ways by the app. The privacy boundary is enforced HERE,
-- in the model, not by prompt discipline: every shared column is
-- project-fact text that passed the language guard in code, and the one
-- private column (private_instruction) is never selected by any shared
-- read. A confirmed direction has no expiry; a proposal lapses.
CREATE TABLE IF NOT EXISTS alignment_events (
    event_id              UUID PRIMARY KEY,
    user_id               TEXT NOT NULL,
    app_id                UUID,
    project_id            TEXT NOT NULL,
    origin_id             TEXT,                 -- source meeting
    origin_type           TEXT,
    topic                 TEXT NOT NULL,        -- clustering key, lowercase slug
    statement             TEXT NOT NULL,        -- one sentence, shared-safe, guarded
    rationale             TEXT,                 -- shared-safe, guarded, may be null
    decision_owner        TEXT,
    implementation_owner  TEXT,
    status                TEXT NOT NULL CHECK (status IN ('proposed','confirmed','corrected','expired')),
    confidence            TEXT NOT NULL CHECK (confidence IN ('high','moderate','emerging')),
    supersedes            UUID[] NOT NULL DEFAULT '{}',   -- prior alignment_events
    superseded_by         UUID,                            -- set when a later event displaces this one
    source_patch_ids      UUID[] NOT NULL DEFAULT '{}',   -- this meeting's decision patches
    superseded_patch_ids  UUID[] NOT NULL DEFAULT '{}',   -- the project decision patches it overrides
    impact                JSONB NOT NULL DEFAULT '[]',    -- derived ImpactLine[]; never free text
    evidence              JSONB NOT NULL DEFAULT '[]',    -- [{origin_id, quote}]; empty = private candidate, never shipped
    shippable             BOOLEAN NOT NULL DEFAULT FALSE, -- evidence non-empty AND guard passed
    proposed_at           TIMESTAMPTZ NOT NULL,
    expires_at            TIMESTAMPTZ,                    -- proposals only
    confirmed_at          TIMESTAMPTZ,
    confirmed_by          TEXT,                           -- the person who confirmed, as the app names them
    confirmation_on_behalf BOOLEAN NOT NULL DEFAULT FALSE, -- admin override, attributed
    correction_reason     TEXT,                           -- status=corrected only
    corrected_by          TEXT,
    private_instruction   TEXT,                           -- PRIVATE: never on a shared read
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_alignment_events_project
    ON alignment_events (user_id, project_id, proposed_at DESC);
CREATE INDEX IF NOT EXISTS idx_alignment_events_origin
    ON alignment_events (user_id, origin_id);
CREATE INDEX IF NOT EXISTS idx_alignment_events_awaiting
    ON alignment_events (user_id, expires_at)
    WHERE status IN ('proposed','corrected') AND confirmed_at IS NULL;
