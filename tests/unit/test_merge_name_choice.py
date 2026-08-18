"""A merge honours which name survives, and can overturn a durable no.

Scott merged Pallavi with Pallavi Kandanu in the app, chose the full name
in SS's "Keep the name" picker, and the survivor still read "Pallavi".
The app was not ignoring him: `PeopleMergeRequest` had no field to
receive the choice, so the survivor always kept its own name.

Worse, that merge had never landed at all. A keep-separate he recorded on
2026-08-07 refused it, twice, and there was no way to say he had changed
his mind.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

MAIN = (ROOT / "src" / "main.py").read_text()


def _merge_body():
    return MAIN.split("async def merge_people")[1].split("\n@app.")[0]


def test_the_request_can_carry_a_name_choice():
    model = MAIN.split("class PeopleMergeRequest")[1].split("class ")[0]
    assert "canonical_name: Optional[str]" in model


def test_the_request_can_overturn_a_recorded_separation():
    model = MAIN.split("class PeopleMergeRequest")[1].split("class ")[0]
    assert "override_separation: Optional[bool]" in model


def test_identity_does_not_move_when_only_the_name_changes():
    """The 88-meeting row stays canonical and keeps its id. Clients hold
    it and insights reference it; folding 88 meetings into a 4-meeting
    row to acquire a surname would be a bad trade for a rename."""
    body = _merge_body()
    rename = body.split("if (req.canonical_name")[1]
    assert "UPDATE entities SET name = $1 WHERE entity_id = $2::uuid" in rename
    assert "merged_into" not in rename.split("alias_rows")[0]


def test_the_old_name_becomes_an_alias():
    """Recall by either spelling has to keep working after the swap."""
    rename = _merge_body().split("if (req.canonical_name")[1]
    assert "INSERT INTO entity_aliases" in rename


def test_an_unrecognised_name_is_refused_not_ignored():
    """Silently keeping the old name is the bug being fixed. An
    unrecognised choice must not do the same thing by another route."""
    body = _merge_body()
    assert "UNKNOWN_CANONICAL_NAME" in body
    assert "status_code=422" in body


def test_the_choice_may_be_any_alias_of_the_merged_set():
    """It runs LAST, after the losers' names are already aliases, so the
    chosen name can be one of theirs. That ordering is the mechanism."""
    body = _merge_body()
    i_alias = body.index("# 1. The loser's name becomes an alias")
    i_rename = body.index("if (req.canonical_name")
    assert i_alias < i_rename, "the rename must run after the aliases exist"


def test_overturning_a_separation_deletes_it():
    """The user has just contradicted it. Leaving the row would refuse
    the next merge for a reason they already answered."""
    body = _merge_body()
    over = body.split("if conflicts and req.override_separation:")[1].split("if conflicts:")[0]
    assert "DELETE FROM entity_separations" in over
    assert "separation_overturned" in over


def test_the_refusal_still_fires_without_the_override():
    """A durable no should be hard to overturn, not impossible, and
    never accidental."""
    body = _merge_body()
    assert "SEPARATION_CONFLICT" in body
    i_over = body.index("if conflicts and req.override_separation:")
    i_raise = body.index("SEPARATION_CONFLICT")
    assert i_over < i_raise, "the override must be checked before the refusal"


def test_the_response_reports_the_final_name():
    """Echoing the name captured before the merge would tell a client the
    choice was ignored, or that it landed when it did not."""
    assert '"canonical_name": renamed_to or canonical["name"],' in MAIN
    assert '"renamed": renamed_to is not None,' in MAIN


def test_person_patches_follow_the_new_name():
    """A patch whose text is an old spelling is still this person's
    patch, and after the swap it should say what the entity says."""
    rename = _merge_body().split("if (req.canonical_name")[1]
    assert "UPDATE context_patches cp" in rename
    assert "updated_at = NOW()" in rename
