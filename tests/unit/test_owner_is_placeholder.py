""""Speaker 3" is somebody, and it is not nobody.

Sibling of test_owner_names_multiple. Same surface, same division of
labour, a different thing the client cannot see: SS's unplaced-owner
sheet renders "Which Speaker 3?" and then explains that Speaker 3 has
not been in a meeting we recorded, which is nonsense about a diarization
label. Measured on prod 2026-08-19: 51 OPEN completables across 10
projects carry a placeholder owner, and `owner_names_multiple` reports
False for every one of them (one name, whoever it is), so nothing on the
wire told a client these were not people.

The second half of this file pins the honesty fix that came with it. A
placeholder owner used to make `owned_by_self` answer False, a confident
"this is somebody else's", when the truth is that CQ does not know whose
it is. False and null are different claims on a three-valued field.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from contextquilt.services.people_identity import (
    owned_by_self_verdict,
    owner_is_placeholder,
    owner_names_multiple,
)

MAIN = (ROOT / "src" / "main.py").read_text()


def resolve(name):
    return None


def test_a_diarization_label_is_named_as_one():
    assert owner_is_placeholder("Speaker 3") is True
    assert owner_is_placeholder("speaker_8") is True
    assert owner_is_placeholder("Unknown") is True
    assert owner_is_placeholder("Unidentified participant") is True


def test_a_real_name_is_a_confident_no():
    assert owner_is_placeholder("Geoffrey") is False
    assert owner_is_placeholder("Scott Guida") is False
    assert owner_is_placeholder("Product team") is False


def test_no_owner_is_null_not_false():
    """False would assert "the owner is a real person" about an item that
    has no owner at all. Nothing to judge is its own answer, and the
    ownerless case is already carried by owned_by_self."""
    assert owner_is_placeholder(None) is None
    assert owner_is_placeholder("") is None
    assert owner_is_placeholder("   ") is None


def test_junk_never_raises_on_a_read_route():
    for junk in (42, ["Speaker 3"], {"name": "Speaker 3"}):
        assert owner_is_placeholder(junk) is None


def test_it_answers_where_the_sibling_field_cannot():
    """The pair is the point. "Speaker 3" is a single name to the
    compound check and a non-person to this one, so a client needs both
    to decide whether asking "which one?" makes any sense."""
    assert owner_names_multiple("Speaker 3", resolve) is False
    assert owner_is_placeholder("Speaker 3") is True


def test_the_field_is_declared_and_populated():
    """Declared but never assigned ships null forever and reads as "no
    placeholder owners exist", which is the silent half of this bug
    class (doc 19.2: a contract with exactly one carrier)."""
    assert "owner_is_placeholder: Optional[bool] = None" in MAIN
    assert "owner_is_placeholder=(" in MAIN
    assert 'owner_is_placeholder(value.get("owner"))' in MAIN


def test_it_is_scoped_to_completables():
    block = MAIN.split("owner_is_placeholder=(")[1][:220]
    assert "in completable else None" in block


SELF = "ego-entity-id"


def test_owned_by_self_abstains_on_a_placeholder_owner():
    """The case this whole file exists for. It must not answer False,
    which is what shipped, and it must not answer True either: the
    ownerless rule hands unowned items to the user, and handing them
    "Speaker 3" would credit the user with a stranger's obligation."""
    assert owned_by_self_verdict(None, SELF, "Speaker 3", "Speaker 3") is None
    assert owned_by_self_verdict(None, SELF, "Unknown", "Unknown") is None


def test_the_three_ordinary_answers_are_unchanged():
    assert owned_by_self_verdict(SELF, SELF, "Scott", "Scott") is True
    assert owned_by_self_verdict("other-id", SELF, "Vijay", "Vijay") is False
    assert owned_by_self_verdict(None, SELF, None, None) is True
    assert owned_by_self_verdict(None, SELF, "", "") is True


def test_a_resolved_owner_wins_over_the_placeholder_check():
    """An owns-edge that resolved to a real person is a stronger signal
    than the text it was resolved from. If a placeholder ever resolves
    to somebody, the resolution is the answer."""
    assert owned_by_self_verdict(SELF, SELF, "Speaker 3", "Speaker 3") is True
    assert owned_by_self_verdict("other-id", SELF, "Speaker 3", "Speaker 3") is False


def test_the_edge_text_is_what_the_placeholder_check_reads():
    """The two owner strings are not interchangeable. An owns-edge
    naming a placeholder over a patch whose own owner field is empty
    must abstain, not fall through to the ownerless rule and become the
    user's item."""
    assert owned_by_self_verdict(None, SELF, "Speaker 6", None) is None


def test_the_verdict_is_the_one_the_route_serves():
    """Lifting the rule out is only worth anything if the route calls
    it. A second copy inside the closure would pass every test in this
    file while serving something else."""
    body = MAIN.split("def _owned_by_self(")[1].split("\n    for row in rows:")[0]
    assert "owned_by_self_verdict(" in body
    assert 'return not value.get("owner")' not in body
