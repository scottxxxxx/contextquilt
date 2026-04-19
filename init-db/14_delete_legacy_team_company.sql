-- ============================================================
-- Migration 14 — Delete legacy team/company patches
-- ============================================================
-- A handful of pre-v1 extractions produced patches with patch_type
-- values 'team' and 'company' — neither of which is a registered
-- patch type under the v1 taxonomy. `company` is an *entity* type
-- (graph-index only, never a user-editable patch); `team` was never
-- canonically registered anywhere.
--
-- These rows survived migration 10's cleanup because that migration
-- only targeted identity/experience/feature/deadline. Once the SS
-- client decoded a quilt response containing any of these types, a
-- strict JSON decoder throw would bail out the entire response and
-- the user would see zero patches.
--
-- Paired with an SS-side fix: add a fallback `.unknown(String)` case
-- so no future type addition causes the same cliff.
--
-- This migration is idempotent: re-running it on a cleaned DB is
-- a no-op (DELETE matches zero rows).
-- ============================================================

DO $$
DECLARE
    deleted_patches INTEGER;
BEGIN
    WITH deleted AS (
        DELETE FROM context_patches
        WHERE patch_type IN ('team', 'company')
        RETURNING patch_id
    )
    SELECT COUNT(*) INTO deleted_patches FROM deleted;
    RAISE NOTICE 'Deleted % legacy patches (team, company).', deleted_patches;
END $$;
