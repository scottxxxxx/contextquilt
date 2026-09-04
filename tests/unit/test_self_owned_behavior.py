"""The behavior corpus is about the people the user works with, not the user.

`BEHAVIOR_SYSTEM` says it outright: "Never record an observation about the
speaker marked (you). That is the user, and this corpus is about the
people they work with."

On 2026-09-04 production held 130 active behavior rows about the users
themselves. 56 belonged to one user, 32 of those written by the main
extraction chain, the newest a day old.

WHY THE DEDICATED LANE WAS CLEAN AND THE MAIN CHAIN WAS NOT.
`behavior_extraction.parse_behavior_response` builds its own self set
from `user_label` and has been handed one since it shipped. It works: of
that user's 56, the 24 the lane produced are all dated 2026-08-17 and it
has made none since. The main chain reached `_is_self_owner` with no
label, so the only forms it could recognise were the markers, and a bare
name walked through.

One rule, two carriers, wired to one. CLAUDE.md already records this
exact shape for this exact lane pointing the other way: the sanitizers
were wired into the main chain and the lane ran without them until
2026-09-01. Learning it twice in opposite directions is why the label is
now a parameter instead of another comment.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import ast  # noqa: E402

from contextquilt.services.extraction_schema import (  # noqa: E402
    _is_self_owner,
    sanitize_behavior_observations,
)

# Verbatim from prod: rows about the user that the main chain wrote.
SELF_ROWS = [
    ("Immediately grasped the business opportunity in the AI outage; reframed it "
     "as a sales pitch for local AI models by calculating ROI", "Scott"),
    ("When asked a technical question about Claude Code settings, immediately "
     "tested the answer empirically rather than relying on documentation", "Scott"),
    ("Explicitly clarified that technical feedback is directed at the bot and "
     "system design, not at team members personally", "Scott"),
]


def _content(text, owner, ptype="behavior"):
    return {"patches": [{"type": ptype, "value": {"text": text, "owner": owner}}]}


# ----------------------------------------------------------------------
# The predicate
# ----------------------------------------------------------------------

def test_the_marker_forms_are_caught_without_a_label():
    """Unchanged behavior. These worked before and must keep working."""
    for owner in ("(you)", "you", "me", "self", "myself", "Scott (you)"):
        assert _is_self_owner(owner) is True, owner


def test_a_bare_name_is_NOT_caught_without_a_label():
    """The gap, stated as a test rather than as a comment.

    This is not a bug in the predicate, it is the reason the label has to
    reach it. Without a label there is nothing here that could tell
    "Scott" the user from "Scott" a colleague of somebody else.
    """
    assert _is_self_owner("Scott") is False


@pytest.mark.parametrize("text,owner", SELF_ROWS)
def test_a_bare_name_IS_caught_with_the_label(text, owner):
    assert _is_self_owner(owner, "Scott") is True


def test_the_match_is_exact_not_a_substring():
    """This rule DELETES rows, so a substring test would drop a real
    colleague whose name contains the user's."""
    assert _is_self_owner("Scotty", "Scott") is False
    assert _is_self_owner("Scott Guida", "Scott") is False
    assert _is_self_owner("Prescott", "Scott") is False


def test_case_and_padding_do_not_matter():
    assert _is_self_owner("  scott  ", "Scott") is True
    assert _is_self_owner("SCOTT", "scott") is True


@pytest.mark.parametrize("label", [None, "", "   ", 7])
def test_a_missing_or_malformed_label_falls_back_to_marker_forms(label):
    """Half the ingests on prod carry no `user_label`. Those must keep
    today's behavior rather than raising or dropping everything."""
    assert _is_self_owner("Scott", label) is False
    assert _is_self_owner("(you)", label) is True


# ----------------------------------------------------------------------
# Through the sanitizer, which is what both carriers call
# ----------------------------------------------------------------------

@pytest.mark.parametrize("text,owner", SELF_ROWS)
def test_the_sanitizer_drops_a_self_row_when_given_the_label(text, owner):
    content = sanitize_behavior_observations(_content(text, owner), "Scott")

    assert content["patches"] == []
    report = content["_behavior_observations_sanitized"]
    assert report["dropped"][0]["reason"] == "self_observation"


@pytest.mark.parametrize("text,owner", SELF_ROWS)
def test_the_same_row_survives_without_a_label(text, owner):
    """The bug, pinned. This is what production was doing.

    Kept as a test rather than deleted, because it documents exactly what
    an ingest with no `user_label` still does, and half of them have none.
    """
    content = sanitize_behavior_observations(_content(text, owner))

    assert len(content["patches"]) == 1


def test_a_colleague_is_never_dropped():
    """The failure this must not cause. Vijay is not the user."""
    content = sanitize_behavior_observations(
        _content("Asked for the cost breakdown before agreeing to the vendor switch",
                 "Vijay"), "Scott")

    assert len(content["patches"]) == 1


def test_the_reason_is_self_observation_not_placeholder_owner():
    """Accurate drop reasons or the counts are useless.

    `placeholder_owner` means a diarization label like "Speaker 3" leaked
    through. `self_observation` means the corpus was pointed at the wrong
    person. Different defects, different fixes; routing the name check
    through the placeholder gate would have merged them.
    """
    content = sanitize_behavior_observations(_content(SELF_ROWS[0][0], "Scott"), "Scott")

    reasons = {d["reason"] for d in content["_behavior_observations_sanitized"]["dropped"]}
    assert reasons == {"self_observation"}


def test_only_behavior_types_are_inspected():
    """A commitment the user owes is entirely legitimate and common."""
    content = sanitize_behavior_observations(
        _content("Send the revised pricing sheet by Thursday", "Scott",
                 ptype="commitment"), "Scott")

    assert len(content["patches"]) == 1


# ----------------------------------------------------------------------
# The invariant the bug was actually about
# ----------------------------------------------------------------------

def test_BOTH_carriers_pass_the_label():
    """One rule, two carriers. This is the test that was missing.

    The predicate tests above all pass a label directly, so every one of
    them would stay green while a call site quietly failed to supply one,
    which is precisely how production ran for weeks. What went wrong was
    never the rule; it was that only one of the two producers handed it
    what it needed.

    Source-level on purpose, and the limit is worth naming: this reads
    worker.py rather than executing it, because the defect is a MISSING
    ARGUMENT at a call site, and an executing test of the sanitizer
    cannot see which arguments its callers chose to pass.
    """
    src = (Path(__file__).resolve().parents[2] / "src" / "worker.py").read_text()
    tree = ast.parse(src)
    carriers = {
        "handle_meeting_summary": "the main extraction chain",
        "_extract_behavior_observations": "the dedicated behavior lane",
    }
    for name, description in carriers.items():
        fn = next(
            (n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name),
            None,
        )
        assert fn is not None, f"{name} not found in worker.py"
        body = ast.get_source_segment(src, fn) or ""
        assert "sanitize_behavior_observations(" in body, (
            f"{description} no longer calls the sanitizer at all"
        )
        args = body.split("sanitize_behavior_observations(", 1)[1].split(")", 1)[0]
        assert "user_label" in args, (
            f"{description} calls the sanitizer without user_label, so a bare "
            f"name owner is invisible to it. That is the bug this file exists for."
        )
