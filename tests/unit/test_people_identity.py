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
    normalise_merge_request,
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
    assert validate_person_name("  Sarah Chen  ") == "Sarah Chen"


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
