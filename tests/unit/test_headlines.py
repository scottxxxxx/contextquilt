"""The one line that fits on cloth, and the six rules it must obey.

Woven handoff section 6.3. The `fact` is the record; the `headline` is
what a tile can hold, and it is WRITTEN rather than truncated. A cut
string on a tile reads as a broken sentence, which is the most visible
thing on that screen, so a headline that breaks a rule is REFUSED
rather than repaired: repairing would mean truncating, which is the
exact thing 6.3 forbids.

Two of the six are checkable rather than merely assertable, and they
are the ones that matter. A number in the headline must appear in the
fact, because an invented figure would put a made-up dollar amount on
someone's home screen in serif, looking certain. And a fact carrying a
concrete figure should not yield a headline that drops it, because
"the revenue goal" is the vaguer of the two.
"""

import pytest

from contextquilt.services.headlines import (
    DASH,
    DROPPED_FIGURE,
    ELLIPSIS,
    EMPTY,
    IMPERATIVE,
    INVENTED_NUMBER,
    NOTHING_ASKED,
    NOTHING_RETURNED,
    NO_JSON,
    MAX_HEADLINE_CHARS,
    TERMINAL_PERIOD,
    TOO_LONG,
    RETRY_SYSTEM,
    apply_retry,
    partition_by_self_headline as da_partition,
    self_headline as da_self,
    build_retry_content,
    build_user_content,
    parse,
    why_invalid,
)

FACT = ("Target market of 60-67% small firms (3-5 attorneys) is ideal: low "
        "customer acquisition friction, and $3M ARR potential.")


def patch(pid="p1", text=FACT, ptype="takeaway"):
    return {"patch_id": pid, "patch_type": ptype, "value": {"text": text}}


# --------------------------------------------------------------------
# The prototype's own headline must pass. If it does not, the validator
# is wrong rather than the design.
# --------------------------------------------------------------------

@pytest.mark.parametrize("headline,fact", [
    ("60-67% small firms is the sweet spot", FACT),
    ("Zero data retention. No exceptions.",
     "Data security and privacy: zero data retention with AI providers."),
    ("Privacy is the differentiator",
     "Competitors ship insecure implementations; privacy will be the "
     "key competitive differentiator."),
    ("The business plan",
     "Camino Caseworks business plan documenting market opportunity."),
])
def test_the_prototypes_own_headlines_are_accepted(headline, fact):
    assert why_invalid(headline, fact) is None, headline


# --------------------------------------------------------------------
# The two content rules, which are the reason this is code
# --------------------------------------------------------------------

def test_an_invented_number_is_refused():
    """The failure that would put a made up dollar amount on a tile.

    `who_they_are` refuses a summary that invents a number for the same
    reason; this is that rule applied one object down.
    """
    assert why_invalid("Achieve $5M ARR next year", FACT) == INVENTED_NUMBER


def test_a_figure_present_in_the_fact_survives_into_the_headline():
    assert why_invalid("The revenue goal", FACT) == DROPPED_FIGURE
    # And a fact with no figure imposes no such requirement.
    assert why_invalid("Privacy is the differentiator",
                       "Privacy will be the differentiator.") is None


def test_a_range_is_one_figure_and_is_not_split():
    """The stitch label bug, prevented here rather than repeated.

    "60-67%" is a single figure. A validator that treated the hyphen as
    a separator would see "60" and "67" as two numbers and could refuse
    a correct headline, or accept an invented one.
    """
    assert why_invalid("60-67% small firms is the sweet spot", FACT) is None


# --------------------------------------------------------------------
# The four form rules
# --------------------------------------------------------------------

@pytest.mark.parametrize("headline,expected", [
    ("", EMPTY),
    ("x" * (MAX_HEADLINE_CHARS + 1), TOO_LONG),
    ("Small firms, the sweet spot...", ELLIPSIS),
    ("Small firms, the sweet spot…", ELLIPSIS),
    ("Small firms are ideal.", TERMINAL_PERIOD),
    ("Small firms — the sweet spot", DASH),
    ("Small firms – the sweet spot", DASH),
])
def test_form_rules(headline, expected):
    assert why_invalid(headline, "Small firms are ideal for us.") == expected


