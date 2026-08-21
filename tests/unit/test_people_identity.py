"""
Unit tests for the People identity write-back decision logic.

Pure functions only; the endpoints own the SQL. The cases that matter
most here are the ones that protect a user's answer: an unordered
separation pair must block a merge from either direction, and a
diarization placeholder must never become a person the API vouched for.
"""

import pytest

from contextquilt.services.people_identity import (
    DEFAULT_IDENTITY_SOURCE,
    IdentityRequestError,
    canonical_pair,
    capability_report,
    merge_project_rollups,
    normalise_merge_request,
    owner_keys,
    resolve_identity_source,
    separation_conflicts,
    validate_person_name,
)

A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"
C = "33333333-3333-4333-8333-333333333333"


# ---------------------------------------------------------------- pairs

def test_canonical_pair_is_order_independent():
    assert canonical_pair(A, B) == canonical_pair(B, A)


def test_canonical_pair_sorts_ascending():
    lo, hi = canonical_pair(B, A)
    assert lo < hi
    assert (lo, hi) == (A, B)


def test_canonical_pair_lowercases_client_uppercase():
    assert canonical_pair(A.upper(), B) == (A, B)


def test_canonical_pair_rejects_self():
    with pytest.raises(IdentityRequestError) as exc:
        canonical_pair(A, A)
    assert exc.value.code == "SELF_PAIR"


def test_canonical_pair_rejects_empty():
    with pytest.raises(IdentityRequestError) as exc:
        canonical_pair(A, "   ")
    assert exc.value.code == "EMPTY_ENTITY_ID"


# ------------------------------------------------------- merge requests

def test_normalise_merge_request_dedups_preserving_order():
    canonical, losers = normalise_merge_request(A, [C, B, C])
    assert canonical == A
    assert losers == [C, B]


def test_normalise_merge_request_drops_canonical_from_losers():
    # Merging something into itself is a harmless intent, not an error,
    # but it must not survive into the loser list or the endpoint would
    # try to fold the canonical into itself.
    _, losers = normalise_merge_request(A, [A, B])
    assert losers == [B]


def test_normalise_merge_request_rejects_only_self():
    with pytest.raises(IdentityRequestError) as exc:
        normalise_merge_request(A, [A])
    assert exc.value.code == "NO_MERGE_TARGETS"


def test_normalise_merge_request_rejects_empty_list():
    with pytest.raises(IdentityRequestError) as exc:
        normalise_merge_request(A, [])
    assert exc.value.code == "NO_MERGE_TARGETS"


def test_normalise_merge_request_rejects_missing_canonical():
    with pytest.raises(IdentityRequestError) as exc:
        normalise_merge_request("", [B])
    assert exc.value.code == "EMPTY_ENTITY_ID"


def test_normalise_merge_request_lowercases():
    canonical, losers = normalise_merge_request(A.upper(), [B.upper()])
    assert canonical == A
    assert losers == [B]


# --------------------------------------------------------- separations

def test_separation_blocks_merge_recorded_in_same_order():
    assert separation_conflicts(A, [B], [(A, B)]) == [(A, B)]


def test_separation_blocks_merge_recorded_in_reverse_order():
    # The whole point of canonicalising the pair: the user said "keep
    # separate" with B first, and merging with A first must still refuse.
    assert separation_conflicts(A, [B], [(B, A)]) == [(A, B)]


def test_separation_ignores_unrelated_pairs():
    assert separation_conflicts(A, [B], [(A, C)]) == []


def test_separation_reports_only_offending_members_of_a_batch():
    conflicts = separation_conflicts(A, [B, C], [(C, A)])
    assert conflicts == [(A, C)]


def test_separation_with_no_records_allows_everything():
    assert separation_conflicts(A, [B, C], []) == []


# --------------------------------------------------------------- names

@pytest.mark.parametrize("bad", ["Speaker 3", "speaker 12", "Unknown", "unknown"])
def test_validate_person_name_rejects_placeholders(bad):
    # drop_placeholder_entities spends real effort keeping these out of
    # the graph; the create endpoint must not be a hole straight through it.
    with pytest.raises(IdentityRequestError) as exc:
        validate_person_name(bad)
    assert exc.value.code == "PLACEHOLDER_NAME"


def test_validate_person_name_rejects_blank():
    with pytest.raises(IdentityRequestError) as exc:
        validate_person_name("   ")
    assert exc.value.code == "EMPTY_NAME"


def test_validate_person_name_rejects_overlong():
    with pytest.raises(IdentityRequestError) as exc:
        validate_person_name("x" * 201)
    assert exc.value.code == "NAME_TOO_LONG"


def test_validate_person_name_trims_and_accepts():
    assert validate_person_name("  Lockridge Chen  ") == "Lockridge Chen"


