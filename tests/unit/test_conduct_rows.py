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
    assert "People: Marcus Lee (CTO at Northwind). How they operate: Held the number back until the cause was known / Asked for per region numbers before agreeing" in out
    assert "[moment]" not in out
    assert "[todo] Tom will deliver the report" in out
    assert n == 1  # the capsule is header, not list


def test_a_capsule_is_capped_and_the_rest_of_that_persons_conduct_leaves_the_list():
    """Capsule or nothing, for a person in the header.

    #446 let overflow keep its list rank, which was right when three or
    four conduct rows reached the candidate set. The conduct guarantee
    (#453) admits a person's whole history, and on prod that same rule
    put EIGHT of one person's rows in a thirteen row block and pushed out
    the decisions, goals and events. The capsule is the representation.
    """
    scored = [(90.0 - i, _row("moment", f"conduct {i}", owner="Dana Whitfield", pid=f"m{i}")) for i in range(5)]
    out, n = format_flat_ranked_with_stats(scored, [_ent("Dana Whitfield")], [], today=TODAY,
                                           conduct_types=frozenset({"moment"}), capsule_limit=3)
    assert "How they operate: conduct 0 / conduct 1 / conduct 2" in out
    assert "[moment] conduct 3" not in out and "[moment] conduct 4" not in out
    assert n == 0


def test_the_other_types_keep_their_slots_when_a_person_has_deep_history():
    """The regression this rule exists to prevent, in one assertion."""
    conduct = [(108.0 - i, _row("moment", f"conduct {i}", owner="Dana Whitfield", pid=f"m{i}"))
               for i in range(8)]
    others = [(60.0, _row("decision", "EMEA pilot starts September 8", pid="d")),
              (58.0, _row("commitment", "Tom will deliver the report", owner="Tom", pid="c")),
              (55.0, _row("takeaway", "The change board is the real gate", pid="t"))]
    out, n = format_flat_ranked_with_stats(conduct + others, [_ent("Dana Whitfield")], [],
                                           today=TODAY, conduct_types=frozenset({"moment"}))
    assert n == 3
    for frag in ("EMEA pilot starts", "Tom will deliver", "change board"):
        assert frag in out
    assert "[moment]" not in out


def test_the_default_capsule_is_two_items_clipped_at_a_word_boundary():
    """Two people at three full items cost ~600 chars of a 700 token block
    and the decisions fell off the end (persona rerun, 2026-09-05)."""
    long = ("Held the APAC number back from the steering summary until Tom's investigation was done, "
            "saying a number without a cause would set the wrong conversation for the committee")
    scored = [(90.0, _row("moment", long, owner="Marcus Lee", pid="m0")),
              (89.0, _row("moment", "Asked for per region numbers", owner="Marcus Lee", pid="m1")),
              (88.0, _row("moment", "Wanted the date in writing", owner="Marcus Lee", pid="m2"))]
    out, n = format_flat_ranked_with_stats(scored, [_ent("Marcus Lee")], [], today=TODAY,
                                           conduct_types=frozenset({"moment"}))
    head = out.split("\n")[0]
    assert head.count(" / ") == 1                       # two items by default
    assert "for the committee" not in head and "..." in head
    clipped = head.split("How they operate: ")[1].split(" / ")[0]
    assert len(clipped) <= 120 + 3 and not clipped[:-3].endswith(" ")
    # Capsule or nothing: the third row is his too, so it leaves the block
    # rather than taking a list slot from a decision.
    assert "Wanted the date in writing" not in out and n == 0


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


# ----------------------------------------------------------------------
# A small budget buys rows, not chrome
# ----------------------------------------------------------------------

def test_under_a_small_budget_the_header_is_names_only_and_relations_are_dropped():
    """GP sends 300 tokens on a draft ask. On prod the hour it went live,
    the descriptions and the Relations line ate ~850 of 1,200 characters
    and one row survived."""
    long_desc = "Consumer iOS app for immigration intake and decision-making; includes iMessage extension and support for recording and transcribing psychological evaluation interviews."
    ents = [_ent("Immigration interview app", "project", desc=long_desc),
            _ent("Steven Williams", desc="Co-founder/partner on the immigration app project; exploring partnerships with legal service providers and law firm service companies.")]
    rels = [{"from_name": "Venkata", "to_name": "Scott Guida", "relationship_type": "reports_to"}]
    scored = [(90.0 - i, _row("preference", f"Prefers channel {i} for anything urgent", owner="Steven Williams", pid=f"p{i}")) for i in range(6)]
    small, n_small = format_flat_ranked_with_stats(scored, ents, rels, max_chars=1200, today=TODAY)
    big, n_big = format_flat_ranked_with_stats(scored, ents, rels, max_chars=2800, today=TODAY)
    assert small.startswith("Projects: Immigration interview app\nPeople: Steven Williams\n")
    assert "Relations:" not in small and long_desc not in small
    assert n_small >= 4
    assert "Relations:" in big and long_desc in big and n_big == 6


def test_the_capsule_survives_the_compact_header():
    ents = [_ent("Marcus Lee", desc="CTO at Northwind")]
    scored = [(90.0, _row("moment", "Asked for per region numbers before agreeing", owner="Marcus Lee", pid="m"))]
    out, _ = format_flat_ranked_with_stats(scored, ents, [], max_chars=1200, today=TODAY,
                                           conduct_types=frozenset({"moment"}))
    assert out.startswith("People: Marcus Lee. How they operate: Asked for per region numbers before agreeing")
    assert "CTO at Northwind" not in out


