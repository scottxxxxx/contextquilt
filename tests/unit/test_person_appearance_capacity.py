"""Unit tests for appearance capacity.

Two pieces: `speaker_labels_in`, the shared reader that decides who spoke,
and `merge_tier`, the backfill fold that accumulates capacities instead of
letting the strongest one mask the rest.

Grounding for the fixtures: on the ABM meeting of 2026-07-28, speaker
labels were clean and consistent (Ellery appears as a label 23 times,
correctly spelled every time) while the same names inside spoken text were
not ("Palavi", "Fenwyck", "JN", "JNZ"). Labels are the trustworthy signal;
mention text is not.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from src.contextquilt.services.extraction_schema import speaker_labels_in  # noqa: E402
from src.contextquilt.services.person_appearances import merge_tier  # noqa: E402


TRANSCRIPT = """[Merrick] Denby. As JN shaves that IP address in the chat here, right?

[Denby] I know the URL have changed, but our white justing is done based on IPs.

[Ellery] I think same thing we need to follow for this also.

[Speaker 12] Some diarized noise nobody named.

[Multiple] Overlapping crosstalk.
"""


class TestSpeakerLabelsIn:
    def test_reads_labels_not_body_text(self):
        labels = speaker_labels_in(TRANSCRIPT)
        assert "merrick" in labels
        assert "denby" in labels
        assert "ellery" in labels

    def test_spoken_nicknames_are_not_speakers(self):
        """'JN' is said out loud inside a Merrick turn. He did not speak."""
        assert "jn" not in speaker_labels_in(TRANSCRIPT)

    def test_diarization_placeholders_dropped(self):
        """A placeholder must never gate an identity decision."""
        assert "speaker 12" not in speaker_labels_in(TRANSCRIPT)

    def test_you_marker_stripped_so_labels_match_entity_names(self):
        assert speaker_labels_in("[Scott (you)] hello") == {"scott"}

    def test_user_label_is_excluded_when_given(self):
        assert speaker_labels_in("[Scott] hi\n[Ada] hey", user_label="Scott") == {"ada"}

    def test_only_matches_at_line_start(self):
        assert speaker_labels_in("chatter [Ada] mid line") == set()

    def test_runaway_bracket_cannot_swallow_a_paragraph(self):
        assert speaker_labels_in("[" + "x" * 200 + "] body") == set()

    def test_empty_and_non_string_inputs(self):
        assert speaker_labels_in("") == set()
        assert speaker_labels_in(None) == set()
        assert speaker_labels_in(42) == set()


def _row(uid="u", eid="e", oid="m", project="p", first=1, last=2):
    return {
        "user_id": uid, "entity_id": eid, "origin_id": oid,
        "origin_type": "meeting", "project_id": project,
        "first": first, "last": last,
    }


class TestMergeTier:
    def test_first_tier_populates(self):
        rows = {}
        assert merge_tier(rows, {("u", "e", "m"): _row()}, "ownership") == 1
        assert rows[("u", "e", "m")]["capacities"] == {"ownership"}

    def test_second_tier_accumulates_rather_than_masking(self):
        """The whole point. Owning and speaking must both survive."""
        rows = {}
        merge_tier(rows, {("u", "e", "m"): _row()}, "ownership")
        merge_tier(rows, {("u", "e", "m"): _row()}, "speaker")
        assert rows[("u", "e", "m")]["capacities"] == {"ownership", "speaker"}

    def test_all_three_accumulate(self):
        rows = {}
        for cap in ("ownership", "speaker", "mention"):
            merge_tier(rows, {("u", "e", "m"): _row()}, cap)
        assert rows[("u", "e", "m")]["capacities"] == {"ownership", "speaker", "mention"}

    def test_second_tier_contributes_no_new_keys(self):
        rows = {}
        merge_tier(rows, {("u", "e", "m"): _row()}, "ownership")
        assert merge_tier(rows, {("u", "e", "m"): _row()}, "speaker") == 0

    def test_first_tier_keeps_timestamps_and_project(self):
        """Earlier tier is better provenance; only capacity accumulates."""
        rows = {}
        merge_tier(rows, {("u", "e", "m"): _row(first=1, last=2, project="orig")}, "ownership")
        merge_tier(rows, {("u", "e", "m"): _row(first=99, last=99, project="later")}, "mention")
        row = rows[("u", "e", "m")]
        assert (row["first"], row["last"], row["project_id"]) == (1, 2, "orig")

    def test_distinct_keys_stay_distinct(self):
        rows = {}
        merge_tier(rows, {("u", "a", "m"): _row(eid="a")}, "speaker")
        merge_tier(rows, {("u", "b", "m"): _row(eid="b")}, "mention")
        assert rows[("u", "a", "m")]["capacities"] == {"speaker"}
        assert rows[("u", "b", "m")]["capacities"] == {"mention"}

    def test_same_person_different_meetings_stay_distinct(self):
        rows = {}
        merge_tier(rows, {("u", "e", "m1"): _row(oid="m1")}, "speaker")
        merge_tier(rows, {("u", "e", "m2"): _row(oid="m2")}, "mention")
        assert rows[("u", "e", "m1")]["capacities"] == {"speaker"}
        assert rows[("u", "e", "m2")]["capacities"] == {"mention"}

    def test_does_not_mutate_the_tier_result(self):
        """merge_tier copies, so a caller can reuse a tier's dict."""
        found = {("u", "e", "m"): _row()}
        rows = {}
        merge_tier(rows, found, "ownership")
        assert "capacities" not in found[("u", "e", "m")]

    def test_empty_tier_is_a_noop(self):
        rows = {("u", "e", "m"): dict(_row(), capacities={"ownership"})}
        assert merge_tier(rows, {}, "speaker") == 0
        assert rows[("u", "e", "m")]["capacities"] == {"ownership"}