def test_validate_person_name_accepts_accented():
    assert validate_person_name("José Álvarez") == "José Álvarez"


# -------------------------------------------------------------- source

def test_resolve_identity_source_defaults():
    assert resolve_identity_source(None) == DEFAULT_IDENTITY_SOURCE
    assert resolve_identity_source("  ") == DEFAULT_IDENTITY_SOURCE


def test_resolve_identity_source_passes_unknown_through():
    # The column is free-form on purpose so a new app can record its own
    # provenance without a CQ deploy.
    assert resolve_identity_source("shouldersurf_voice_v2") == "shouldersurf_voice_v2"


# --------------------------------------------------------- owner_keys

def test_owner_keys_covers_canonical_and_aliases():
    assert owner_keys("Lockridge Chen", ["Lockridge C", "S. C"]) == {
        "lockridge chen", "lockridge c", "s. c",
    }


def test_owner_keys_drops_blank_forms():
    # A blank alias would match every commitment whose owner is empty,
    # handing one person the entire ownerless backlog.
    assert owner_keys("Lockridge Chen", ["", "   ", None]) == {"lockridge chen"}


def test_owner_keys_handles_no_aliases():
    assert owner_keys("Marcus Webb", []) == {"marcus webb"}


# ----------------------------------------------------- project rollups

def test_rollup_marks_observed_and_stated_separately():
    out = merge_project_rollups(
        [{"project_id": "p1", "project": "Atlas", "meeting_count": 5}],
        [{"project_id": "p2", "project": "Vendor Eval"}],
    )
    atlas = next(r for r in out if r["project_id"] == "p1")
    vendor = next(r for r in out if r["project_id"] == "p2")
    assert (atlas["observed"], atlas["stated"]) == (True, False)
    assert (vendor["observed"], vendor["stated"]) == (False, True)
    assert vendor["meeting_count"] == 0


def test_rollup_merges_both_signals_for_one_project():
    out = merge_project_rollups(
        [{"project_id": "p1", "project": "Atlas", "meeting_count": 3}],
        [{"project_id": "p1", "project": "Atlas"}],
    )
    assert len(out) == 1
    assert out[0]["observed"] and out[0]["stated"]
    assert out[0]["meeting_count"] == 3


def test_rollup_orders_by_meeting_count_then_name():
    # A browse surface must not reshuffle between polls.
    out = merge_project_rollups(
        [
            {"project_id": "p1", "project": "Zeta", "meeting_count": 1},
            {"project_id": "p2", "project": "Alpha", "meeting_count": 9},
            {"project_id": "p3", "project": "Beta", "meeting_count": 1},
        ],
        [],
    )
    assert [r["project"] for r in out] == ["Alpha", "Beta", "Zeta"]


def test_rollup_falls_back_to_name_when_project_id_missing():
    # Unscoped meetings carry a display name and no stable id; two of
    # them are still the same project.
    out = merge_project_rollups(
        [
            {"project_id": None, "project": "Atlas", "meeting_count": 1},
            {"project_id": None, "project": "atlas", "meeting_count": 2},
        ],
        [],
    )
    assert len(out) == 1
    assert out[0]["meeting_count"] == 3


def test_rollup_backfills_a_missing_display_name():
    out = merge_project_rollups(
        [{"project_id": "p1", "project": None, "meeting_count": 1}],
        [{"project_id": "p1", "project": "Atlas"}],
    )
    assert out[0]["project"] == "Atlas"


def test_rollup_of_nothing_is_empty():
    assert merge_project_rollups([], []) == []


# ------------------------------------------------------- capabilities

def test_you_owe_is_reported_unavailable_with_a_reason():
    # The whole point: a client must be able to render "not tracked yet"
    # instead of reading a null as "owes nothing".
    caps = capability_report()
    assert caps["you_owe"]["available"] is False
    assert "owed_to" in caps["you_owe"]["reason"]


def test_confirmed_mention_split_is_reported_unavailable():
    assert capability_report()["confirmed_mention_split"]["available"] is False


def test_available_capabilities_carry_no_reason():
    caps = capability_report()
    assert caps["they_owe"]["available"] is True
    assert caps["they_owe"]["reason"] is None


def test_capability_report_is_a_copy():
    # Handing out the module dict would let one request's mutation leak
    # into every later response.
    caps = capability_report()
    caps["you_owe"]["available"] = True
    assert capability_report()["you_owe"]["available"] is False


# =====================================================================
# SS review round 1 deltas (doc 16 section 8b).
#
# These assert the CONTRACT the SS team acked, not the plumbing. Each one
# encodes a condition they attached to their ack, so a future refactor
# that "tidies" one of them fails here with the reason attached.
# =====================================================================