def test_a_capsule_does_not_repeat_the_same_clause():
    """Seen on prod 2026-09-05: "Asked a probing question about why all AI
    providers went down simultaneously, rather than accepting the surface
    explanation" and "Asked why all AI units went down simultaneously,
    expressing curiosity about the root cause" rendered as a capsule of two.
    Moments are opted out of dedup on purpose; the capsule is not."""
    a = "Asked a probing question about why all AI providers went down simultaneously, rather than accepting the surface explanation"
    b = "Asked why all AI units went down simultaneously, expressing curiosity about the root cause"
    c = "Offered to write the index architecture document himself"
    scored = [(90.0, _row("moment", a, owner="Steven Williams", pid="a")),
              (89.0, _row("moment", b, owner="Steven Williams", pid="b")),
              (88.0, _row("moment", c, owner="Steven Williams", pid="c"))]
    out, n = format_flat_ranked_with_stats(scored, [_ent("Steven Williams")], [], today=TODAY,
                                           conduct_types=frozenset({"moment"}), capsule_item_chars=400)
    head = out.split("\n")[0]
    assert a in head and c in head and b not in head
    # The near duplicate is his conduct too, so it leaves the block
    # entirely rather than reappearing in the list as a near copy of a
    # line already in the capsule.
    assert b not in out and n == 0


# ----------------------------------------------------------------------
# The conduct guarantee: history reaches the capsule (2026-09-06 A/B)
# ----------------------------------------------------------------------

from contextquilt.services.entity_match import owner_tokens  # noqa: E402
from contextquilt.services.recall_scope import build_conduct_fetch  # noqa: E402

AGE_SQL = "AND ($4::int IS NULL OR cp.patch_type = ANY($3::text[]))"


def test_owner_tokens_carry_both_the_full_name_and_the_first_token():
    assert owner_tokens(["Steven Williams"]) == ["steven williams", "steven"]
    assert owner_tokens(["Raj"]) == ["raj"]
    assert owner_tokens(["", None, " Dana Whitfield "]) == ["dana whitfield", "dana"]


def test_the_leg_matches_an_owner_by_whole_name_or_first_token():
    sql, args = build_conduct_fetch(
        "user:u", frozenset({"moment"}), ["steven williams", "steven"],
        ["trait"], None, AGE_SQL, recall_project_id="P")
    assert "lower(cp.value->>'owner') = ANY($5::text[])" in sql
    assert "lower(split_part(cp.value->>'owner', ' ', 1)) = ANY($5::text[])" in sql
    assert args[4] == ["steven williams", "steven"]
    assert args[5] == ["moment"] and args[6] == 12


def test_the_leg_is_bounded_and_carries_the_age_window_and_the_scope_rule():
    sql, args = build_conduct_fetch(
        "user:u", frozenset({"moment"}), ["steven"], ["trait"], 30, AGE_SQL,
        recall_project_id="P", limit=6)
    assert AGE_SQL in sql                      # a tier window still bounds it
    assert "LIMIT $7" in sql and args[6] == 6  # bounded
    assert sql.startswith("WITH origins AS MATERIALIZED")   # same scope rule
    assert "held AS (" in sql
    assert args[3] == 30


def test_the_leg_has_no_recency_window_because_that_is_the_defect():
    """The flat leg's newest-20 is exactly what hid 34 of 35 conduct rows."""
    sql, _ = build_conduct_fetch("user:u", frozenset({"moment"}), ["steven"],
                                 ["trait"], None, AGE_SQL, recall_project_id="P")
    body = sql[sql.index("SELECT cp.patch_id"):]
    assert body.count("LIMIT") == 1            # only the explicit bound
    assert "UNION ALL" not in body             # not the two-window flat leg


def test_recall_runs_it_only_with_a_project_and_a_matched_person_and_fails_open():
    i = MAIN.index("# Conduct guarantee:")
    block = MAIN[i:i + 1400]
    assert "if type_runtime.conduct_types and matched_names and has_project_scope:" in block
    assert "owner_tokens(matched_names)" in block
    assert 'logger.warning("conduct_guarantee_failed"' in block
    # It only ADDS candidates; the merge dedupes and the scorer ranks.
    assert "list(overdue_rows) + list(conduct_rows) + list(cue_rows)" in MAIN


def test_a_folded_row_does_not_spend_one_of_the_callers_row_slots():
    """Applying the cap before the fold took the prod block from 14 rows
    to 5 once a person's whole conduct history was a candidate."""
    conduct = [(108.0 - i, _row("moment", f"conduct {i}", owner="Dana Whitfield", pid=f"m{i}"))
               for i in range(8)]
    others = [(60.0 - i, _row("decision", f"decision {i}", pid=f"d{i}")) for i in range(6)]
    out, n = format_flat_ranked_with_stats(conduct + others, [_ent("Dana Whitfield")], [],
                                           today=TODAY, conduct_types=frozenset({"moment"}),
                                           max_chars=6000, max_rows=5)
    assert n == 5, "five decisions should render, not zero after eight folds ate the cap"
    for i in range(5):
        assert f"decision {i}" in out
    assert "decision 5" not in out and "[moment]" not in out


def test_max_rows_is_optional_and_absent_means_unlimited():
    rows = [(60.0 - i, _row("decision", f"decision {i}", pid=f"d{i}")) for i in range(20)]
    _, n = format_flat_ranked_with_stats(rows, [], [], today=TODAY, max_chars=9000)
    assert n == 20
    _, capped = format_flat_ranked_with_stats(rows, [], [], today=TODAY, max_chars=9000, max_rows=7)
    assert capped == 7


def test_recall_caps_rendered_rows_instead_of_slicing_the_candidates():
    assert "scored_for_output = scored\n" in MAIN
    assert "scored[:flat_cap]" not in MAIN
    assert "max_rows=flat_cap," in MAIN
