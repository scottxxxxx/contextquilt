"""A role the person STATED beats the description a meeting INFERRED,
on the RECALL side and not only on the person detail route.

The rule shipped on the detail route in #301 and nowhere else, so a
title a user stated showed on their page while the recall block went on
feeding every AI surface the inference. SS refused to ship their "this
isn't right" correction against that, on the grounds that a title the
user typed and the memory contradicting it is worse than not offering
the feature. Scott authorised the hot-path spend on 2026-09-06 after it
was measured rather than estimated: one batched query, 7ms median on
real data at the 1 to 4 matched names a real header carries, 12ms at a
pathological 12. (The 5ms figure quoted while sizing was the query with
only ONE matching leg; adding the `describes` leg it actually needs cost
2ms and its flatness. Corrected here rather than left standing.)

The other half is that exactly one user-stated title stays live, and the
predicate that matters there is `origin_mode = 'declared'`: a role a
MEETING recorded is an observation and is never archived to tidy a list.
"""

import pathlib

from contextquilt.services.people_identity import (
    STATED_TITLE_SQL,
    SUPERSEDE_PRIOR_STATED_ROLE_SQL,
    apply_stated_titles,
    describes_target,
    title_from_stated_role,
)

MAIN = pathlib.Path("src/main.py").read_text()


def _person(name, desc, etype="person"):
    return {"entity_id": name.lower(), "name": name,
            "entity_type": etype, "description": desc}


# --- the read side -----------------------------------------------------

def test_a_stated_title_leads_and_the_observation_follows_labelled():
    """SS's actual case. Sarah reads "HR representative handling
    terminations and offboarding" and is VP of HR.

    This test asserted REPLACEMENT for about four hours on 2026-09-06 and
    was wrong to. Updated to the new rule with the reason rather than
    softened: stating a title adds what the user knows, it does not
    assert the observation was false."""
    rows = [_person("Sarah Brooks", "HR representative handling terminations")]
    out = apply_stated_titles(
        rows, [{"matched_name": "Sarah Brooks",
                "text": "Sarah Brooks is the VP of HR at Acme"}])
    assert out[0]["description"] == (
        "VP of HR at Acme; also observed: HR representative handling terminations")
    assert out[0]["title_stated"] is True


def test_the_observation_is_never_deleted_by_a_title():
    """The reversal, pinned. Scott, on seeing SS offer to "replace" an
    observed role: "it doesn't necessarily undo all of the other
    observations ... we shouldn't be trying to eliminate context."

    Steven being a founder is compatible with him also being the person
    who keeps picking up project management. The old rule generalised
    from the one case where title and description CONFLICT to every
    case, and built deletion into the wrong mechanism: CQ already has a
    separate path, the description dismissal, for "this claim is
    wrong"."""
    rows = [_person("Sarah Brooks", "HR representative handling terminations")]
    out = apply_stated_titles(
        rows, [{"matched_name": "Sarah Brooks", "text": "Sarah Brooks is VP of HR"}])
    assert "HR representative" in out[0]["description"]
    assert out[0]["description"].startswith("VP of HR")


def test_the_observation_is_labelled_as_an_inference():
    """If blending was ever the risk, labelling is the fix and deletion
    was not. A bare separator would read as one claim in two halves."""
    rows = [_person("Sarah Brooks", "HR representative handling terminations")]
    out = apply_stated_titles(
        rows, [{"matched_name": "Sarah Brooks", "text": "Sarah Brooks is VP of HR"}])
    assert "also observed:" in out[0]["description"]


def test_no_dangling_label_when_there_is_nothing_observed():
    rows = [_person("Nobody Yet", "")]
    out = apply_stated_titles(
        rows, [{"matched_name": "Nobody Yet", "text": "Nobody Yet is CTO"}])
    assert out[0]["description"] == "CTO"


def test_a_description_that_merely_restates_the_title_is_not_served_twice():
    rows = [_person("Same Person", "VP of HR")]
    out = apply_stated_titles(
        rows, [{"matched_name": "Same Person", "text": "Same Person is VP of HR"}])
    assert out[0]["description"] == "VP of HR"


def test_a_person_with_no_stated_role_is_untouched():
    """Which is every person today. The change must be invisible until
    somebody states something."""
    rows = [_person("Jaun Paul", "Lider empresarial en Casandina")]
    out = apply_stated_titles(rows, [])
    assert out[0]["description"] == "Lider empresarial en Casandina"
    assert "title_stated" not in out[0]

    out = apply_stated_titles(rows, [{"matched_name": "Someone Else",
                                      "text": "Someone Else is CTO"}])
    assert out[0]["description"] == "Lider empresarial en Casandina"


