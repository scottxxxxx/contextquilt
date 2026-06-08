-- Provider health probe history.
--
-- The provider_health_loop in src/worker.py probes Anthropic and
-- OpenRouter every 15 minutes and inserts one row per probe per
-- provider here. The dashboard's "Providers" surface reads the latest
-- row per provider; consecutive-failure detection for alerts reads the
-- last N rows.
--
-- Why a table and not just a Redis key:
--   * History matters. The future Providers tab should chart the last
--     24h of latency / status transitions.
--   * Consecutive-failure detection is cleaner against a table: the
--     loop queries "last N rows for provider X" and decides whether
--     to fire report_incident.
--   * Probes are infrequent (4 per provider per hour), so row volume
--     is tiny: ~200 rows/day across both providers, well under a
--     million per year even if we never prune.
--
-- Why no FK to providers/apps:
--   The provider name is a free-text enum-by-convention. CQ today
--   talks to two providers (anthropic, openrouter); a new provider is
--   a code change anyway, so a CHECK constraint here is overkill.

CREATE TABLE IF NOT EXISTS provider_health_probes (
    probe_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider         text NOT NULL,
    probed_at        timestamptz NOT NULL DEFAULT NOW(),
    status           text NOT NULL,
    latency_ms       integer,
    balance_usd      numeric(10, 4),  -- OpenRouter only; null on Anthropic probes
    limit_usd        numeric(10, 4),  -- OpenRouter only; null = pay-as-you-go
    is_free_tier     boolean,         -- OpenRouter only
    error_message    text             -- null on status='active'
);

-- The dominant access pattern is "latest row per provider", which
-- gives us the dashboard tile shape and the consecutive-failure
-- lookback for alerts. DESC on probed_at lets the queries use a
-- bog-standard ORDER BY ... LIMIT 1 / LIMIT N.
CREATE INDEX IF NOT EXISTS idx_provider_health_probes_latest
    ON provider_health_probes (provider, probed_at DESC);