def test_ledger_owner_must_stay_the_raw_surface_form():
    """The whole point of `owner` is diffing against SS's own action-item
    owner strings. Resolving "Lockridge C" to canonical "Lockridge Chen" makes the
    field look helpful while doing nothing, because the caller never sees
    the form that would match. SS already knows the canonical identity:
    it is the person whose endpoint they called.

    Guards the docstring contract on _item() in src/main.py.
    """
    import inspect
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[2] / "src" / "main.py"
    text = src.read_text(encoding="utf-8")

    # The field exists and is wired straight from the row, unresolved.
    assert '"owner": r["owner"],' in text, (
        "ledger _item() must pass value.owner through verbatim; any "
        "canonicalisation here breaks SS-side ledger reconciliation"
    )
    # The reason is recorded where someone refactoring would see it.
    assert "regression, not a tidy-up" in text


def test_open_items_query_selects_raw_owner():
    """`owner` has to come from value.owner, not from a join against
    entities, or it would arrive canonicalised by construction."""
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[2] / "src" / "main.py"
    text = src.read_text(encoding="utf-8")
    assert "cp.value->>'owner' AS owner" in text


def test_min_meetings_is_applied_before_limit():
    """A floor applied after pagination is a truncation, not a filter:
    the caller gets an arbitrary subset and cannot tell whether the next
    page holds more that would have passed. Assert source ordering."""
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[2] / "src" / "main.py"
    text = src.read_text(encoding="utf-8")
    body = text.split("async def list_people")[1].split("@app.get")[0]

    floor_at = body.index('r["meeting_count"] >= min_meetings')
    total_at = body.index("total = len(rows)")
    limit_at = body.index("rows[:limit]")

    assert floor_at < total_at < limit_at, (
        "order must be: filter, then total, then limit"
    )


def test_total_unfiltered_is_taken_before_any_filter():
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[2] / "src" / "main.py"
    text = src.read_text(encoding="utf-8")
    body = text.split("async def list_people")[1].split("@app.get")[0]

    unfiltered_at = body.index("total_unfiltered = len(rows)")
    confirmed_filter_at = body.index('if confirmed in ("true", "false")')
    assert unfiltered_at < confirmed_filter_at


def test_total_unfiltered_counts_active_entities_only():
    """SS pinned this: it must exclude anything folded away by a merge,
    so tidying a roster makes the number go DOWN rather than inflate it.
    _people_core is the only source, and it filters merged_into IS NULL."""
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[2] / "src" / "main.py"
    text = src.read_text(encoding="utf-8")
    core = text.split("async def _people_core")[1].split("def _public_person")[0]
    assert "e.merged_into IS NULL" in core


def test_query_echo_reports_received_values_not_applied_ones():
    """SS needs the contract named, because their three-way assertion is
    written against it. Received, plus an `ignored` array for anything CQ
    could not use. A malformed `confirmed` echoes the malformed value AND
    appears in `ignored`."""
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[2] / "src" / "main.py"
    text = src.read_text(encoding="utf-8")
    body = text.split("async def list_people")[1].split("@app.get")[0]

    # Echoed straight from the request parameters, never from local
    # post-fallback variables.
    assert '"since": since,' in body
    assert '"confirmed": confirmed,' in body
    assert '"ignored": ignored,' in body
    # And both degradation paths record themselves.
    assert 'ignored.append("since")' in body
    assert 'ignored.append("confirmed")' in body
    assert "echoes what CQ RECEIVED" in body


# =====================================================================
# Person-patch fold on merge (doc 16 section 6.5).
#
# Merging entities alone left two Sarahs visible one segment over,
# because `person` is a rendered patch type and /v1/quilt applies no
# type exclusion. These cover which patch survives the fold.
# =====================================================================

from datetime import datetime, timedelta  # noqa: E402

from contextquilt.services.people_identity import (  # noqa: E402
    choose_surviving_person_patch,
)

T0 = datetime(2026, 5, 1)


def _p(pid, text, days=0):
    return {"patch_id": pid, "text": text, "created_at": T0 + timedelta(days=days)}


def test_exact_canonical_name_wins_even_when_newer():
    # Keeping the patch that already says the canonical name means the
    # surviving fact needs no rewrite.
    survivor, losers = choose_surviving_person_patch(
        [_p("old", "Lockridge C", 0), _p("new", "Lockridge Chen", 10)], "Lockridge Chen"
    )
    assert survivor["patch_id"] == "new"
    assert [l["patch_id"] for l in losers] == ["old"]


def test_oldest_wins_when_no_exact_match():
    # The oldest patch carries the longest history and the most
    # connections; the newest is just the extractor's latest guess.
    survivor, losers = choose_surviving_person_patch(
        [_p("newer", "Lockridge C", 10), _p("older", "S. Chen", 0)], "Lockridge Chen"
    )
    assert survivor["patch_id"] == "older"