@pytest.mark.parametrize("headline", [
    "Remember to call the small firms",
    "Ask him about the pricing model",
    "Follow up on the retention numbers",
    "Make sure privacy is covered",
    "You should target small firms",
])
def test_the_quilt_states_it_does_not_nag(headline):
    # 6.3: never address the user in the imperative.
    assert why_invalid(headline, "Small firms are the target.") == IMPERATIVE


def test_a_terminal_period_is_only_wrong_on_a_single_sentence():
    """Section 6.3 says no terminal period; the prototype ships one.

    "Zero data retention. No exceptions." is two clipped sentences whose
    stop is deliberate. What 6.3 guards against is a headline that reads
    as a sentence that got CUT OFF, which is a single sentence with a
    stop on the end. This validator refused the design's own headline
    until it was run against it.
    """
    assert why_invalid("Zero data retention. No exceptions.",
                       "Zero data retention with AI providers.") is None
    assert why_invalid("Small firms are ideal.",
                       "Small firms are ideal for us.") == TERMINAL_PERIOD


def test_a_mid_sentence_verb_is_not_an_imperative():
    # "ask" as a noun mid-phrase must not trip the check, or half the
    # legitimate headlines about requests get refused.
    assert why_invalid("The pricing ask is unresolved",
                       "The pricing ask remains unresolved.") is None


def test_exactly_48_characters_is_allowed():
    # The cap is a limit, not a target, and an off by one here silently
    # refuses every headline that used the space it was given.
    text = "a" * MAX_HEADLINE_CHARS
    assert why_invalid(text, "some fact with no figures at all") is None


# --------------------------------------------------------------------
# Parsing a model response
# --------------------------------------------------------------------

def test_only_valid_headlines_survive_and_refusals_are_counted():
    patches = [patch("a"), patch("b"), patch("c")]
    out = parse({"headlines": [
        {"id": "a", "headline": "60-67% small firms is the sweet spot"},
        {"id": "b", "headline": "Achieve $9M ARR"},          # invented
        {"id": "c", "headline": "x" * 60},                    # too long
    ]}, patches)
    assert out["headlines"] == {"a": "60-67% small firms is the sweet spot"}
    assert out["refused"] == {INVENTED_NUMBER: 1, TOO_LONG: 1}


def test_a_hallucinated_id_is_downgraded_not_trusted():
    # The resolved_commitments pattern: an id the model made up is a
    # refusal, never a write against whatever it happens to collide with.
    out = parse({"headlines": [{"id": "nope", "headline": "Anything"}]},
                [patch("a")])
    assert out["headlines"] == {}
    assert out["refused"] == {"unknown_id": 1}


@pytest.mark.parametrize("content", [None, {}, {"headlines": None},
                                     {"headlines": ["not a dict"]}, "prose"])
def test_a_malformed_response_yields_nothing_rather_than_raising(content):
    # The client does not enforce json_schema on the wire, so a prose
    # answer is a real possibility and must cost one call, not a crash.
    out = parse(content, [patch("a")])
    assert out["headlines"] == {}


def test_the_prompt_carries_no_dash_for_the_model_to_copy():
    # A model copies the punctuation it is shown, and rule 7 forbids
    # dashes in the output, so the instruction itself must not use one.
    from contextquilt.services.headlines import SYSTEM
    assert "—" not in SYSTEM and "–" not in SYSTEM


def test_the_batch_prompt_skips_textless_patches():
    body = build_user_content([patch("a"), {"patch_id": "b", "value": {}}])
    assert "id: a" in body and "id: b" not in body


# --------------------------------------------------------------------
# An empty result has to name itself
# --------------------------------------------------------------------

def test_a_value_arriving_as_a_json_string_still_reaches_the_prompt():
    """The bug that shipped, and the third instance of it in one night.

    `value` is JSONB and asyncpg hands it back as a JSON STRING. Both
    readers here checked `isinstance(value, dict)`, so every patch
    looked textless, `build_user_content` emitted a prompt with NO facts
    in it, and the model correctly returned an empty list. The first
    prod dry run reported "0 headlines, 0 refused, $0.00", which reads
    exactly like a healthy batch with nothing to do.
    """
    import json as _json
    p = patch("a")
    p["value"] = _json.dumps(p["value"])
    body = build_user_content([p])
    assert "id: a" in body and "Target market" in body


def test_a_prompt_with_no_facts_is_reported_rather_than_silent():
    out = parse({"headlines": []}, [])
    assert out["refused"] == {NOTHING_ASKED: 1}


