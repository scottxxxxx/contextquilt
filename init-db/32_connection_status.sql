-- ============================================================
-- 32: patch_connections.status
-- ============================================================
--
-- Connections can now be archived instead of deleted, the same way
-- patches already are.
--
-- Why
-- ---
-- On 2026-08-03 a cleanup removed 14 `owns` edges that asserted ownership
-- the extraction could not justify. The removal was correct. Two things
-- about it were not:
--
--   1. It was unauditable. A deleted row leaves no trace, so afterwards
--      there was no way to ask the database which edges had gone. The
--      exact set could not be reconstructed.
--   2. It could not be reasoned about later. "Was this edge never
--      created, or created and then removed?" has no answer.
--
-- That is precisely why patches archive rather than delete. Connections
-- were the exception only because this column did not exist.
--
-- What this does NOT fix
-- ----------------------
-- Propagation. An archived edge and a deleted edge look identical to a
-- client, because both simply stop appearing in the payload. A removal
-- reaches the app only when the patch that OWNS the outgoing edge has its
-- `updated_at` bumped, since the delta filters on that and connections
-- are fetched outgoing-only. Archival and the bump are complementary:
-- archival is for us, the bump is for them. Do both.
--
-- Reviving
-- --------
-- The unique constraint is (from_patch_id, to_patch_id, connection_role),
-- which now collides with archived rows. Every insert path therefore
-- upserts `status = 'active'` rather than DO NOTHING. Without that, a
-- legitimately re-emitted edge would hit the archived row, no-op, and
-- stay archived forever, which is a silent and very hard to find failure.
--
-- Reads
-- -----
-- Every production read filters to active. An unfiltered read resurrects
-- archived edges into recall and the quilt payload, which is worse than
-- not having the column at all.
--
-- Deliberately still hard deletes:
--   - account purge (account_purge.py). Deleting means deleting.
--   - merge internals, where an edge becomes structurally invalid because
--     one endpoint no longer exists as a distinct identity.
--
-- Additive and defaulted, so every existing row stays active and any read
-- that ignores the column behaves exactly as before. Nothing is archived
-- by this migration, so no count anywhere should move on the day it
-- ships. If one does, a read filter is wrong.

ALTER TABLE patch_connections
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';

COMMENT ON COLUMN patch_connections.status IS
    'active | archived. Archived edges are retained for audit and are '
    'excluded from every production read. Removing an edge also requires '
    'bumping updated_at on the from-side patch, or the change never '
    'reaches a client. See migration 32.';

-- Every production read is "active edges for these patches", so the
-- partial index matches the access pattern and stays small.
CREATE INDEX IF NOT EXISTS idx_connections_from_active
    ON patch_connections (from_patch_id) WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_connections_to_active
    ON patch_connections (to_patch_id) WHERE status = 'active';
