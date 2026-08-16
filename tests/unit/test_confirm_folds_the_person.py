"""Confirming a person is an identity assertion, so it does identity work.

Scott, 2026-08-16: "on the app, I see nothing showing someone is
confirmed or even what doing that buys me."

He was right, and the data proved it harder than the question did. FOUR
people on his roster were already confirmed by a human and still held
four or five `person` patches each: Xhoi 5, Mike DiTroia 4, Vijay 4,
Sukumar 4. The user had answered the question and the answer changed
nothing, which is the worst shape a prompt can have.

Before this, confirm stamped `confirmed_at` and returned the stamp.
Nothing in the worker read it, no alias was written, and every join that
keys on a person patch still saw a fraction of the person.
"""

import pathlib

MAIN = pathlib.Path("src/main.py").read_text()


def test_the_fold_is_one_implementation_shared_by_both_writers():
    """Merge and confirm are the same write with different triggers. A
    second copy would drift from this one the first time either moved."""
    assert "async def _fold_person_patches(" in MAIN
    assert MAIN.count("await _fold_person_patches(") == 2


def test_confirm_folds_the_surface_forms():
    confirm = MAIN.split("Mark a person a human has vouched for.")[1]
    assert "_fold_person_patches(" in confirm.split("@app.post")[0]


def test_confirm_reports_what_the_tap_actually_did():
    """A confirmation that reports nothing is indistinguishable from one
    that did nothing, which is precisely how this read before."""
    assert '"folded_patch_ids": folded_patch_ids,' in MAIN
    assert '"folded_count": len(folded_patch_ids),' in MAIN


def test_confirm_resolves_through_aliases_not_just_the_canonical_name():
    """The forms worth folding are the ones the extractor invented, and
    those reach the entity through its aliases."""
    confirm = MAIN.split("Mark a person a human has vouched for.")[1]
    head = confirm.split("@app.post")[0]
    assert "entity_aliases" in head
    assert "owner_keys(" in head


def test_confirm_rebuilds_the_entity_index():
    """The recall name index is built from names and aliases, so folding
    identities behind its back would leave it pointing at retired ones."""
    confirm = MAIN.split("Mark a person a human has vouched for.")[1]
    assert "_rebuild_entity_index(user_id)" in confirm.split("@app.post")[0]


def test_the_fold_archives_rather_than_deletes():
    """Delta sync's `deleted` array is how a client learns a patch went
    away. A hard delete is the tombstone lesson all over again."""
    fold = MAIN.split("async def _fold_person_patches(")[1].split("@app.post")[0]
    assert "SET status = 'archived'" in fold
    assert "'archive_cause', 'merge'" in fold
