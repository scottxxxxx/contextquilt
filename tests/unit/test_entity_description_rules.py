"""What we ask a model to write in an entity `description`.

THE OLD RULE WAS WRITTEN TO FIX A REAL BUG AND CAUSED ANOTHER. It said
`description` states "what this transcript SHOWS about the entity" and,
where no role was stated, "describe the conduct". That exists to stop a
profession being inferred from the topic, which is exactly how Steven
Williams became an immigration attorney. But its cure produced
descriptions of a Tuesday:

    "Participant in standup meeting"
    "QA validation tester, out of office today"
    "Mentioned in context of hardware form issue investigation"

The cost: 0 of 122 description rows across 43 people had EVER been
confirmed as the same perception, because each genuinely contained
something new. The series recorded paraphrase drift rather than
perception change, "how they're changing" said one colleague changed ten
times in thirteen days, and a semantic judge built to fix it answered
CHANGED on 47 of 47 real pairs and was right to.

Measured, 3 runs per arm on a standup transcript shaped like the
failure, one shared classifier grading both arms in one call:

    OLD   6 durable, 12 episodes, 0 nulls
    NEW  14 durable,  0 episodes, 4 nulls
"""

from contextquilt.services.schema_prompt_builder import (
    DESCRIPTION_RULES,
    build_prompt,
)

MANIFEST = {"patch_types": [{"domain_type": "commitment"}],
            "entity_types": [{"entity_type": "person"}]}


def test_the_rules_reach_the_built_prompt():
    # A rule in a constant nothing renders is a rule that does not exist.
    assert DESCRIPTION_RULES.strip()[:50] in build_prompt(MANIFEST)


def test_the_anti_inference_rule_survived_the_rewrite():
    """The half that must NOT be lost.

    This is what stops a person discussing immigration law becoming an
    immigration attorney, which is a live incident on this system and
    the reason the old wording existed at all. A rewrite that fixed the
    Tuesday problem by dropping this would trade a noisy description for
    a confidently wrong one, which is far worse.
    """
    assert "NOT thereby an immigration attorney" in DESCRIPTION_RULES
    assert "NOT thereby in finance" in DESCRIPTION_RULES


def test_null_is_offered_as_a_correct_answer_rather_than_a_failure():
    """The model filled every description before this.

    0 nulls across 18 person entities on the old rule. Most people named
    in one meeting have no durable description, so the instruction has
    to say null is RIGHT for them, not merely permitted.
    """
    assert "the value is null" in DESCRIPTION_RULES
    assert "not a failure to try" in DESCRIPTION_RULES


def test_the_month_test_is_stated_as_a_question_the_model_can_apply():
    # A rule the model can evaluate beats an adjective it has to
    # interpret. "Durable" is an adjective; "will this still be true in
    # a month" is a check.
    assert "will this still be true in a month" in DESCRIPTION_RULES


def test_the_welding_rule_is_present_and_was_the_last_17_percent():
    """The residual the first two attempts left behind.

    Every survivor of the second measured attempt had the same shape: a
    real standing with an in-flight task welded onto the end. "QA lead
    for asset management releases, validating the workspace changes" is
    half durable and half Tuesday, and the half that rots is the half a
    reader trusts. Adding this rule took episodes from 3 to 0.
    """
    assert "DO NOT WELD TODAY ONTO A ROLE" in DESCRIPTION_RULES


def test_one_action_is_not_a_role():
    # Without this the model promotes a single observed act into a
    # standing, which is inference wearing a description's clothes.
    assert "ONE ACTION IS NOT A ROLE" in DESCRIPTION_RULES


def test_the_wrong_examples_are_real_production_strings():
    """Examples taken from what actually shipped, not invented ones.

    A model shown a plausible-looking wrong example learns less than one
    shown the exact sentence the system has been producing, and an
    invented example risks teaching against a failure that never occurs.
    """
    for real in ("Participant in standup meeting",
                 "QA validation tester, out of office today"):
        assert real in DESCRIPTION_RULES


def test_the_instruction_carries_no_dash_for_the_model_to_copy():
    # A model copies the punctuation it is shown, and this text is read
    # on every extraction.
    assert "—" not in DESCRIPTION_RULES and "–" not in DESCRIPTION_RULES


def test_the_inline_shape_hint_no_longer_asks_for_this_transcript():
    """The same instruction in miniature, and the model reads it first.

    The per-entity shape line said `"description": "<brief context from
    this transcript>"`, which asks for a meeting record in six words. A
    rewrite of the rules that left this in place would have been arguing
    with itself.
    """
    prompt = build_prompt(MANIFEST)
    assert "brief context from this transcript" not in prompt
    assert "what is durably true of them, or null" in prompt


def test_a_manifest_can_still_override_the_whole_entity_guidance():
    # Per-app taxonomy stays owned by the manifest. Verified against
    # prod before this change: no registered manifest overrides
    # entity_guidance today, so the default reaches every app.
    out = build_prompt(dict(MANIFEST, extraction_prompt_guidance={
        "entity_guidance": "APP SPECIFIC RULES ONLY"}))
    assert "APP SPECIFIC RULES ONLY" in out
    assert "durably TRUE OF THIS PERSON" not in out
