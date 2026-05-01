-- Track the upstream subscription tier on every ingestion.
--
-- The calling app (today: GhostPour) forwards subscription_tier on
-- every /v1/memory write. If the user crossed a tier boundary inside
-- the previous 24h, previous_tier is set so we can detect upgrade /
-- downgrade transitions from the audit stream alone (without a
-- separate webhook event).
--
-- Free / Plus / Pro / Trial / Admin are the documented values today,
-- but stored as TEXT to keep new tiers cheap to add.
--
-- The index on subscription_tier supports cost-by-tier dashboards —
-- answering questions like "how much are we paying for Free user
-- extractions" — which is the immediate ask driving this column.

ALTER TABLE extraction_metrics ADD COLUMN IF NOT EXISTS subscription_tier TEXT;
ALTER TABLE extraction_metrics ADD COLUMN IF NOT EXISTS previous_tier TEXT;

CREATE INDEX IF NOT EXISTS idx_metrics_subscription_tier
    ON extraction_metrics(subscription_tier);
