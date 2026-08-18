"""Unit tests for the closure evidence classifier.

The fixtures are REAL evidence strings taken off prod on 2026-08-17, not
invented ones. Every string marked BELIEVED here actually closed and
archived a live commitment, and would have appeared on the owner's page
as something they delivered.
"""

from __future__ import annotations

from contextquilt.services import closure_evidence as ce


# ---------------------------------------------------------------
# The receipts. These closed real items.
# ---------------------------------------------------------------

def test_promise_restated_is_not_a_delivery():
    """The purest case: the item closed because the promise was made
    again. Doc 16 5.12 already says a restatement is not an advance."""
    got = ce.classify_closure(
        owner="Suresh Muchakurti",
        evidence=(
            "Suresh commits to remind Liz about threshold logic dependency "
            "requirements before the 11:30 call"
        ),
    )
    assert got["band"] == ce.BELIEVED
    assert ce.REASON_FUTURE_ONLY in got["reasons"]
    # The owner IS named here, so that test must not be what saved it.
    assert got["owner_named"] is True


def test_setting_a_date_is_not_a_delivery():
    """Closed because somebody set an ETA. That is a re_dated item."""
    got = ce.classify_closure(
        owner="Sukumar Gurugubelli",
        evidence="Suresh stated 'I will just add ETA for Monday' for endpoint integration with core",
    )
    assert got["band"] == ce.BELIEVED
    assert ce.REASON_FUTURE_ONLY in got["reasons"]


def test_someone_elses_activity_does_not_close_your_item():
    """Karthik's standup item closed on Scott's unrelated agent testing."""
    got = ce.classify_closure(
        owner="Karthik",
        evidence=(
            "Scott commits to testing the Happy Path agent today: "
            "'I'll do the test, as I said today'"
        ),
    )
    assert got["band"] == ce.BELIEVED
    assert ce.REASON_OWNER_NOT_NAMED in got["reasons"]


def test_future_tense_quote_is_not_a_delivery():
    got = ce.classify_closure(
        owner="Scott Guida",
        evidence=(
            "Speaker 1 confirms they will 'check with my backend team' on both "
            "KB Retrieval search partial match behavior"
        ),
    )
    assert got["band"] == ce.BELIEVED


# ---------------------------------------------------------------
# The other side: real completions must still close.
# ---------------------------------------------------------------

def test_named_owner_plus_past_tense_closes():
    got = ce.classify_closure(
        owner="Sukumar",
        evidence="MCP server code details shared with Sukumar; Sukumar confirms 'I did the integration'",
    )
    assert got["band"] == ce.CONFIDENT
    assert got["reasons"] == []


def test_plain_completion_report_closes():
    got = ce.classify_closure(
        owner="Sukumar",
        evidence="Sukumar sent out the invite for the HR intake call on Monday",
    )
    assert got["band"] == ce.CONFIDENT


def test_speech_act_verbs_are_not_completion_evidence():
    """The bug the dry run caught. 'Confirmed' describes somebody
    speaking, and a sentence can confirm a future perfectly well. All
    three of these closed real items and all three are promises."""
    for owner, evidence in (
        ("Karthik", "Karthik confirmed he will provide the HDK today by end of day"),
        ("Vijay", "Vijay confirmed he will lead the call later today"),
        ("Srikanth", "Srikanth confirmed he is working on the attachment download issue"),
    ):
        got = ce.classify_closure(owner=owner, evidence=evidence)
        assert got["band"] == ce.BELIEVED, evidence
        assert ce.REASON_NO_COMPLETION_LANGUAGE in got["reasons"]


def test_speech_act_list_stays_out_of_the_completion_list():
    """Pins the reasoning so it cannot be undone in good faith later."""
    overlap = set(ce.SPEECH_ACT_MARKERS) & set(ce.PAST_MARKERS)
    assert not overlap, f"speech acts leaked into completion markers: {overlap}"


def test_mixed_tense_asks_rather_than_guessing():
    """Completion language AND forward language in one string, with no
    way to tell which clause owns THIS item without parsing.

    The receipt: "Vijay confirmed he will lead the call later today.
    Meeting agenda already prepared." closed an item about a call that
    had not happened, because "already" matched the agenda. Ambiguity
    routes to a confirm tap, never to a fabricated delivery."""
    got = ce.classify_closure(
        owner="Vijay",
        evidence=(
            "Vijay confirmed he will lead the call later today. "
            "Meeting agenda already prepared with team."
        ),
    )
    assert got["band"] == ce.BELIEVED
    assert ce.REASON_MIXED_TENSE in got["reasons"]


def test_unambiguous_completion_with_no_forward_language_closes():
    got = ce.classify_closure(
        owner="Pallavi",
        evidence=(
            "Pallavi and Vijay completed LLM response API integration for "
            "screenshot-based troubleshooting"
        ),
    )
    assert got["band"] == ce.CONFIDENT
    assert got["reasons"] == []


def test_present_continuous_is_not_a_delivery():
    """"are being shared" is work in flight, not work done."""
    got = ce.classify_closure(
        owner="Sukumar",
        evidence="the MCP server code details are being shared with Sukumar for deployment",
    )
    assert got["band"] == ce.BELIEVED


# ---------------------------------------------------------------
# Guards.
# ---------------------------------------------------------------

def test_empty_evidence_is_never_confident():
    for bad in ("", "   ", None):
        got = ce.classify_closure(owner="Vijay", evidence=bad)
        assert got["band"] == ce.BELIEVED
        assert got["reasons"] == [ce.REASON_NO_EVIDENCE]


