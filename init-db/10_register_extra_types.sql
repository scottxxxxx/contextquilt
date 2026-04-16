-- Register patch types that the extraction pipeline has been emitting but
-- that were never added to patch_type_registry: feature, company, team.
-- These are legitimate named-entity concepts that belong in the patch schema
-- alongside person/project. Idempotent — safe to re-run.

INSERT INTO patch_type_registry (type_key, app_id, display_name, schema, persistence, is_completable, project_scoped) VALUES
    ('feature', NULL, 'Feature', '{"text": "string"}', 'sticky', FALSE, FALSE),
    ('company', NULL, 'Company', '{"text": "string"}', 'sticky', FALSE, FALSE),
    ('team',    NULL, 'Team',    '{"text": "string"}', 'sticky', FALSE, FALSE)
ON CONFLICT (type_key) DO NOTHING;
