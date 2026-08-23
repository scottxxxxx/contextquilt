"""last_seen_in: the badge for a "which Sam?" picker.

Receipt 2026-08-23: the live picker offered Sam Altman (0 meetings) and
Sam Wisco (1 meeting, mention only) and Scott asked for the project of
Sam Wisco's last meeting as a badge. top_project is presence-grade and is
null for him; this field is the newest appearance in any capacity, with
the capacity stated so the badge cannot claim he spoke.
"""
import pathlib
from datetime import datetime, timezone

from contextquilt.services.people_signals import last_seen_in


def ap(ts, project=None, project_id=None, caps=("mention",), origin="m"):
    return {"last_seen_at": datetime(2026, 8, ts, tzinfo=timezone.utc), "project": project,
            "project_id": project_id, "capacities": list(caps), "origin_id": origin}


def test_newest_appearance_wins_regardless_of_input_order():
    got = last_seen_in([ap(1, "Old", "o"), ap(17, "Agent Utilization", "au", origin="m17"), ap(9, "Mid", "x")])
    assert got["project"] == "Agent Utilization" and got["project_id"] == "au"
    assert got["origin_id"] == "m17" and got["last_seen_at"].startswith("2026-08-17")


def test_mention_only_is_served_and_says_so():
    """The whole point: top_project hides him, this does not, and it
    reports the capacity so the badge reads 'mentioned in', never 'spoke'."""
    got = last_seen_in([ap(17, "Agent Utilization", "au", caps=("mention",))])
    assert got["capacities"] == ["mention"]


def test_no_project_still_serves_date_and_capacity():
    got = last_seen_in([ap(17, None, None, caps=("speaker", "mention"))])
    assert got["project"] is None and got["project_id"] is None
    assert got["capacities"] == ["mention", "speaker"]   # sorted, stable


def test_pre_31_empty_capacities_are_never_fabricated():
    assert last_seen_in([ap(3, "P", "p", caps=())])["capacities"] == []


def test_no_appearances_is_none_not_an_empty_badge():
    assert last_seen_in([]) is None and last_seen_in(None) is None


def test_it_is_on_the_list_row_next_to_top_project():
    text = (pathlib.Path(__file__).resolve().parents[2] / "src" / "main.py").read_text()
    i = text.index('"top_project": projects[0] if projects else None,')
    assert '"last_seen_in": last_seen_in(appearances),' in text[i:i + 600]
