# Incident — origin_id data loss during v1 migration

**Date:** 2026-04-19
**Severity:** High (user-visible data loss; partial recovery only)
**Status:** Resolved — partial recovery applied; 93 patches permanently unrecoverable
**Owner:** Scott / Claude

## Summary

Migration `init-db/10_app_schema_registration.sql` renamed `context_patches.meeting_id` to the unified `origin_id` + `origin_type` columns as part of the v1 schema rollout. The migration added the new columns and dropped `meeting_id` without backfilling. As a result, every pre-v1 patch lost its link to the meeting it came from.

ShoulderSurf's iOS client groups patches into a "Meetings" view by `origin_id` (with fallback to the deprecated `meeting_id`). With both columns NULL on historical rows, the Meetings tab rendered empty for the sole production user until recovery.

## Impact

- **Affected user:** `fa4d903c-24c0-45d5-9fdb-b5496e32501b` (sole prod user at the time)
- **Affected rows:** 243 project-scoped active patches with `origin_id IS NULL`
- **Recovered:** 150 patches (from 19 distinct GhostPour meetings)
- **Unrecoverable:** 93 patches (predate GhostPour's `meeting_transcripts` logging, which began ~2026-04-10)
- **User-visible effect:** the SS Meetings tab was empty; universal ("About You") patches were unaffected

## Timeline

- **Pre-v1:** `context_patches.meeting_id` populated by the extraction worker for every meeting-sourced patch.
- **2026-04-18:** v1 schema design finalizes `origin_type` + `origin_id` as the unified origin pointer.
- **2026-04-19 (early):** Migration 10 written. A backfill step (`UPDATE ... SET origin_id = meeting_id, origin_type = 'meeting' WHERE meeting_id IS NOT NULL`) was drafted, then removed at the operator's instruction that pre-launch data was disposable and backward-compat wasn't a concern.
- **2026-04-19 (deploy):** Migration applied to prod. `meeting_id` column dropped. 243 active project-scoped patches now have `origin_id = NULL`.
- **2026-04-19 (later):** Scott opens the SS app; Meetings tab is empty. Logs show `no patches with meeting_id found`.
- **2026-04-19 (recovery):** GhostPour's `meeting_transcripts` table exported and used to match patch `created_at` timestamps to meeting timestamps within a 6h window. 150 patches recovered; 93 predate GP logging and are permanently lost.

## Root cause

**Primary:** the backfill was dropped from migration 10 based on an incomplete read of backward-compatibility scope. "No backward compat" was taken to mean we could drop old column data, but it should only have applied to API/schema shape — not to user data that the SS client was actively depending on for UX grouping.

**Contributing:** we did not enumerate downstream readers of `meeting_id` before dropping it. The SS client's `sourceMeetingId` accessor (`QuiltPatch.swift`) reads `originId` first and falls back to `meetingId`, so dropping one without populating the other silently broke the fallback chain.

## Recovery

`scripts/recover_origin_id_from_gp.py` reads an exported `meetings.json` from GhostPour (`meeting_id`, `created_at` per meeting for the target user) and, for each patch with `origin_id IS NULL` and a project-scoped `patch_type`, assigns `origin_id` to the latest GP meeting whose `created_at <= patch.created_at` within a 6-hour window. `origin_type` is set to `'meeting'`.

Scope limited to project-scoped types: `decision`, `commitment`, `blocker`, `takeaway`, `goal`, `constraint`, `event`, `role`, `project`. Universal types (`trait`, `preference`) don't carry an origin and were correctly left alone.

Dry-run → review match/unmatched distribution → apply, inside the contextquilt container with `DATABASE_URL` set.

**Result:** 150 patches updated (19 meetings represented). 93 patches remain `origin_id IS NULL` because their `created_at` predates GP's meeting_transcripts table (first entry ~2026-04-10). These are not recoverable from any known source.

## Lessons

1. **"No backward compat" applies to interfaces, not data.** Schema renames that drop user data need an explicit data-migration plan, even when the API contract is allowed to break.
2. **Enumerate downstream readers before dropping columns.** Before the next column drop, grep every client repo for the column name and confirm either (a) no one reads it or (b) the new column is populated first.
3. **Backfill in the same migration that drops the old column.** If we had written `UPDATE ... SET origin_id = meeting_id WHERE meeting_id IS NOT NULL` before `ALTER TABLE ... DROP COLUMN meeting_id`, nothing would have been lost.
4. **Shadow tables in sibling services are load-bearing.** GhostPour's `meeting_transcripts` turned out to be the only surviving source of truth for meeting↔patch association after this migration. Treat sibling-service data as a last line of defense, not a primary one.

## Follow-ups

- [x] Apply recovery script to prod (done — 150 recovered)
- [x] Persist recovery script at `scripts/recover_origin_id_from_gp.py`
- [x] Document this incident
- [ ] Add a CI check that flags `ALTER TABLE ... DROP COLUMN` in migrations without a paired `UPDATE` or comment justifying it
- [ ] Before next schema rename: grep ShoulderSurf + GhostPour + any future clients for the column name