def test_only_people_are_retitled():
    """An org or project has no stated role, and a name collision must
    not rewrite one."""
    rows = [_person("Acme", "a consultancy", etype="org")]
    out = apply_stated_titles(
        rows, [{"matched_name": "Acme", "text": "Acme is VP of HR"}])
    assert out[0]["description"] == "a consultancy"


def test_the_read_side_uses_the_same_strip_as_the_detail_route():
    """The two surfaces disagreeing is the whole defect. A second strip
    here would just move it."""
    raw = "Sarah Brooks is the VP of HR at Acme"
    out = apply_stated_titles([_person("Sarah Brooks", "old")],
                              [{"matched_name": "Sarah Brooks", "text": raw}])
    assert out[0]["description"].startswith(
        title_from_stated_role(raw, ["Sarah Brooks"]))


def test_the_lookup_matches_on_BOTH_legs_the_detail_route_uses():
    """The first cut of this query carried only the name-prefix leg. It
    would have missed exactly the entity-id-bound shape SS chose, where
    the role phrase deliberately does NOT begin with the person's name
    and reaches them through a `describes` edge, so the title would have
    shown on the page and not in the block. That is the defect this
    whole change exists to remove, reproduced inside its own fix."""
    assert "LIKE lower(m.nm) || '%'" in STATED_TITLE_SQL
    assert "connection_label = 'describes'" in STATED_TITLE_SQL
    assert "lower(person_p.value->>'text') = lower(m.nm)" in STATED_TITLE_SQL


def test_the_lookup_is_one_batched_query_not_one_per_person():
    """One query for the whole header because it unnests the name array.
    Per-person would multiply a 7ms cost by the header's size on the hot
    path."""
    assert "unnest($2::text[])" in STATED_TITLE_SQL
    assert "DISTINCT ON (lower(m.nm))" in STATED_TITLE_SQL
    # newest wins, same as the detail route
    assert "cp.created_at DESC" in STATED_TITLE_SQL


def test_recall_runs_the_lookup_after_disambiguation():
    """The title belongs to whichever person the bare name RESOLVED to.
    Running it first would title a namesake who then left the header."""
    body = MAIN.split("async def recall_context")[1].split("\n@app.")[0]
    assert "STATED_TITLE_SQL" in body
    assert body.index("BARE_NAME_CANDIDATES_SQL") < body.index("STATED_TITLE_SQL")


def test_recall_fails_open_on_the_lookup():
    """A header carrying yesterday's description is a worse block. A
    broken recall is no block at all."""
    body = MAIN.split("async def recall_context")[1].split("\n@app.")[0]
    seg = body.split("STATED_TITLE_SQL")[1]
    assert "stated_title_lookup_failed" in seg
    assert "logger.warning" in seg


# --- the write side ----------------------------------------------------

def test_a_prior_user_stated_role_is_superseded():
    assert "origin_mode = 'declared'" in SUPERSEDE_PRIOR_STATED_ROLE_SQL
    assert "status = 'archived'" in SUPERSEDE_PRIOR_STATED_ROLE_SQL
    assert "'\"replaced\"'" in SUPERSEDE_PRIOR_STATED_ROLE_SQL


def test_an_extracted_role_is_never_archived():
    """THE load-bearing predicate. A role a meeting recorded is an
    observation, and a user stating their title today is not grounds to
    delete what was observed last month. Shortening a list by destroying
    receipts is what doc 16 5.13 forbids."""
    assert "cp.origin_mode = 'declared'" in SUPERSEDE_PRIOR_STATED_ROLE_SQL


def test_supersession_is_scoped_to_the_same_person():
    """Bound by the describes edge target, not by name, so a rename
    cannot silently widen the blast radius."""
    assert "pc.to_patch_id = $4::uuid" in SUPERSEDE_PRIOR_STATED_ROLE_SQL
    assert "connection_label = 'describes'" in SUPERSEDE_PRIOR_STATED_ROLE_SQL


def test_the_new_row_never_archives_itself():
    assert "cp.patch_id <> $1::uuid" in SUPERSEDE_PRIOR_STATED_ROLE_SQL


def test_the_target_comes_from_connections_actually_written():
    """Not the ones the caller asked for. A target that failed its
    ownership check must not trigger a supersession against somebody
    else's person."""
    assert describes_target([{"to": "p2", "role": "informs", "label": "describes"}]) == "p2"
    assert describes_target([{"to": "p1", "role": "informs", "label": "owns"}]) is None
    assert describes_target([]) is None
    assert describes_target(None) is None

    body = MAIN.split("async def create_patch")[1].split("\n@app.")[0]
    assert "describes_target(created_connections)" in body