class TestTheGateScenario:
    """The four prod pairs that motivated the column.

    In every one, the short name is a speaker and the full name is not, so
    a veto that requires BOTH to be speakers correctly stays silent and the
    merge is allowed to proceed on other evidence.
    """

    def _both_spoke(self, rows, a, b, meeting):
        def spoke(eid):
            row = rows.get(("u", eid, meeting)) or {}
            return "speaker" in (row.get("capacities") or set())
        return spoke(a) and spoke(b)

    def test_veto_stays_silent_when_only_one_side_spoke(self):
        rows = {}
        merge_tier(rows, {("u", "holloway", "m"): _row(eid="holloway")}, "speaker")
        merge_tier(rows, {("u", "sukumar_g", "m"): _row(eid="sukumar_g")}, "mention")
        assert not self._both_spoke(rows, "holloway", "sukumar_g", "m")

    def test_veto_fires_when_both_spoke(self):
        rows = {}
        merge_tier(rows, {("u", "ada", "m"): _row(eid="ada")}, "speaker")
        merge_tier(rows, {("u", "ada_b", "m"): _row(eid="ada_b")}, "speaker")
        assert self._both_spoke(rows, "ada", "ada_b", "m")

    def test_absence_of_capacity_is_not_evidence_of_absence(self):
        """A row with no recorded capacity must not read as 'did not speak'
        in a way that lets a merge through on false confidence. The gate
        requires positive evidence from BOTH sides, so an unknown row
        simply produces no veto rather than an assertion."""
        rows = {("u", "x", "m"): dict(_row(eid="x"), capacities=set()),
                ("u", "y", "m"): dict(_row(eid="y"), capacities=set())}
        assert not self._both_spoke(rows, "x", "y", "m")


# --------------------------------------------------------------------
# Served on the read surface, for SS's duplicate veto (2026-08-07)
# --------------------------------------------------------------------

from src.contextquilt.services.person_appearances import (  # noqa: E402
    MENTION, OWNERSHIP, SPEAKER,
)

def test_capacities_distinguish_label_drift_from_two_people():
    """The signature the veto needs, restated as a predicate.

    Across the Vijay set's eight co-occurring meetings, exactly one spelling
    carried `speaker` in each; the other was ownership only. That is label
    drift. Two genuinely different people show up as BOTH carrying speaker,
    which is what Ella Srikanth and Joy Srikanth do across six shared
    meetings.
    """
    def both_spoke(a, b):
        return SPEAKER in set(a or ()) and SPEAKER in set(b or ())

    # Label drift: one spoke, the other was only named as an owner.
    assert both_spoke([SPEAKER, OWNERSHIP], [OWNERSHIP]) is False
    assert both_spoke([SPEAKER], [OWNERSHIP]) is False
    # Two real people in one room.
    assert both_spoke([SPEAKER], [SPEAKER]) is True
    assert both_spoke([SPEAKER, OWNERSHIP], [SPEAKER, MENTION]) is True


def test_unknown_capacities_do_not_read_as_did_not_speak():
    """Migration 31's rule. An empty list predates the column, and treating
    it as "did not speak" would turn missing data into a licence to merge."""
    def both_spoke(a, b):
        return SPEAKER in set(a or ()) and SPEAKER in set(b or ())

    assert both_spoke([], [SPEAKER]) is False      # unknown, so no evidence
    assert both_spoke(None, None) is False
    # ...and the caller must not invert that into "safe to merge". The veto
    # is the conservative direction: absence of proof is not proof of absence.
