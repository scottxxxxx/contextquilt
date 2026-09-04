"""A moment is a CHOICE. The rule has three carriers and needs all three.

The type kept collecting things that merely happened while somebody was
in the room. "Identified a rendering bug on the homepage" is a finding.
"Reported that the deployment is blocked" is a status. "Looked up project
assignments in real time" is a thing that occurred. All three survived
every rule the type had, because all three are conduct, are checkable
against a transcript, and are not character verdicts.

What separates the good rows from those is that the good ones are
CHOICES: the person did something when something else was available.
Asked for the breakdown when agreeing was available. Moved off the date
when holding was available. Said the critique was about the design when
just delivering it was available.

THIS FILE EXISTS FOR THE CARRIER COUNT, not for the wording. Tonight
this codebase found the same shape twice in one session: a sanitizer
wired into one of two producers, and before that a set of rules wired
into the other one. A semantic rule that lives in the extraction prompt
but not in the judge is enforced only on the model that already ignores
it, and a rule in the judge but not in the manifest is invisible to the
contrast the judge reasons over. Three carriers, and the test counts
them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from contextquilt.services import behavior_classifier, behavior_extraction  # noqa: E402

MANIFEST = json.loads((ROOT / "init-db" / "11_shouldersurf_schema.json").read_text())


def _behavior_type() -> dict:
    for t in MANIFEST.get("patch_types", []):
        if t.get("domain_type") == "behavior":
            return t
    raise AssertionError("the behavior type is not declared in the manifest")


def _carriers() -> dict[str, str]:
    """The three places the rule has to exist, and what each one reaches.

    Built as a dict rather than three separate tests so that adding a
    fourth carrier later is one line here instead of a new test somebody
    forgets to write.
    """
    t = _behavior_type()
    return {
        "the extraction prompt (reaches the model that writes the rows)":
            behavior_extraction.BEHAVIOR_SYSTEM,
        "the manifest description (reaches the judge as contrast)":
            t["description"],
        "the manifest guidance (reaches the schema-driven prompt builder)":
            t["extraction_rules"]["guidance"],
        "the classifier system prompt (reaches the judge that drops rows)":
            _judge_own_words(),
    }


def _judge_own_words() -> str:
    """The judge prompt with the embedded manifest definitions REMOVED.

    Written this way after the first version of this file passed a
    sabotage it should have failed. `build_classifier_system` embeds the
    manifest's own descriptions as contrast, and the manifest now states
    the choice rule, so asserting against the whole prompt found the
    words no matter what the judge's own instruction said. Neutering the
    judge's paragraph entirely left all fifteen tests green.

    An instrument that fires cleanly and answers a different question is
    worse than no instrument, so this one is pointed at the judge's
    hardcoded text alone.
    """
    full = behavior_classifier.build_classifier_system(MANIFEST)
    definitions = behavior_classifier._definitions(
        MANIFEST, behavior_classifier.classifier_types(MANIFEST)
    )
    assert definitions and definitions in full, (
        "the definitions block is no longer embedded the way this test assumes; "
        "re-derive how the manifest text reaches the judge before trusting this"
    )
    return full.replace(definitions, "")


@pytest.mark.parametrize("where", list(_carriers()))
def test_every_carrier_states_the_choice_rule(where):
    """The rule is on this carrier at all."""
    body = _carriers()[where].lower()
    assert "choice" in body, f"{where} does not mention a choice"


@pytest.mark.parametrize("where", list(_carriers()))
def test_every_carrier_gives_the_test_in_its_operative_form(where):
    """"A choice" alone is a slogan. The operative form is that something
    else was AVAILABLE, which is what a reader or a model can apply."""
    body = _carriers()[where].lower()
    assert "available" in body, (
        f"{where} says choice without saying what makes it one"
    )


@pytest.mark.parametrize("where", [
    "the extraction prompt (reaches the model that writes the rows)",
    "the manifest guidance (reaches the schema-driven prompt builder)",
    "the classifier system prompt (reaches the judge that drops rows)",
])
def test_the_prompting_carriers_name_the_failing_shapes(where):
    """A rule with no counter-example is a rule the model reads past.

    Doc 19.8: contrast is what makes a definition usable. Each of these
    names at least one thing that LOOKS like conduct and is not.
    """
    body = _carriers()[where].lower()
    assert "looking something up" in body or "looked up" in body
    assert "relay" in body


def test_the_manifest_description_still_refuses_character_verdicts():
    """The new rule must not have displaced the old one. Guardrail 12b
    predates this and is the reason the type is safe to serve at all."""
    d = _behavior_type()["description"].lower()
    assert "never a verdict" in d
    assert "who a person is" in d


def test_the_description_rejects_mere_occurrence():
    """The specific gap this closes, stated on the wire rather than only
    in a prompt: a thing that happened in someone's presence is not
    theirs just because they were there."""
    d = _behavior_type()["description"].lower()
    assert "occurred in" in d or "merely occurred" in d


def test_the_manifest_version_was_bumped():
    """The judge is shown the manifest's own text, and the registered
    copy on prod is what it reads. A description change that never
    re-registers is a change nobody sees, which is the whole reason the
    ops rules require a version bump and a re-registration."""
    assert MANIFEST["version"] >= 15


def test_the_classifier_still_carries_its_earlier_two_tests():
    """This adds a third test and must not have replaced the first two.

    The owner test and the remove-the-name test were measured at roughly
    92% agreement with a hand read; losing either to gain this one would
    be a trade nobody priced.
    """
    body = behavior_classifier.build_classifier_system(MANIFEST).lower()
    assert "remove the owner's name" in body
    assert "the row is about its owner" in body
