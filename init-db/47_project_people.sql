-- Explicit project membership: the user SAYING who is on a project.
--
-- Until now CQ only ever INFERRED who was on a project, from speaker
-- labels and from ownership inside meetings. Someone genuinely relevant
-- who has never been in a recorded meeting was invisible. ShoulderSurf
-- reached for that with a "Contact" project note, which stored a person
-- as prose inside a project: no entity, no aliases, no appearances, no
-- insights, no ledger, no `owed_to`, and no way to correct, merge or
-- rename them. Scott retired that note type on 2026-08-31 and this is
-- the replacement.
--
-- WHY NOT THE EXISTING `works_on` EDGE, which was the obvious answer and
-- the one I started with. It connects a person PATCH to a project PATCH,
-- and measured on live data that cannot express membership keyed on a
-- project id: 140 of 160 project rows have no project patch at all, 329
-- project patches carry no project_id, and 315 of the 357 existing
-- works_on edges point at a project patch that cannot be resolved back
-- to a project row. It is an extraction-time artifact between patches,
-- not a statement about the projects the app knows.
--
-- KEYED ON entity_id, NOT ON A PERSON PATCH. A person holds several
-- patches, one per surface form the extractor has used, and which one is
-- "current" changes as they are rephrased. Insights keyed on a patch id
-- went unreachable for exactly that reason (2026-08-16). The entity is
-- the identity that does not move.
--
-- KEYED ON project_id, the stable id the app holds, because a name is
-- what caused the incident this table was designed during: four of this
-- user's projects normalise to "cbe" and one carries a double space.
--
-- ROWS ARE RETAINED, NOT DELETED. Removing a person from a project is a
-- statement ("they are not on this after all"), and it must stay
-- distinguishable from never having been added. Same argument as
-- `shelve` and as description dismissal.

CREATE TABLE IF NOT EXISTS project_people (
    user_id       TEXT        NOT NULL,
    project_id    TEXT        NOT NULL,
    entity_id     UUID        NOT NULL,
    added_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    added_source  TEXT,
    removed_at    TIMESTAMPTZ,
    removed_source TEXT,
    PRIMARY KEY (user_id, project_id, entity_id)
);

-- The two access patterns, both filtered to live rows, which is the
-- whole of how this table is read.
CREATE INDEX IF NOT EXISTS idx_project_people_project
    ON project_people (user_id, project_id) WHERE removed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_project_people_entity
    ON project_people (user_id, entity_id) WHERE removed_at IS NULL;
