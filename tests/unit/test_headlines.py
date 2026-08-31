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