def test_an_empty_model_answer_is_reported_rather_than_silent():
    out = parse({"headlines": []}, [patch("a")])
    assert out["headlines"] == {}
    assert out["refused"] == {NOTHING_RETURNED: 1}


def test_prose_instead_of_json_is_reported_rather_than_silent():
    # The client does not enforce json_schema on the wire, so a prose
    # answer is a real outcome and must be distinguishable from an
    # honest empty one.
    assert parse("Here are your headlines!", [patch("a")])["refused"] == {NO_JSON: 1}


def test_a_healthy_batch_reports_no_batch_level_reason():
    out = parse({"headlines": [
        {"id": "p1", "headline": "60-67% small firms is the sweet spot"},
    ]}, [patch("p1")])
    assert out["headlines"]
    for reason in (NO_JSON, NOTHING_RETURNED, NOTHING_ASKED):
        assert reason not in out["refused"]


# --------------------------------------------------------------------
# The second pass: a rewrite, never a repair
# --------------------------------------------------------------------

def test_a_refused_line_comes_back_with_its_attempt_and_reason():
    """Enough to quote the failure back, which is why the retry works.

    Measured on the hard residue, patches already refused once: 16%
    accepted on a single pass, 52% after a retry naming the failure.
    Models count characters badly and REVISE well. A stricter first
    instruction was tried instead and did WORSE, 6% against 8%, because
    "at most six words" pushed numbers into words and made the lines
    longer.
    """
    out = parse({"headlines": [{"id": "p1", "headline": "x" * 60}]}, [patch("p1")])
    assert out["retryable"] == [
        {"id": "p1", "attempt": "x" * 60, "reason": TOO_LONG}]


def test_an_empty_attempt_is_not_retryable():
    # Nothing to quote back and nothing was learned, so a retry would
    # be a second identical request at twice the price.
    out = parse({"headlines": [{"id": "p1", "headline": "   "}]}, [patch("p1")])
    assert out["retryable"] == []
    assert out["refused"] == {EMPTY: 1}


def test_a_hallucinated_id_is_not_retryable():
    # There is no fact to rewrite against.
    out = parse({"headlines": [{"id": "nope", "headline": "x" * 60}]}, [patch("p1")])
    assert out["retryable"] == []


def test_the_retry_prompt_states_the_actual_length():
    """The one thing the model could not check for itself.

    Telling it the limit again is not information. Telling it that its
    line was 60 characters is.
    """
    body = build_retry_content(
        [{"id": "p1", "attempt": "x" * 60, "reason": TOO_LONG}], [patch("p1")])
    assert "(60 characters)" in body
    assert "fact: " in body and TOO_LONG in body


def test_the_retry_prompt_carries_no_dash_for_the_model_to_copy():
    # Rule 7 forbids dashes in the output, and a model copies the
    # punctuation it is shown.
    assert "—" not in RETRY_SYSTEM and "–" not in RETRY_SYSTEM


def test_the_retry_instruction_says_rewrite_and_forbids_truncating():
    """The distinction 6.3 turns on, stated in the prompt itself.

    A retry that cut the tail off would be the forbidden repair wearing
    a second call's clothes, so the instruction has to rule it out
    rather than merely not ask for it.
    """
    lowered = RETRY_SYSTEM.lower()
    assert "never truncate" in lowered
    assert "ellipsis" in lowered


def test_a_retried_line_is_still_validated_and_can_still_be_refused():
    # The second pass earns nothing if it is trusted more than the
    # first. An invented number in a rewrite is still an invented number.
    out = parse({"headlines": [{"id": "p1", "headline": "Achieve $9M ARR"}]},
                [patch("p1")])
    assert out["headlines"] == {} and out["refused"] == {INVENTED_NUMBER: 1}


def test_the_retry_only_ever_adds_to_the_first_pass():
    """Found by sabotage, and only because the result surprised me.

    Swapping the merge for an assignment discards every line the first
    pass got right and keeps only the recovered ones, and the whole
    suite stayed GREEN: the worker tests read source, and the one
    written for this checked the exception path rather than the success
    path. So the merge moved into the service where a test can execute
    it. A retry that loses more than it recovers is the shape of every
    optimisation that ships a regression.
    """
    first = {"headlines": {"a": "Kept from pass one"}, "refused": {TOO_LONG: 1},
             "retryable": [{"id": "b", "attempt": "x" * 60, "reason": TOO_LONG}]}
    second = {"headlines": {"b": "Recovered on pass two"}, "refused": {}}
    merged = apply_retry(first, second)
    assert merged["headlines"] == {"a": "Kept from pass one",
                                   "b": "Recovered on pass two"}
    assert merged["recovered"] == 1


