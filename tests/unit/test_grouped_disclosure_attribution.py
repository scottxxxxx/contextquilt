"""'About you' must not contain other people's opinions.

Scott fed the app a TWiT episode on 2026-09-07. Its project quilt showed
seven `preference` tiles, among them "Apple better off without Johnny
Ive" next to "Johnny Ive's original iMac design was excellent", reading
as one person contradicting himself. They are four different podcast
hosts, and every row carried the right owner in `value.owner`.

The grouped formatter dropped the owner and printed them under a header
that says "About you", so the model was told the user holds opinions he
never expressed. The flat formatter has rendered `[owner: X]` on these
rows the whole time: one rule, two carriers, and the carrier that
dropped the name is the one whose heading makes the claim.

Measured on prod 2026-09-08: 45 self-typed rows carry an owner (39
preferences, 6 goals). TEN NAME THE USER HIMSELF, which is why moving
every owner-bearing row would fix one false statement by minting
another.
"""

import pytest

from contextquilt.services.recall_formatter import format_category_grouped

SELF = {"scott", "scott guida"}


def _p(ptype, text, owner=None, **value):
    v = {"text": text, **value}
    if owner is not None:
        v["owner"] = owner
    return (1.0, {"patch_type": ptype, "value": v})


def _block(patches, self_names=SELF):
    return format_category_grouped(patches, [], [], self_names=self_names)


def test_another_persons_preference_is_not_about_you():
    """THE BUG, exactly as it reached the model."""
    out = _block([
        _p("preference", "Apple was better off without Johnny Ive", owner="Nicholas"),
        _p("preference", "Johnny Ive's original iMac design was excellent", owner="Iain"),
    ])
    about = out.split("About you:")[1] if "About you:" in out else ""
    assert "Johnny Ive" not in about
    assert "Stated by others:" in out
    assert "- Nicholas: Apple was better off without Johnny Ive" in out
    assert "- Iain: Johnny Ive's original iMac design was excellent" in out


def test_an_unowned_preference_stays_about_you():
    """Empty owner IS the user by construction: the write path strips a
    self owner from these types precisely because it is implicit."""
    out = _block([_p("preference", "Prefers the diff before the summary")])
    assert "About you:\n- Prefers the diff before the summary" in out
    assert "Stated by others:" not in out


def test_the_user_under_their_own_name_stays_about_you():
    """Ten prod rows carry owner "Scott". Moving them would replace one
    false statement with another."""
    out = _block([_p("preference", "Prefers short meetings", owner="Scott")])
    assert "About you:\n- Prefers short meetings" in out
    assert "Stated by others:" not in out
    assert "Scott: Prefers short meetings" not in out


def test_the_self_name_match_is_case_insensitive():
    out = _block([_p("trait", "Works late", owner="SCOTT GUIDA")])
    assert "About you:\n- Works late" in out


def test_a_placeholder_owner_is_not_the_user():
    """"Speaker 2" is not evidence a preference is the user's. Two such
    rows are live. It renders under its raw label, which is honest about
    what was actually observed."""
    out = _block([_p("preference", "Wants the API frozen", owner="Speaker 2")])
    assert "About you:" not in out
    assert "- Speaker 2: Wants the API frozen" in out


def test_both_kinds_split_into_their_own_sections():
    out = _block([
        _p("preference", "Prefers async updates"),
        _p("trait", "Detail oriented", owner="Scott"),
        _p("preference", "Thinks the FTC is overreaching", owner="Leo"),
    ])
    about = out.split("About you:")[1].split("\n\n")[0]
    assert "Prefers async updates" in about and "Detail oriented" in about
    assert "Leo" not in about
    assert "- Leo: Thinks the FTC is overreaching" in out


def test_a_goal_someone_else_owns_is_named_but_stays_a_goal():
    """"Goals:" does not assert whose the way "About you" does, so the
    six owned goals gain the name in place rather than moving."""
    out = _block([_p("goal", "Ship the radar build", owner="Leo")])
    assert "Goals:\n- Leo: Ship the radar build" in out
    assert "Stated by others:" not in out


def test_the_users_own_goal_keeps_no_prefix():
    out = _block([_p("goal", "Ship the radar build", owner="Scott")])
    assert "Goals:\n- Ship the radar build" in out


def test_no_self_names_never_raises_and_states_nothing_false():
    """The lookup is allowed to fail. An empty set attributes the user's
    own row to their name, which reads oddly and asserts nothing false;
    failing the block would cost the whole recall."""
    out = format_category_grouped(
        [_p("preference", "Prefers short meetings", owner="Scott")], [], [])
    assert "About you:" not in out
    assert "- Scott: Prefers short meetings" in out


def test_the_section_is_absent_when_nobody_else_spoke():
    out = _block([_p("preference", "Prefers async updates")])
    assert "Stated by others" not in out


@pytest.mark.parametrize("owner", ["", "   ", None])
def test_blank_owners_are_treated_as_the_user(owner):
    out = _block([_p("preference", "Prefers async updates", owner=owner)])
    assert "About you:\n- Prefers async updates" in out
