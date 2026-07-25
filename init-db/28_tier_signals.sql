-- Tier signals: durable inbox for app-fired account/tier lifecycle
-- events (GP's cq-tier-signals lane; first consumer is
-- event_type='account_deleted', the deletion request for everything
-- CQ holds for a user).
--
-- Design: the endpoint only RECORDS. Processing (the account purge)
-- is a separate consumer keyed on processed_at IS NULL, so a signal
-- that arrives before its processor ships is queued, not lost — the
-- endpoint went live ahead of the purge wiring precisely so app-side
-- senders stop receiving 404s (observed: GP test fire 2026-07-25).

CREATE TABLE IF NOT EXISTS tier_signals (
    signal_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        TEXT NOT NULL,
    app_id         TEXT NOT NULL,
    event_type     TEXT NOT NULL,
    old_tier       TEXT,
    new_tier       TEXT,
    received_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at   TIMESTAMPTZ,
    action         TEXT,
    raw_payload    JSONB
);

CREATE INDEX IF NOT EXISTS idx_tier_signals_unprocessed
    ON tier_signals (received_at)
    WHERE processed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_tier_signals_user
    ON tier_signals (user_id, received_at);
