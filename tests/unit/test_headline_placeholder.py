"""A diarization label is not a name, and a tile is where it shows most.

18 live headlines read like 'Speaker 3 create checklist snapshot by
Friday' on the most prominent text in the product. The ruling that
'Speaker 3' is not a name already existed elsewhere in this codebase
(`is_placeholder_or_self_person` refuses them out of the entities
array); the headline validator had simply never heard of it.
"""

import pytest

from contextquilt.services import headlines as h


class TestTheLabelIsRefused:

    @pytest.mark.parametrize("line", [
        "Speaker 3 create checklist snapshot by Friday",
        "Asked Speaker 7 about API key for QA",
        "Fixed assets need follow-up with Speaker 4",
        "CTS QA endpoint returns 400 error for Speaker 2",
        "Speaker 10 validate screenshot solution in XPX",
        "Speaker_6 import Excel to smart sheets",
        "Unknown Speaker raised the billing question",
        "Unidentified speaker asked about scope",
    ])
    def test_a_headline_carrying_a_label_is_refused(self, line):
        assert h.why_invalid(line, line) == h.PLACEHOLDER_NAME

    def test_it_is_caught_mid_sentence_not_only_at_the_start(self):
        # The whole reason this is its own expression rather than a call
        # to is_placeholder_or_self_person, which matches on a PREFIX.
        assert h.why_invalid("Asked Speaker 7 about the key",
                             "Asked Speaker 7 about the key") \
            == h.PLACEHOLDER_NAME


class TestOrdinaryEnglishSurvives:
    """The label is refused. The words are not."""

    @pytest.mark.parametrize("line", [
        "Unknown cause of the latency spike",
        "Speakerphone budget approved",
        "Keynote speaker confirmed for October",
        "Unknown owner on the lease tab",
    ])
    def test_a_good_line_is_not_refused_as_a_placeholder(self, line):
        assert h.why_invalid(line, line) != h.PLACEHOLDER_NAME

    def test_a_bare_unknown_is_deliberately_allowed(self):
        # A rule that ate this would refuse good lines to catch a form
        # that has never appeared in the data.
        assert h._PLACEHOLDER.search("Unknown cause of the outage") is None


class TestTheModelIsToldNotOnlyRefused:
    """A rule with no prompt guidance turns 18 tiles into 0 tiles."""

    def test_the_writer_prompt_forbids_the_label(self):
        assert "Speaker 3" in h.SYSTEM
        assert "speaker label" in h.SYSTEM.lower()

    def test_the_retry_prompt_forbids_it_too(self):
        # The retry is a second chance shown the failure reason, so it
        # must know the rule or it rewrites into the same refusal.
        assert "speaker label" in h.RETRY_SYSTEM.lower()

    def test_the_prompt_shows_the_fix_not_only_the_ban(self):
        # Telling a model what not to write leaves it guessing what to
        # write; the example gives it the shape.
        assert "Create checklist snapshot by Friday" in h.SYSTEM


class TestItIsCheckedBeforeTheFactIsNeeded:

    def test_a_placeholder_beats_the_figure_rules(self):
        # Ordering matters for the REASON reported, which is what the
        # refusal counters are tuned against. A line naming Speaker 2
        # and dropping a figure should report the placeholder.
        line = "Speaker 2 owns the rollout"
        assert h.why_invalid(line, "Speaker 2 owns the 40% rollout") \
            == h.PLACEHOLDER_NAME


class TestSelfHeadlineHonoursTheRule:
    """The deterministic path judges by the SAME validator."""

    def test_a_fact_that_is_a_label_is_not_taken_as_its_own_headline(self):
        assert h.self_headline("Speaker 3 case") is None