def test_match_is_case_and_whitespace_insensitive():
    survivor, _ = choose_surviving_person_patch(
        [_p("a", "Lockridge C", 0), _p("b", "  lockridge chen  ", 5)], "Lockridge Chen"
    )
    assert survivor["patch_id"] == "b"


def test_single_candidate_is_not_a_fold():
    # One patch is already the survivor; folding would archive the only
    # copy of the fact.
    assert choose_surviving_person_patch([_p("only", "Lockridge Chen")], "Lockridge Chen") == (None, [])


def test_no_candidates_is_a_noop():
    assert choose_surviving_person_patch([], "Lockridge Chen") == (None, [])


def test_rows_without_a_patch_id_are_ignored():
    survivor, losers = choose_surviving_person_patch(
        [_p("real", "Lockridge C", 0), {"text": "Lockridge Chen"}, None], "Lockridge Chen"
    )
    assert survivor is None and losers == []


def test_missing_created_at_does_not_break_ordering():
    # Hand-made rows can lack created_at; sorting must not raise.
    survivor, losers = choose_surviving_person_patch(
        [{"patch_id": "x", "text": "A", "created_at": None}, _p("y", "B", 3)], "Z"
    )
    assert survivor["patch_id"] == "y"
    assert [l["patch_id"] for l in losers] == ["x"]


def test_every_loser_is_returned_for_a_three_way_merge():
    survivor, losers = choose_surviving_person_patch(
        [_p("a", "Lockridge Chen", 0), _p("b", "Lockridge C", 1), _p("c", "S. Chen", 2)],
        "Lockridge Chen",
    )
    assert survivor["patch_id"] == "a"
    assert sorted(l["patch_id"] for l in losers) == ["b", "c"]


# ============================================================
# Stated roles and the title (2026-08-21)
# ============================================================

from src.contextquilt.services.people_identity import (
    title_from_stated_role,
    stated_roles_payload,
    people_vocabulary,
)


def test_title_strips_own_name_and_copula():
    assert title_from_stated_role("Suresh is scrum master on ABM project", ["Suresh Muchakurti", "Suresh"]) == "scrum master on ABM project"
    assert title_from_stated_role("Xhoi is business analyst and QA on ABM project", ["Xhoi"]) == "business analyst and QA on ABM project"
    assert title_from_stated_role("Suresh: scrum master", ["Suresh"]) == "scrum master"
    assert title_from_stated_role("Suresh, scrum master on ABM", ["Suresh"]) == "scrum master on ABM"


def test_title_prefers_longest_name_so_a_full_name_is_not_split():
    # "Suresh Muchakurti is..." must not strip only "Suresh" and leave
    # "Muchakurti is scrum master".
    assert title_from_stated_role("Suresh Muchakurti is scrum master", ["Suresh", "Suresh Muchakurti"]) == "scrum master"


def test_title_without_a_leading_name_is_the_text_itself():
    assert title_from_stated_role("Head coach of the U12 team", ["Sam"]) == "Head coach of the U12 team"


def test_title_never_invents_and_handles_empty():
    assert title_from_stated_role("", ["Suresh"]) is None
    assert title_from_stated_role(None, ["Suresh"]) is None
    assert title_from_stated_role("Suresh is ", ["Suresh"]) is None


def test_stated_roles_payload_newest_first_wins_title():
    rows = [
        {"patch_id": "p2", "text": "Suresh is scrum master on ABM project", "origin_id": "m2", "stated_at": "2026-08-17"},
        {"patch_id": "p1", "text": "Suresh is a developer on Kore", "origin_id": "m1", "stated_at": "2026-06-01"},
    ]
    out = stated_roles_payload(rows, ["Suresh"])
    assert out["title"] == "scrum master on ABM project"
    assert out["title_source"] == {"patch_id": "p2", "origin_id": "m2", "stated_at": "2026-08-17"}
    assert [i["text"] for i in out["items"]] == [r["text"] for r in rows]  # raw text kept, never rewritten


def test_stated_roles_payload_empty_is_tracked_but_none():
    out = stated_roles_payload([], ["Suresh"])
    assert out == {"title": None, "title_source": None, "items": []}


def test_vocabulary_stated_role_type_floor_and_explicit_null():
    assert people_vocabulary({}).stated_role_type == "role"
    assert people_vocabulary({"people": {"person_type": "person"}}).stated_role_type == "role"
    # An explicit null is "we do not track stated roles", not the floor.
    assert people_vocabulary({"people": {"person_type": "person", "stated_role_type": None}}).stated_role_type is None
    assert people_vocabulary({"people": {"person_type": "contact", "stated_role_type": "position"}}).stated_role_type == "position"
