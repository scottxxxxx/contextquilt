"""Conduct rows (origin scoped, not project scoped: SS's moment) in recall
and on the Woven quilt.

Persona test, 2026-09-05: with CQ's real scorer and formatter at the
700 token default, three moments in a 15 row block displaced the FCR
takeaway, Dana's risks-as-owner-and-date preference and the trait, and
the full block lost to the block without moments on every question,
while moments alone scored as well as the rules on the meeting chat.
The knowledge is real; the ranking and the rendering were the defect.

Four changes, one type set, read from the registered manifests: conduct
rows rank below the person rules and are boosted by OWNER, they fold
into the named person's header line as a capsule, they never earn a
Woven tile on their own, and the overdue marker says what recall knows.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from contextquilt.services.facet_runtime import (
    TypeRuntime,
    build_type_runtime,
    conduct_types_from_manifests,
    fallback_type_runtime,
)
from contextquilt.services.recall_formatter import format_flat_ranked_with_stats
from contextquilt.services.recall_scorer import (
    CONDUCT_PRIORITY,
    TYPE_PRIORITY,
    score_patches,
)
from contextquilt.services.woven_digest import (
    DROP_CONDUCT,
    build_digest,
    why_not_a_tile,
)

ROOT = Path(__file__).resolve().parents[2]
MAIN = (ROOT / "src" / "main.py").read_text()
WORKER = (ROOT / "src" / "worker.py").read_text()

SS_MANIFEST = {"patch_types": [
    {"domain_type": "moment", "origin_scoped": True, "project_scoped": False},
    {"domain_type": "commitment", "project_scoped": True},
    {"domain_type": "role", "origin_scoped": True, "project_scoped": True},
]}
TODAY = date(2026, 9, 5)


def _row(ptype, text, owner=None, pid="p", **v):
    value = {"text": text, **v}
    if owner:
        value["owner"] = owner
    return {"patch_id": pid, "patch_type": ptype, "value": value,
            "created_at": None, "last_observed_at": None}


# ----------------------------------------------------------------------
# The type set comes from the manifests
# ----------------------------------------------------------------------

def test_conduct_types_are_origin_scoped_and_not_project_scoped():
    assert conduct_types_from_manifests([SS_MANIFEST]) == frozenset({"moment"})
    assert conduct_types_from_manifests([{"manifest": SS_MANIFEST}]) == frozenset({"moment"})


def test_no_manifest_means_no_conduct_type_and_the_floor_has_none():
    assert conduct_types_from_manifests([]) == frozenset()
    assert conduct_types_from_manifests([None, "not json", 7]) == frozenset()
    assert fallback_type_runtime().conduct_types == frozenset()
    assert build_type_runtime([], [SS_MANIFEST]).conduct_types == frozenset({"moment"})


def test_a_manifest_arriving_as_a_json_string_is_read():
    """asyncpg hands JSONB back as a STRING unless a codec is registered
    (the same trap the woven route hit). On prod the first deploy served
    an empty conduct set and no capsule rendered, because the unwrap step
    turned the string into None before the parse ever ran."""
    import json as _json
    assert conduct_types_from_manifests([_json.dumps(SS_MANIFEST)]) == frozenset({"moment"})
    assert conduct_types_from_manifests([{"manifest": _json.dumps(SS_MANIFEST)}]) == frozenset({"moment"})


def test_the_runtime_reads_manifests_and_survives_their_absence():
    src = (ROOT / "src" / "contextquilt" / "services" / "facet_runtime.py").read_text()
    assert 'manifests = [r["manifest"] for r in await fetch(MANIFEST_QUERY)]' in src
    assert 'logger.warning("type_runtime_manifests_unavailable"' in src
    assert "_cache_snapshot = build_type_runtime(rows, manifests)" in src


# ----------------------------------------------------------------------
# Ranking: below the person rules, boosted by owner only
# ----------------------------------------------------------------------

def test_conduct_ranks_below_preference_and_above_takeaway():
    assert TYPE_PRIORITY["takeaway"] < CONDUCT_PRIORITY < TYPE_PRIORITY["preference"]
    rows = [_row("moment", "Held the number back", owner="Marcus Lee", pid="m"),
            _row("preference", "Prefers risks as owner and date", pid="pref"),
            _row("takeaway", "The change board is the real gate", pid="t")]
    out = score_patches(rows, "prep me", [], facet_by_type={"moment": "Episode"},
                        conduct_types=frozenset({"moment"}))
    assert [r["patch_id"] for _, r in out] == ["pref", "m", "t"]


def test_without_the_set_a_moment_is_still_an_episode_at_thirty():
    """Every caller that does not pass conduct_types is byte-identical."""
    rows = [_row("moment", "Held the number back", owner="Marcus Lee", pid="m"),
            _row("preference", "Prefers risks as owner and date", pid="pref")]
    out = score_patches(rows, "prep me", [], facet_by_type={"moment": "Episode"})
    assert [r["patch_id"] for _, r in out] == ["m", "pref"]


def test_a_conduct_row_is_boosted_by_its_owner_not_by_a_name_in_its_text():
    about_raj = _row("moment", "Asked Hassan whether Tripp was joining", owner="Raj Kumar", pid="raj")
    about_hassan = _row("moment", "Relayed the team's feedback", owner="Hassan Waheed", pid="hassan")
    scores = dict((r["patch_id"], s) for s, r in score_patches(
        [about_raj, about_hassan], "what did Hassan say", ["Hassan"],
        conduct_types=frozenset({"moment"})))
    # The +100 went to the owner match; the Raj row got at most the +15
    # keyword overlap for the word "hassan" in its text, never the +100.
    assert scores["hassan"] > scores["raj"] + 80
    assert scores["raj"] < 50


def test_a_bare_first_name_in_the_query_matches_a_full_owner_name():
    row = _row("moment", "Pushed back on the date", owner="Dana Whitfield", pid="d")
    (s_named,), (s_other,) = (score_patches([row], "handle Dana", ["Dana"], conduct_types=frozenset({"moment"})),
                              score_patches([row], "handle Ines", ["Ines"], conduct_types=frozenset({"moment"})))
    assert s_named[0] > s_other[0] + 90


# ----------------------------------------------------------------------
# Rendering: the capsule
# ----------------------------------------------------------------------

def _ent(name, etype="person", desc=None):
    return {"entity_type": etype, "name": name, "description": desc}


def test_a_named_persons_conduct_folds_into_their_header_line_and_leaves_the_list():
    scored = [(100.0, _row("moment", "Held the number back until the cause was known", owner="Marcus Lee", pid="m1")),
              (99.0, _row("moment", "Asked for per region numbers before agreeing", owner="Marcus Lee", pid="m2")),
              (50.0, _row("commitment", "Tom will deliver the report", owner="Tom Okafor", pid="c"))]
    out, n = format_flat_ranked_with_stats(
        scored, [_ent("Marcus Lee", desc="CTO at Northwind")], [], today=TODAY,
        conduct_types=frozenset({"moment"}))
    assert "People: Marcus Lee (CTO at Northwind). How they operate: Held the number back until the cause was known; Asked for per region numbers before agreeing" in out
    assert "[moment]" not in out
    assert "[todo] Tom will deliver the report" in out
    assert n == 1  # the capsule is header, not list


def test_a_capsule_is_capped_and_keeps_rank_order():
    scored = [(90.0 - i, _row("moment", f"conduct {i}", owner="Dana Whitfield", pid=f"m{i}")) for i in range(5)]
    out, n = format_flat_ranked_with_stats(scored, [_ent("Dana Whitfield")], [], today=TODAY,
                                           conduct_types=frozenset({"moment"}), capsule_limit=3)
    assert "How they operate: conduct 0; conduct 1; conduct 2" in out
    assert "conduct 3" not in out and n == 0


def test_conduct_about_somebody_not_in_the_header_stays_in_the_list():
    scored = [(60.0, _row("moment", "Relayed positive feedback", owner="Hassan Waheed", pid="h"))]
    out, n = format_flat_ranked_with_stats(scored, [_ent("Raj Kumar")], [], today=TODAY,
                                           conduct_types=frozenset({"moment"}))
    assert "[moment] Relayed positive feedback [owner: Hassan Waheed]" in out
    assert "How they operate" not in out and n == 1


def test_without_the_set_the_formatter_is_byte_identical_to_before():
    scored = [(60.0, _row("moment", "Held the number back", owner="Marcus Lee", pid="m"))]
    a, _ = format_flat_ranked_with_stats(scored, [_ent("Marcus Lee")], [], today=TODAY)
    assert "[moment] Held the number back [owner: Marcus Lee]" in a and "How they operate" not in a


def test_the_overdue_marker_says_what_recall_knows():
    scored = [(60.0, _row("commitment", "Ship it", owner="Priya", pid="c", deadline_date="2026-08-30"))]
    out, _ = format_flat_ranked_with_stats(scored, [], [], today=TODAY)
    assert "(OVERDUE, not confirmed done)" in out
    assert "(OVERDUE)" not in out


# ----------------------------------------------------------------------
# Woven: never a tile of its own
# ----------------------------------------------------------------------

def _tile(ptype, text, origin="M1"):
    return {"patch_id": ptype + text[:6], "patch_type": ptype, "origin_id": origin,
            "value": {"text": text, "headline": text[:40]}, "created_at": None}


def test_a_conduct_row_is_dropped_with_its_own_reason():
    m = _tile("moment", "Pushed back on the September 8 start")
    assert why_not_a_tile(m, conduct_types=frozenset({"moment"})) == DROP_CONDUCT
    assert why_not_a_tile(m) is None   # no set, no rule: byte-identical for every other caller


def test_the_digest_reports_the_drop_and_keeps_the_decision():
    d = build_digest([_tile("moment", "Pushed back on the September 8 start"),
                      _tile("decision", "EMEA pilot starts September 8 with logging off")],
                     limit=6, conduct_types=frozenset({"moment"}))
    assert d["dropped"].get(DROP_CONDUCT) == 1
    assert [p["patch_type"] for p in d["patches"]] == ["decision"]


# ----------------------------------------------------------------------
# Wiring
# ----------------------------------------------------------------------

def test_recall_passes_the_set_to_the_scorer_and_the_formatter():
    assert "conduct_types=type_runtime.conduct_types,\n    )" in MAIN
    assert "person_entity_type=recall_vocab.person_entity_type,\n                conduct_types=type_runtime.conduct_types," in MAIN


def test_the_digest_route_and_the_headline_lane_pass_the_set():
    assert "conduct_types = (await facet_runtime.get_type_runtime(db_pool.fetch)).conduct_types" in MAIN
    assert "conduct_types=conduct_types)" in MAIN
    assert "conduct_types = (await get_type_runtime(self.db.fetch)).conduct_types" in WORKER
    assert "require_headline=False,\n                    conduct_types=conduct_types) is None" in WORKER
