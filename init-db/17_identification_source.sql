-- Track the upstream identification signal from the calling app's metadata
-- on POST /v1/memory. Lets the dashboard answer: "for this ingestion, did
-- the app know who the user was, and how did it know?" — useful for
-- correlating extraction quality with the source of identity confidence.
--
-- Fields mirror the metadata GhostPour now forwards from ShoulderSurf:
--   user_identified       — bool. Did the upstream app claim the user is
--                           identifiable in this transcript?
--   identification_source — string. How identification was made:
--                           "enrollment" | "user_confirmation" | "none"
--                           (free-form; record whatever the client sent).

ALTER TABLE extraction_metrics ADD COLUMN IF NOT EXISTS user_identified BOOLEAN;
ALTER TABLE extraction_metrics ADD COLUMN IF NOT EXISTS identification_source TEXT;

CREATE INDEX IF NOT EXISTS idx_metrics_identification_source
    ON extraction_metrics(identification_source);