def test_a_retry_that_recovers_nothing_still_keeps_pass_one():
    merged = apply_retry({"headlines": {"a": "Kept"}, "refused": {TOO_LONG: 1}},
                         {"headlines": {}, "refused": {TOO_LONG: 1}})
    assert merged["headlines"] == {"a": "Kept"}
    assert merged["recovered"] == 0


def test_only_the_surviving_refusal_is_reported():
    # A line refused then rewritten was not refused. Counting both would
    # say a batch failed twice as often as it did, and the refusal
    # counts are the signal for whether the writer is improving.
    merged = apply_retry({"headlines": {}, "refused": {TOO_LONG: 5}},
                         {"headlines": {"a": "Short enough"}, "refused": {}})
    assert merged["refused"] == {}


# --------------------------------------------------------------------
# A fact that is already a valid headline IS the headline
# --------------------------------------------------------------------

def test_a_short_valid_fact_is_its_own_headline():
    """Measured on the residue, 2026-09-01.

    Of 123 tileable patches carrying no headline, 21 were facts that
    pass every rule unchanged: "Kevin Thompson case", "Boland case",
    "Rivera case". They had been through the writer TWICE and come back
    empty both times, because the model was being asked to improve on a
    line that was already correct.

    The gate makes this a product bug rather than a cost one: no
    headline means no tile, so those memories could not appear in the
    quilt at all.
    """
    assert da_self("Kevin Thompson case") == "Kevin Thompson case"
    assert da_self("Boland case") == "Boland case"


def test_the_same_validator_judges_an_adopted_fact():
    """Nothing is waived for being original.

    A fact adopted here must not be one the writer would have been
    refused for, or the rule becomes "48 characters unless we thought of
    it first".
    """
    assert da_self("x" * 60) is None                      # too long
    assert da_self("Kevin Thompson — the case") is None   # a dash
    assert da_self("Remember to call Kevin") is None      # imperative
    assert da_self("The case is closed.") is None         # cut sentence


def test_a_fact_carrying_a_figure_still_passes_as_itself():
    # It cannot drop a figure it already contains, so the figure rules
    # are satisfied trivially rather than skipped.
    assert da_self("Anderson owes $2,300") == "Anderson owes $2,300"


@pytest.mark.parametrize("bad", [None, "", "   "])
def test_an_empty_fact_is_not_a_headline(bad):
    """Rejected by the VALIDATOR, not by a guard in front of it.

    The first version had its own empty check. A sabotage removing that
    check changed nothing, because `why_invalid` already answers EMPTY
    for a blank line. A branch no test can distinguish is not a
    safeguard, it is a second opinion nobody asked for, so it is gone
    and this test now covers the path that actually decides.
    """
    assert da_self(bad) is None


def test_the_partition_sends_only_the_rest_to_the_model():
    """The cheaper half of the same change.

    A fact that is its own headline costs no call and cannot be refused,
    and every batch gets smaller.
    """
    free, rest = da_partition([
        {"patch_id": "a", "value": {"text": "Rivera case"}},
        {"patch_id": "b", "value": {"text": "y" * 60}},
    ])
    assert free == {"a": "Rivera case"}
    assert [p["patch_id"] for p in rest] == ["b"]


def test_the_partition_handles_a_json_string_value():
    # The JSONB trap, on the newest code path rather than the oldest.
    import json as _json
    free, rest = da_partition([
        {"patch_id": "a", "value": _json.dumps({"text": "Glass case"})}])
    assert free == {"a": "Glass case"} and rest == []


def test_a_headline_with_a_gendered_pronoun_is_refused_and_the_writer_is_told():
    from contextquilt.services.headlines import GENDERED, SYSTEM, why_invalid
    assert why_invalid("Verified her Carta access on the call", "Verified her Carta access on the call") == GENDERED
    assert why_invalid("Verified Carta access on the call", "Verified Carta access on the call") is None
    assert "Never use a gendered pronoun for anyone" in SYSTEM
