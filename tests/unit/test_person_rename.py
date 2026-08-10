"""Source-level guards for the person rename endpoint.

Rename is a display-name update that has to stay coherent across four
stores at once (entities, entity_aliases, person patches, the Redis
index), because person patches join to entities BY NAME. Each guard here
pins a way the coherence quietly breaks.
"""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
MAIN = (SRC / "main.py").read_text()


def _handler() -> str:
    m = re.search(
        r'@app\.post\("/v1/people/\{user_id\}/\{entity_id\}/rename".*?'
        r'@app\.post\("/v1/people/\{user_id\}"',
        MAIN, re.DOTALL,
    )
    assert m, "rename handler not found"
    return m.group(0)


def test_rename_route_exists():
    assert '@app.post("/v1/people/{user_id}/{entity_id}/rename"' in MAIN


def test_old_name_becomes_an_alias():
    """Without this, recall stops matching the old surface form and the
    next transcript that says it mints a duplicate person, the exact
    split the aliaser exists to prevent."""
    src = _handler()
    assert "INSERT INTO entity_aliases" in src
    assert "ON CONFLICT (user_id, LOWER(alias))" in src


def test_person_patches_are_rewritten_not_orphaned():
    """Patches join to entities by case-insensitive name. A rename that
    stops at the entities row orphans the person patch from its entity;
    the rewrite must cover aliases too (a patch whose text is an old
    alias is still this person's patch) and bump updated_at so the
    change rides the next delta."""
    src = _handler()
    m = re.search(r"UPDATE context_patches cp.*?RETURNING cp\.patch_id", src, re.DOTALL)
    assert m, "person patch rewrite not found"
    rewrite = m.group(0)
    assert "jsonb_set(value, '{text}'" in rewrite
    assert "updated_at = NOW()" in rewrite
    assert "= ANY($3::text[])" in rewrite, "must match name AND aliases, not just the name"
    assert "COALESCE(cp.status, 'active') = 'active'" in rewrite


def test_ledger_owner_strings_stay_raw():
    """doc 16 section 8b: value.owner is the RAW extracted surface form,
    never canonicalised. The ledger keeps matching old-name owners
    through the alias; rewriting them here would be the regression that
    contract exists to prevent."""
    src = _handler()
    assert "'{owner}'" not in src
    assert re.search(r"jsonb_set\(value, '\{text\}'", src), "only text is rewritten"


def test_name_collisions_are_a_merge_question():
    """409 NAME_TAKEN for a name that is another person's name or alias.
    Silently letting two people share a name would overturn any recorded
    keep-separate without asking."""
    src = _handler()
    assert "NAME_TAKEN" in src
    assert "LOWER(name) = LOWER($2)" in src
    assert "LOWER(alias) = LOWER($2)" in src


def test_self_name_uses_the_shared_predicate():
    """The user is the root of the graph, not a person node. The check
    must be is_user_reference, the SAME predicate the sanitizer and the
    ledger read side share; a rename-local variant is the write/read
    drift that produced the owed_to self hole."""
    src = _handler()
    assert "is_user_reference(" in src
    assert "SELF_NAME" in src


def test_rename_vouches_and_rebuilds_the_index():
    src = _handler()
    assert "confirmed_at = COALESCE(confirmed_at, NOW())" in src
    assert "_rebuild_entity_index" in src


def test_case_only_rename_skips_the_alias():
    """Renaming 'vijay' to 'Vijay' must not record an alias equal to the
    new name itself; the unique index on LOWER(alias) makes that a
    collision, and semantically it is dead weight."""
    assert "if not case_only:" in _handler()
