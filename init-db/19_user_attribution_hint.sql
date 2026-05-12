-- Track the inbound user-attribution soft signal on every extraction.
--
-- The calling app's identity layer (today: ShoulderSurf via
-- GhostPour) optionally forwards a `user_attribution_hint` object
-- on `/v1/capture-transcript` (i.e. metadata on /v1/memory) when no
-- hard `user_identified=True` claim is available. This is the
-- pre-calibration soft-signal path that replaces the post-meeting
-- attribution sheet for the in-chat-nudge feature.
--
-- v1 wire shape (per the SS spec ack'd 2026-05-12):
--   {
--     "speaker_label": "Speaker 3",
--     "confidence": 0.42,
--     "confidence_basis": "combined",
--     "secondary_candidate": { "speaker_label": "Speaker 5",
--                              "confidence": 0.31 }
--   }
--
-- Columns are split flat (rather than JSONB) so calibration queries
-- against confidence + basis run on indexes. Secondary candidate
-- captured for logging only in PR A — primary gating uses only the
-- top hint.
--
-- The composite index on (basis, confidence) supports the threshold-
-- calibration queries SS will want once they've collected a corpus,
-- e.g. "what fraction of `combined` hints in [0.40, 0.70) ended up
-- corroborated by a later hard identification?"

ALTER TABLE extraction_metrics
    ADD COLUMN IF NOT EXISTS attribution_hint_speaker_label TEXT;
ALTER TABLE extraction_metrics
    ADD COLUMN IF NOT EXISTS attribution_hint_confidence DOUBLE PRECISION;
ALTER TABLE extraction_metrics
    ADD COLUMN IF NOT EXISTS attribution_hint_basis TEXT;
ALTER TABLE extraction_metrics
    ADD COLUMN IF NOT EXISTS attribution_hint_secondary_label TEXT;
ALTER TABLE extraction_metrics
    ADD COLUMN IF NOT EXISTS attribution_hint_secondary_confidence DOUBLE PRECISION;

CREATE INDEX IF NOT EXISTS idx_metrics_attribution_hint
    ON extraction_metrics(attribution_hint_basis, attribution_hint_confidence)
    WHERE attribution_hint_confidence IS NOT NULL;
