-- Extraction metrics tracking for cost and performance monitoring
-- Populated by the cold path worker after each LLM extraction call

CREATE TABLE IF NOT EXISTS extraction_metrics (
    metric_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd FLOAT,
    latency_ms FLOAT,
    patches_extracted INTEGER,
    entities_extracted INTEGER,
    source_prompt TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_metrics_created ON extraction_metrics(created_at);
CREATE INDEX IF NOT EXISTS idx_metrics_model ON extraction_metrics(model);
CREATE INDEX IF NOT EXISTS idx_metrics_user ON extraction_metrics(user_id);
