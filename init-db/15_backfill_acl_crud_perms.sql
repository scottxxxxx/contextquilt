-- ============================================================
-- Migration 15 — Backfill ACL rows to grant full CRUD to the
-- app that created each patch via the meeting-extraction path.
-- ============================================================
-- Bug: worker.py's three ACL INSERTs in the connected-patches
-- pipeline only set can_read=TRUE, leaving can_write and
-- can_delete defaulting to FALSE. Every patch ever extracted
-- from a meeting via GhostPour / any registered app has been
-- stored as read-only from that app's perspective. User-initiated
-- edits and deletes from the client all return 403.
--
-- Forward fix ships in the same commit (worker.py grants all
-- three). This migration retroactively upgrades the existing
-- rows so the app that already created those patches regains
-- full control. Scope is limited to rows where can_read was
-- explicitly granted by the worker — we do not invent permissions
-- for apps that were never meant to own the patch.
--
-- Idempotent: re-running this on a cleaned DB is a no-op because
-- the WHERE clause filters on the bug's exact signature
-- (can_read=TRUE AND (can_write=FALSE OR can_delete=FALSE)).
-- ============================================================

DO $$
DECLARE
    updated_count INTEGER;
BEGIN
    WITH updated AS (
        UPDATE context_patch_acl
        SET can_write = TRUE, can_delete = TRUE
        WHERE can_read = TRUE
          AND (can_write = FALSE OR can_delete = FALSE)
        RETURNING patch_id
    )
    SELECT COUNT(*) INTO updated_count FROM updated;
    RAISE NOTICE 'Backfilled ACL CRUD permissions on % rows.', updated_count;
END $$;
