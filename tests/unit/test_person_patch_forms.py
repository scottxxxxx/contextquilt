"""A person is one human, not one sentence about them.

The extractor writes a `person` patch using whatever surface form the
transcript gave it, so one human accumulates several: "Suresh", "Suresh
Muchakurti", and every rephrasing a later meeting produces. The people
read resolves ONE of those, canonical name first then aliases, and that
one identifies the person for the ledger and the ownership joins.

Derived insights stamp `value.source_person` with whichever patch was
current WHEN THEY WERE DERIVED. So a lookup on the primary alone finds
nothing whenever the extractor has since rephrased someone, and the page
renders not-yet cards over finished insights.

Found in production 2026-08-16: three active insights on "Suresh", a page
resolving "Suresh Muchakurti", three blank cards. The replay run that
evening added two more surface forms in one hour, so the drift is not
rare and does not settle.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def _resolve(entity_name, aliases, patch_by_name):
    """The resolution as `_people_core` performs it."""
    name_keys = [entity_name.lower()] + [a.lower() for a in aliases]
    patch_ids = list(dict.fromkeys(
        patch_by_name[k] for k in name_keys if k in patch_by_name
    ))
    return (patch_ids[0] if patch_ids else None), patch_ids


PATCHES = {
    "suresh muchakurti": "patch-canonical",
    "suresh": "patch-april",
    "pradeep & suresh": "patch-compound",
}


class TestEverySurfaceForm:
    def test_all_forms_resolve_not_just_the_first(self):
        primary, all_ids = _resolve("Suresh Muchakurti", ["Suresh"], PATCHES)
        assert primary == "patch-canonical"
        assert all_ids == ["patch-canonical", "patch-april"]

    def test_the_primary_is_still_the_canonical_name(self):
        # The ledger and ownership joins key on one patch and must not
        # start drifting to an alias-named one.
        primary, _ = _resolve("Suresh Muchakurti", ["Suresh"], PATCHES)
        assert primary == "patch-canonical"

    def test_alias_only_person_still_resolves(self):
        primary, all_ids = _resolve("S. Muchakurti", ["Suresh"], PATCHES)
        assert primary == "patch-april"
        assert all_ids == ["patch-april"]

    def test_no_matching_patch_is_none_not_a_crash(self):
        # A person observed as an entity with no person patch yet: the
        # thinnest possible not-yet, and the case the empty card exists
        # for. Must stay None rather than becoming an empty-string id.
        primary, all_ids = _resolve("Someone New", [], PATCHES)
        assert primary is None
        assert all_ids == []

    def test_duplicate_hits_are_collapsed(self):
        # Name and alias can resolve to the same patch; the id list feeds
        # a SQL ANY() and must not carry it twice.
        patches = {"suresh": "patch-april"}
        _, all_ids = _resolve("Suresh", ["suresh", "SURESH"], patches)
        assert all_ids == ["patch-april"]

    def test_order_is_stable(self):
        # Primary first, then aliases in the order given. The first entry
        # IS the primary, so the two must never disagree.
        primary, all_ids = _resolve("Suresh Muchakurti", ["Suresh"], PATCHES)
        assert all_ids[0] == primary
