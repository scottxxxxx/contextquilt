-- ============================================================
-- Migration 16 — Backup audit table
-- ============================================================
-- Records every backup attempt (success and failure) so the
-- admin dashboard can show backup freshness/health and operators
-- can audit DR posture without reaching into GCS directly.
--
-- The cq-backup sidecar inserts a row in 'running' state when a
-- run begins, then updates the same row to 'success' or 'failure'
-- on completion. The dashboard reads the most recent row to
-- decide health status (green if newest 'success' is <26h old).
--
-- Idempotent: CREATE TABLE IF NOT EXISTS guards against re-runs.
-- ============================================================

CREATE TABLE IF NOT EXISTS backup_runs (
  id BIGSERIAL PRIMARY KEY,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ,
  status TEXT NOT NULL CHECK (status IN ('running', 'success', 'failure')),
  gcs_object TEXT,
  size_bytes BIGINT,
  duration_seconds NUMERIC(10, 2),
  error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_backup_runs_completed_status
  ON backup_runs (completed_at DESC, status);

CREATE INDEX IF NOT EXISTS idx_backup_runs_started
  ON backup_runs (started_at DESC);

COMMENT ON TABLE backup_runs IS 'Audit log of pg_dump → GCS backup runs. Written by cq-backup sidecar; read by admin dashboard.';
