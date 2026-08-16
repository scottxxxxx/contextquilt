"""One human is one cluster, whatever the transcript called them.

The profile pass groups candidates by person PATCH, and the extractor
mints one person patch per surface form, so a colleague arrived as
several clusters. Measured on production 2026-08-16: Sukumar's 117 owned
items were split across `Sukumar` and `Sukumar Gurugubelli`, and he
rendered two HOW THEY DECIDE cards that were near paraphrases of each
other because each was derived from half his record.

The duplication was the visible symptom. The split corpus was the bug.
"""

import pytest

from contextquilt.services.people_identity import (
    build_entity_resolver,
    merge_person_clusters,
)


SUKUMAR = "49547cc9-f0b5-40eb-b2dc-c3e316193ff7"
VIJAY = "1f0b7f9f-588a-4d30-988e-c091207cc367"


@pytest.fixture
def resolve():
    """The live shape: a canonical entity, a merged duplicate of it, and
    recorded aliases, exactly as production held them."""
    entities = [
        {"entity_id": SUKUMAR, "name": "Sukumar Gurugubelli", "merged_into": None},
        {"entity_id": "dead0000-0000-0000-0000-000000000001",
         "name": "Sukumar", "merged_into": SUKUMAR},
        {"entity_id": VIJAY, "name": "Vijay Rayudu", "merged_into": None},
    ]
    aliases = [
        {"entity_id": SUKUMAR, "alias": "Sukumar"},
        {"entity_id": VIJAY, "alias": "Vijay"},
    ]
    return build_entity_resolver(entities, aliases)


def _cluster(patch_id, name, items, created_at="2026-04-14", meetings=3):
    return {
        "person_patch_id": patch_id,
        "person_name": name,
        "created_at": created_at,
        "patch_ids": items,
        "meeting_count": meetings,
    }


def test_two_surface_forms_of_one_human_become_one_cluster(resolve):
    merged = merge_person_clusters(
        [
            _cluster("p-short", "Sukumar", ["i1", "i2"]),
            _cluster("p-full", "Sukumar Gurugubelli", ["i3"], "2026-05-22"),
        ],
        resolve,
    )
    assert len(merged) == 1
    assert merged[0]["entity_id"] == SUKUMAR


def test_the_merged_cluster_sees_the_whole_record(resolve):
    """The correctness half. Each form used to be profiled on its own
    slice, so a computed claim counted a fraction of the person's items
    and served the wrong number."""
    merged = merge_person_clusters(
        [
            _cluster("p-short", "Sukumar", ["i1", "i2"]),
            _cluster("p-full", "Sukumar Gurugubelli", ["i3", "i4", "i5"]),
        ],
        resolve,
    )
    assert sorted(merged[0]["patch_ids"]) == ["i1", "i2", "i3", "i4", "i5"]


def test_source_ids_are_deduplicated_across_forms(resolve):
    """An item can hang off more than one form; it is still one item."""
    merged = merge_person_clusters(
        [
            _cluster("p-short", "Sukumar", ["shared", "i1"]),
            _cluster("p-full", "Sukumar Gurugubelli", ["shared", "i2"]),
        ],
        resolve,
    )
    assert sorted(merged[0]["patch_ids"]) == ["i1", "i2", "shared"]


def test_every_form_rides_along_for_the_durable_no(resolve):
    """A card stamps whichever form was current when it was derived, so
    a suppressed lens is only honoured if all the forms are read."""
    merged = merge_person_clusters(
        [
            _cluster("p-short", "Sukumar", ["i1"]),
            _cluster("p-full", "Sukumar Gurugubelli", ["i2"]),
        ],
        resolve,
    )
    assert sorted(merged[0]["person_patch_ids"]) == ["p-full", "p-short"]


def test_primary_is_the_patch_whose_text_is_already_canonical(resolve):
    """The id that identifies this person elsewhere must not move under
    us just because a merge happened."""
    merged = merge_person_clusters(
        [
            _cluster("p-short", "Sukumar", ["i1"], "2026-04-14"),
            _cluster("p-full", "Sukumar Gurugubelli", ["i2"], "2026-05-22"),
        ],
        resolve,
    )
    assert merged[0]["person_patch_id"] == "p-full"
    assert merged[0]["person_name"] == "Sukumar Gurugubelli"


def test_unresolvable_forms_stay_separate_rather_than_collapsing(resolve):
    """Two names CQ cannot resolve are not evidence they are the same
    person. Null means cannot tell, and cannot tell must not merge."""
    merged = merge_person_clusters(
        [
            _cluster("p-a", "Someone Unknown", ["i1"]),
            _cluster("p-b", "Another Stranger", ["i2"]),
        ],
        resolve,
    )
    assert len(merged) == 2
    assert all(c["entity_id"] is None for c in merged)


def test_an_unresolvable_person_is_still_profiled(resolve):
    """Dropping them would be worse than not merging them."""
    merged = merge_person_clusters(
        [_cluster("p-a", "Someone Unknown", ["i1", "i2"])], resolve
    )
    assert merged[0]["person_patch_id"] == "p-a"
    assert merged[0]["patch_ids"] == ["i1", "i2"]


def test_distinct_humans_are_not_merged(resolve):
    merged = merge_person_clusters(
        [
            _cluster("p-sukumar", "Sukumar", ["i1"]),
            _cluster("p-vijay", "Vijay", ["i2"]),
        ],
        resolve,
    )
    assert len(merged) == 2
    assert {c["entity_id"] for c in merged} == {SUKUMAR, VIJAY}


def test_ordering_is_deterministic_richest_record_first(resolve):
    """A pass that reshuffles its own inputs between cycles spends its
    budget somewhere different every night."""
    clusters = [
        _cluster("p-vijay", "Vijay", ["i1"]),
        _cluster("p-sukumar", "Sukumar", ["i2", "i3", "i4"]),
    ]
    first = merge_person_clusters(clusters, resolve)
    second = merge_person_clusters(list(reversed(clusters)), resolve)
    assert [c["person_patch_id"] for c in first] == \
           [c["person_patch_id"] for c in second]
    assert first[0]["entity_id"] == SUKUMAR


def test_rows_without_a_patch_id_are_skipped_not_crashed(resolve):
    merged = merge_person_clusters(
        [{"person_patch_id": None, "person_name": "Sukumar", "patch_ids": ["i1"]}],
        resolve,
    )
    assert merged == []


def test_no_resolver_degrades_to_one_cluster_per_patch():
    """A database without the alias table must not lose the pass."""
    merged = merge_person_clusters(
        [
            _cluster("p-short", "Sukumar", ["i1"]),
            _cluster("p-full", "Sukumar Gurugubelli", ["i2"]),
        ],
        lambda _s: None,
    )
    assert len(merged) == 2


def test_meeting_count_is_the_max_not_the_sum(resolve):
    """The forms overlap on meetings, so adding them would inflate the
    receipts gate the claim has to clear."""
    merged = merge_person_clusters(
        [
            _cluster("p-short", "Sukumar", ["i1"], meetings=5),
            _cluster("p-full", "Sukumar Gurugubelli", ["i2"], meetings=3),
        ],
        resolve,
    )
    assert merged[0]["meeting_count"] == 5
