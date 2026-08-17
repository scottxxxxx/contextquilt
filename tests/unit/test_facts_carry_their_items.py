"""A fact has to know WHICH items it counted.

Scott, on a card a reviewer liked: "it's literally citing an example of
what we're describing. It couldn't be a better way to help characterize
somebody."

He is right, and the rule that produced our blandness was never "be
gentle". It was INSTANCES NEVER TRAITS, which came from a
defamation-shape concern. An example is the MOST compliant form of that
rule, not a violation of it: "the KB item has moved three times since
June" is pure instance, while "reschedules more often than others" is
the one that generalises toward a trait.

`Fact.patch_ids` existed to carry exactly that and was always empty:
`facts_for_person` read five `*_patch_ids` keys and the worker's query
produced none of them. A contract with exactly one carrier and no
carrier at all, doc 19.2, inside the lens whose own comments cite 19.2.
"""

import pathlib

from contextquilt.services.relationship_lenses import FACT_SUBJECTS, facts_for_person

WORKER = pathlib.Path("src/worker.py").read_text()
STANDS_OUT_SQL = WORKER.split("WITH resolver AS (")[1].split("GROUP BY canonical_id")[0]

# The key `facts_for_person` reads, per fact.
EXPECTED_KEYS = {
    "went_quiet": "quiet_patch_ids",
    "closed_late": "late_patch_ids",
    "re_dated": "re_dated_patch_ids",
    "handed_back": "handed_patch_ids",
    "restated": "restated_patch_ids",
}


def test_every_fact_key_has_a_matching_id_column_in_the_query():
    """The failure this pins: the reader existed, the writer did not, and
    nothing raised because a missing dict key is just None."""
    assert set(EXPECTED_KEYS) == set(FACT_SUBJECTS)
    for fact_key, id_key in EXPECTED_KEYS.items():
        assert f"AS {id_key}" in STANDS_OUT_SQL, f"{fact_key} has no {id_key}"


def test_the_ids_are_filtered_to_the_same_rows_as_the_count():
    """An id list gathered on a different predicate than its count is
    worse than no list: the card would cite an item it did not count."""
    for predicate in ("WHERE re_dated) AS re_dated_patch_ids",
                      "WHERE handed) AS handed_patch_ids"):
        assert predicate in STANDS_OUT_SQL


def test_a_fact_carries_the_items_it_counted():
    fact = [f for f in facts_for_person({
        "closed_items": 20, "closed_late": 6,
        "late_patch_ids": ["a", "b", "c", "d", "e", "f"],
    }) if f.key == "closed_late"][0]
    assert fact.numerator == 6
    assert fact.patch_ids == ["a", "b", "c", "d", "e", "f"]


def test_a_fact_with_no_ids_still_works():
    """Older rows predate the columns, and a card without an exemplar is
    worse than no card only if the absence crashes it."""
    fact = [f for f in facts_for_person({
        "closed_items": 20, "closed_late": 6,
    }) if f.key == "closed_late"][0]
    assert fact.patch_ids == []