def test_missing_owner_does_not_vote():
    """A self owned item carries no owner string. That is a question that
    does not apply, not a failed test."""
    got = ce.classify_closure(
        owner=None,
        evidence="Confirmed the SOW was delivered to the team",
    )
    assert got["owner_named"] is None
    assert ce.REASON_OWNER_NOT_NAMED not in got["reasons"]
    assert got["band"] == ce.CONFIDENT


def test_short_owner_token_cannot_be_matched():
    """'Al' would match 'also' and 'always'. Unanswerable, so it must not
    vote either way."""
    assert ce.owner_named("Al", "we also finished it") is None
    got = ce.classify_closure(owner="Al", evidence="Confirmed, the work was delivered")
    assert ce.REASON_OWNER_NOT_NAMED not in got["reasons"]


def test_owner_match_is_case_insensitive_and_first_name_only():
    assert ce.owner_named("Sukumar Gurugubelli", "SUKUMAR confirmed it") is True
    assert ce.owner_named("Sukumar Gurugubelli", "nobody said anything") is False


def test_non_english_evidence_routes_to_believed_not_confident():
    """Markers are English only. Unreadable text must fail SAFE, which is
    BELIEVED: it costs a confirm tap, where the other direction fabricates
    a delivery."""
    got = ce.classify_closure(
        owner="Vijay",
        evidence="Vijay ha confermato que el trabajo fue entregado ayer",
    )
    assert got["band"] == ce.BELIEVED
    assert ce.REASON_NO_COMPLETION_LANGUAGE in got["reasons"]


def test_owner_gate_can_be_relaxed_without_touching_the_rest():
    """The owner signal is the softer of the two and may need retuning
    against real data. Turning it off must not disturb tense routing."""
    strict = ce.classify_closure(
        owner="Karthik",
        evidence="Scott confirmed the standup was held and notes were sent",
    )
    assert strict["band"] == ce.BELIEVED
    relaxed = ce.classify_closure(
        owner="Karthik",
        evidence="Scott confirmed the standup was held and notes were sent",
        require_owner_named=False,
    )
    assert relaxed["band"] == ce.CONFIDENT


def test_negated_completion_never_closes():
    """"did not send" contains "did" and "sent". Without the negation
    guard this closes an item on evidence that it is NOT done."""
    for evidence in (
        "Vijay did not send the email yet",
        "Sukumar has not completed the integration",
        "Pallavi is still waiting on the endpoint; nothing delivered",
        "Suresh never shared the threshold logic",
    ):
        got = ce.classify_closure(owner=None, evidence=evidence)
        assert got["band"] == ce.BELIEVED, evidence
        assert ce.REASON_NEGATED in got["reasons"], evidence


def test_word_boundaries_not_substrings():
    """"did" must not match "candidate", and a sentence ending in a
    completion word must still match it."""
    assert ce.classify_closure(None, "the candidate pool was reviewed")["past_markers"] == []
    assert "sent" in ce.classify_closure(None, "the notes were sent")["past_markers"]
    assert "done" in ce.classify_closure(None, "it is done.")["past_markers"]


def test_bare_complete_is_an_assignment_not_a_delivery():
    """"Pallavi to complete the integration" is work being handed out."""
    got = ce.classify_closure(owner="Pallavi", evidence="Pallavi to complete the integration")
    assert got["band"] == ce.BELIEVED


def test_in_progress_is_not_complete():
    got = ce.classify_closure(
        owner="Srikanth",
        evidence="Srikanth is working on the attachment download issue",
    )
    assert got["band"] == ce.BELIEVED


def test_reasons_are_stable_identifiers_not_prose():
    """These ship on the wire, so they must be machine readable and free
    of the punctuation the house style bans."""
    for reason in (
        ce.REASON_NO_EVIDENCE, ce.REASON_FUTURE_ONLY,
        ce.REASON_NO_COMPLETION_LANGUAGE, ce.REASON_OWNER_NOT_NAMED,
        ce.REASON_NEGATED, ce.REASON_IN_PROGRESS, ce.REASON_MIXED_TENSE,
    ):
        assert reason == reason.lower()
        assert " " not in reason
        assert "—" not in reason and "–" not in reason


# ---------------------------------------------------------------
# The collapse: nothing auto-closes any more.
# ---------------------------------------------------------------

from pathlib import Path  # noqa: E402

WORKER = (Path(__file__).resolve().parents[2] / "src" / "worker.py").read_text()


def _apply_body():
    return WORKER.split("async def _apply_resolved_commitments")[1].split(
        "\n    async def "
    )[0]


def test_the_extraction_path_never_closes_an_item():
    """The audit that ended the confident band: 13 of 167 auto-closed and
    at least two were wrong, including one whose own evidence read "Joy
    TO VALIDATE in QA". A wrong close fabricates delivery history in the
    artifact people are least likely to question."""
    body = _apply_body()
    assert "completed_at = NOW()" not in body
    assert "status = 'archived'" not in body
    assert "'\"extraction\"'" not in body, "no completion_source may be stamped here"


def test_every_reported_resolution_becomes_a_belief():
    body = _apply_body()
    assert "believed_complete_at" in body
    assert "believed_complete_evidence" in body
    assert "believed_complete_reasons" in body


def test_the_band_survives_only_as_a_presentation_hint():
    """Keeping it is what lets the app put likely-yes cards first. It
    must not be reachable as a close decision again."""
    body = _apply_body()
    assert "believed_evidence_strength" in body
    assert 'if verdict["band"] == BELIEVED' not in body, "band must not branch behaviour"


def test_the_return_value_is_beliefs_raised_not_closures():
    """A permanently-zero 'resolved' count reads identically whether the
    pass worked or never ran, which is the instrument failure this whole
    change came from."""
    body = _apply_body()
    assert "return believed_count" in body
    assert "resolved_count" not in body
