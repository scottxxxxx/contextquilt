-- Extend extraction_metrics into a full ingestion audit log.
-- Adds pipeline-trace fields so the dashboard can show what came in,
-- what was extracted, and what was filtered — per request.

ALTER TABLE extraction_metrics ADD COLUMN IF NOT EXISTS app_id TEXT;
ALTER TABLE extraction_metrics ADD COLUMN IF NOT EXISTS meeting_id TEXT;
ALTER TABLE extraction_metrics ADD COLUMN IF NOT EXISTS interaction_type TEXT DEFAULT 'meeting_summary';
ALTER TABLE extraction_metrics ADD COLUMN IF NOT EXISTS owner_speaker_label TEXT;
ALTER TABLE extraction_metrics ADD COLUMN IF NOT EXISTS owner_marker_present BOOLEAN;
ALTER TABLE extraction_metrics ADD COLUMN IF NOT EXISTS owner_gate_filtered INTEGER DEFAULT 0;
ALTER TABLE extraction_metrics ADD COLUMN IF NOT EXISTS connection_dropped INTEGER DEFAULT 0;
ALTER TABLE extraction_metrics ADD COLUMN IF NOT EXISTS patches_before_filters INTEGER;
ALTER TABLE extraction_metrics ADD COLUMN IF NOT EXISTS patches_after_filters INTEGER;
ALTER TABLE extraction_metrics ADD COLUMN IF NOT EXISTS reasoning_chars INTEGER;
ALTER TABLE extraction_metrics ADD COLUMN IF NOT EXISTS transcript_chars INTEGER;

CREATE INDEX IF NOT EXISTS idx_metrics_app ON extraction_metrics(app_id);
CREATE INDEX IF NOT EXISTS idx_metrics_meeting ON extraction_metrics(meeting_id);
CREATE INDEX IF NOT EXISTS idx_metrics_interaction ON extraction_metrics(interaction_type);