def test_supersession_only_fires_for_the_apps_stated_role_type():
    """Never hardcoded to SS's `role`. A new app's vocabulary decides."""
    body = MAIN.split("async def create_patch")[1].split("\n@app.")[0]
    assert "vocab.stated_role_type" in body
    assert "patch.type == vocab.stated_role_type" in body


def test_a_supersession_failure_cannot_fail_the_write():
    """The new title is already written and wins on created_at anyway.
    A failure leaves an extra row in `items`, which is untidy, not
    wrong."""
    body = MAIN.split("async def create_patch")[1].split("\n@app.")[0]
    seg = body.split("describes_target(created_connections)")[1]
    assert "stated_role_supersede_failed" in seg
    assert "logger.warning" in seg


def test_the_archive_is_echoed_back_not_inferred():
    """Rule 4: a 200 says the write was processed, never that it did
    what the caller meant. "Did my old title actually go away" is the
    question a correction UI has to answer."""
    body = MAIN.split("async def _created_patch_response")[1].split("\n@app.")[0]
    assert '"superseded_patch_ids"' in body
    create = MAIN.split("async def create_patch")[1].split("\n@app.")[0]
    assert "superseded=superseded" in create


# --- the compact header, where the budget is smallest ------------------

def test_the_compact_header_keeps_the_stated_title_and_drops_the_observation():
    """Below 1600 chars the header drops inferred descriptions. It used
    to drop the stated title with them, because the title rides in the
    same field. That is the wrong thing to lose first: it is the user's
    own assertion, it is about 20 characters, and the conduct capsule
    that survives on this same line is nearer 120."""
    from contextquilt.services.recall_formatter import _name_with_stated_title
    row = {"name": "Sarah Brooks", "entity_type": "person",
           "stated_title": "VP of HR at Acme",
           "description": "VP of HR at Acme; also observed: HR representative"}
    assert _name_with_stated_title(row) == "Sarah Brooks (VP of HR at Acme)"
    # the observation does NOT ride along at this budget
    assert "also observed" not in _name_with_stated_title(row)


def test_the_compact_header_is_unchanged_for_a_person_with_no_stated_title():
    """Which is everyone today. The compact header must stay names-only
    until somebody states something."""
    from contextquilt.services.recall_formatter import _name_with_stated_title
    assert _name_with_stated_title(
        {"name": "Steven Williams", "description": "business founder"}
    ) == "Steven Williams"


def test_the_bare_title_is_carried_for_the_compact_header():
    """The formatter must not have to parse the joined string back
    apart to find the assertion inside it."""
    rows = [_person("Sarah Brooks", "HR representative handling terminations")]
    out = apply_stated_titles(
        rows, [{"matched_name": "Sarah Brooks",
                "text": "Sarah Brooks is the VP of HR at Acme"}])
    assert out[0]["stated_title"] == "VP of HR at Acme"
    assert out[0]["description"].startswith("VP of HR at Acme; also observed:")


def test_the_formatter_ACTUALLY_serves_the_stated_title_at_a_small_budget():
    """EXECUTING sibling to the helper tests above, and it exists
    because removing the wiring at the call site broke nothing.

    The helper tests prove `_name_with_stated_title` works. They do NOT
    prove the formatter calls it, and a sabotage that reverted the call
    site to `line = name` passed the entire suite. That is the "the fix
    landed on the call site somebody looked at" shape, caught here by
    running the real formatter rather than reading it.
    """
    from contextquilt.services.recall_formatter import (
        format_flat_ranked_with_stats,
    )

    entity_rows = [{
        "entity_id": "e1", "name": "Sarah Brooks", "entity_type": "person",
        "stated_title": "VP of HR at Acme",
        "description": "VP of HR at Acme; also observed: HR representative",
    }]
    patches = [(50.0, {
        "patch_id": "p1", "patch_type": "commitment",
        "value": {"text": "Send the offer letter", "owner": "Scott"},
    })]

    # Small budget: the compact header fires.
    small, _ = format_flat_ranked_with_stats(
        patches, entity_rows, [], max_chars=800)
    assert "Sarah Brooks (VP of HR at Acme)" in small, small
    assert "also observed" not in small

    # Large budget: the full header carries both.
    big, _ = format_flat_ranked_with_stats(
        patches, entity_rows, [], max_chars=4000)
    assert "VP of HR at Acme" in big
    assert "also observed" in big
