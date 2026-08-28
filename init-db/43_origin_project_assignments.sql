-- 43: the user's project decision for a meeting, kept as its own fact.
--
-- WHAT WENT WRONG, on Scott's own data, 2026-08-28. He recorded a meeting
-- with no project, assigned it to one when the meeting ended, and asked
-- whether everything had followed. It had not. The worker log:
--
--   15:26:38  main extraction stored its patches
--   15:26:50  his assign-project ran, 200 OK, patches_updated = 3
--   15:26:58  the behaviour extraction stored 12 MORE patches
--
-- An ingest is multi-phase (main extraction, then the communication
-- profile, then behaviour observations) and takes about twenty seconds.
-- The rescope could only move what existed when it ran, nothing re-ran
-- it, and the twelve patches written eight seconds later stayed unscoped
-- permanently. That is not an edge case: assigning a project as soon as
-- the meeting ends is the workflow, so the assignment lands inside that
-- window nearly every time.
--
-- WHY A TABLE AND NOT A HEURISTIC. The obvious cheap fix is to have a
-- late-arriving write copy the project off a patch that already has one.
-- That closes the observed race and still leaves a hole: an assignment
-- made BEFORE any phase has committed has nothing to copy from, updates
-- zero rows, and is silently forgotten. Scott's question was whether we
-- can be SURE, and a rule that works whenever the timing happens to
-- cooperate is not sure. So the decision itself is stored, once, and the
-- ingest reads it.
--
-- THREE STATES, and the third is the reason project_id is nullable:
--   no row            the user has never said anything about this origin
--   row, project set  assigned
--   row, project NULL EXPLICITLY unassigned
-- The third must not be collapsed into the first. Without it, a user who
-- removes a project mid-ingest would have it silently restored by the
-- next phase adopting the project still sitting on the earlier patches,
-- which is the same bug in the opposite direction.
--
-- NO app_id column, per doc 18.
CREATE TABLE IF NOT EXISTS origin_project_assignments (
    user_id      TEXT NOT NULL,
    origin_id    TEXT NOT NULL,
    origin_type  TEXT NOT NULL DEFAULT 'meeting',
    -- NULL means explicitly unassigned. Absence of the ROW means never
    -- stated. See above; these are not the same fact.
    project_id   TEXT,
    project      TEXT,
    assigned_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, origin_id, origin_type)
);

COMMENT ON TABLE origin_project_assignments IS
    'What the user decided this meeting belongs to, stored so an ingest '
    'phase that finishes after the decision can still honour it. NULL '
    'project_id means explicitly unassigned; no row means never stated.';
