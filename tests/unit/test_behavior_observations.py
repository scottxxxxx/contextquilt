"""Behavioral observations: the corpus the profile pass was missing.

Two of the three 16a lenses ask a model to infer HOW a person behaves
from a record that only ever stored WHAT happened, and they decline for
the same stated reason on the best evidenced people in the corpus. The
fix is not a better prompt, it is a type: capture the behavior at
extraction, owned by the person, so the pattern has somewhere to
accumulate.

These tests pin the four things that would silently break it, plus the
one the earlier audit missed:

1. The ownership edge. `PERSON_OWNED_ACTION_TYPES` is the only route
   anything has to a person, so a manifest-only type never reaches a
   lens.
2. Dedup. The trigram fast path is type blind, and a collapse keeps one
   origin_id, so it destroys a receipt the pass counts in meetings.
3. The two sanitizers that touch person-shaped patches.
4. Character, not behavior (guardrail 12b) at capture time.
5. The origin stamp. A non project scoped type lands with a null
   origin_id, and the cluster query requires one.
"""

import datetime
import json
from pathlib import Path

import pytest

from contextquilt.services.consolidation import (
    build_insight_readiness,
    person_insight_rule,
)
from contextquilt.services.extraction_schema import (
    BEHAVIOR_OBSERVATION_TYPES,
    PERSON_OWNED_ACTION_TYPES,
    drop_placeholder_and_self_person_patches,
    enforce_owner_edge_agreement,
    enforce_person_ownership,
    no_collapse_patch_types,
    origin_scoped_patch_types,
    sanitize_behavior_observations,
    sanitize_you_marker_from_patches,
)
from contextquilt.services.facet_runtime import FRESHNESS_FACETS, build_type_runtime
from contextquilt.services.follow_through import (
    CHARACTER_TRAIT_WORDS,
    CHARACTER_WORDS,
    character_word_in,
)

MANIFEST = json.loads(
    (Path(__file__).resolve().parents[2] / "init-db" / "11_shouldersurf_schema.json")
    .read_text(encoding="utf-8")
)
BEHAVIOR = "behavior"


def _observation(text: str, owner: str | None = None, **extra) -> dict:
    value = {"text": text}
    if owner is not None:
        value["owner"] = owner
    value.update(extra)
    return {"type": BEHAVIOR, "value": value, "connects_to": []}


def _person(text: str, edges=None) -> dict:
    return {"type": "person", "value": {"text": text}, "connects_to": edges or []}


# ============================================================
# 1. The ownership edge: without it, nothing reaches a person
# ============================================================


class TestOwnershipEdge:
    def test_behavior_is_person_owned(self):
        assert BEHAVIOR in PERSON_OWNED_ACTION_TYPES

    def test_named_owner_gets_person_patch_and_owns_edge(self):
        content = {
            "patches": [
                _observation(
                    "Asked for the cost breakdown before agreeing to the switch",
                    owner="Denby",
                )
            ]
        }
        enforce_person_ownership(content, user_label="Scott")

        people = [p for p in content["patches"] if p["type"] == "person"]
        assert [p["value"]["text"] for p in people] == ["Denby"]
        edges = people[0]["connects_to"]
        assert edges == [
            {
                "role": "informs",
                "label": "owns",
                "target_type": BEHAVIOR,
                "target_text": "Asked for the cost breakdown before agreeing to the switch",
            }
        ]

    def test_existing_person_patch_is_reused_not_duplicated(self):
        content = {
            "patches": [
                _person("Denby"),
                _observation("Read the clause out loud rather than summarizing", "Denby"),
            ]
        }
        enforce_person_ownership(content, user_label="Scott")
        assert sum(1 for p in content["patches"] if p["type"] == "person") == 1
        assert content["patches"][0]["connects_to"][0]["label"] == "owns"

    def test_bystander_owns_edge_is_dropped_from_an_observation(self):
        # An observation is about exactly one person. The mirror enforcer
        # has to treat it like every other owned item or a second person
        # inherits somebody else's behavior.
        obs = _observation("Reopened the scope question after the vote", "Denby")
        content = {
            "patches": [
                obs,
                _person(
                    "Ellery",
                    [{
                        "role": "informs", "label": "owns",
                        "target_type": BEHAVIOR,
                        "target_text": "Reopened the scope question after the vote",
                    }],
                ),
            ]
        }
        enforce_owner_edge_agreement(content, user_label="Scott")
        ellery = content["patches"][1]
        assert ellery["connects_to"] == []
        assert content["_owner_edge_agreement_enforced"]["dropped"][0]["person"] == "Ellery"


# ============================================================
# 2. Dedup: two observations are a trajectory, not a duplicate
# ============================================================


class TestNoCollapseFlag:
    def test_manifest_declares_the_opt_out_for_behavior_only(self):
        assert no_collapse_patch_types(MANIFEST) == {BEHAVIOR}

    def test_absent_key_means_todays_behavior(self):
        # The default has to be byte-identical for every type that shipped
        # before the key existed, including every type in SS's own
        # manifest other than the new one.
        legacy = {
            "patch_types": [
                {"domain_type": "commitment"},
                {"domain_type": "takeaway", "collapse_duplicates": True},
            ]
        }
        assert no_collapse_patch_types(legacy) == frozenset()
        assert origin_scoped_patch_types(legacy) == frozenset()

    def test_garbage_manifests_are_survivable(self):
        for junk in (None, {}, {"patch_types": None}, {"patch_types": ["x"]}):
            assert no_collapse_patch_types(junk) == frozenset()
            assert origin_scoped_patch_types(junk) == frozenset()

    def test_the_flag_is_not_the_longitudinal_flag(self):
        # `longitudinal` carries series identity (a descriptor field, a
        # patch_observations history) and is wired only into structured
        # ingest. Overloading it here would have asserted a series that
        # does not exist.
        behavior = next(
            pt for pt in MANIFEST["patch_types"] if pt["domain_type"] == BEHAVIOR
        )
        assert "longitudinal" not in behavior
        assert "series_descriptor_field" not in behavior


# ============================================================
# 5. The origin stamp: no origin_id, no receipts, no cluster
# ============================================================


class TestOriginScoped:
    def test_behavior_declares_origin_scoped_without_project_scope(self):
        behavior = next(
            pt for pt in MANIFEST["patch_types"] if pt["domain_type"] == BEHAVIOR
        )
        assert behavior["origin_scoped"] is True
        assert behavior["project_scoped"] is False
        assert origin_scoped_patch_types(MANIFEST) == {BEHAVIOR}

    def test_no_shipped_ss_type_gains_an_origin_stamp(self):
        # Every SS type that already carried an origin carried it because
        # it is project scoped. The new flag must add nothing to them.
        for pt in MANIFEST["patch_types"]:
            if pt["domain_type"] == BEHAVIOR:
                continue
            assert pt.get("origin_scoped") is None


# ============================================================
# 3. The two sanitizers that touch person-shaped patches
# ============================================================


class TestSanitizerInteractions:
    def test_you_marker_is_stripped_from_observation_text_and_owner(self):
        content = {
            "patches": [
                _observation(
                    "Pushed back when Scott (you) proposed the Friday cutover",
                    owner="Denby (you)",
                )
            ]
        }
        sanitize_you_marker_from_patches(content)
        value = content["patches"][0]["value"]
        assert value["text"] == "Pushed back when Scott proposed the Friday cutover"
        assert value["owner"] == "Denby"

    def test_observation_about_the_user_is_stored_and_never_clustered(self):
        # The decision, pinned: an observation whose owner is the (you)
        # speaker is KEPT, and simply never reaches a lens. The ownership
        # enforcer refuses to mint a self person patch, the sanitizer
        # drops any the model emitted, and the profile pass excludes the
        # self person by design. So the record keeps what was observed and
        # no card can ever be built from it.
        content = {
            "patches": [
                _observation("Interrupted twice while the vendor was presenting", "Scott"),
                _person("Scott"),
            ]
        }
        enforce_person_ownership(content, user_label="Scott")
        drop_placeholder_and_self_person_patches(content, user_label="Scott")

        kinds = [p["type"] for p in content["patches"]]
        assert kinds == [BEHAVIOR]
        assert content["patches"][0]["value"]["text"] == (
            "Interrupted twice while the vendor was presenting"
        )

    def test_placeholder_owner_gets_no_person_patch(self):
        content = {"patches": [_observation("Talked over the summary", "Speaker 4")]}
        enforce_person_ownership(content, user_label="Scott")
        assert [p["type"] for p in content["patches"]] == [BEHAVIOR]


# ============================================================
# 4. Character, not behavior (guardrail 12b) at capture time
# ============================================================


class TestCharacterGuardrail:
    def test_character_verdict_is_dropped(self):
        content = {"patches": [_observation("Is insecure about review feedback", "Yardley")]}
        sanitize_behavior_observations(content)
        assert content["patches"] == []
        assert content["_behavior_observations_sanitized"]["count"] == 1
        assert content["_behavior_observations_sanitized"]["dropped"][0]["word"] == "insecure"

    def test_citable_conduct_survives(self):
        content = {
            "patches": [
                _observation("Reopens vague commitments at the next standup", "Yardley")
            ]
        }
        sanitize_behavior_observations(content)
        assert len(content["patches"]) == 1
        assert "_behavior_observations_sanitized" not in content

    def test_dangling_owns_edge_is_stripped_with_the_dropped_observation(self):
        # Without this the Pass-2 resolver in store_connected_patches
        # finds an unresolved target and synthesizes a stub patch that
        # puts the dropped verdict straight back into the quilt.
        content = {
            "patches": [
                _observation("Is arrogant in design review", "Yardley"),
                _person(
                    "Yardley",
                    [{
                        "role": "informs", "label": "owns",
                        "target_type": BEHAVIOR,
                        "target_text": "Is arrogant in design review",
                    }],
                ),
            ]
        }
        sanitize_behavior_observations(content)
        assert [p["type"] for p in content["patches"]] == ["person"]
        assert content["patches"][0]["connects_to"] == []

    def test_other_types_are_untouched_by_the_character_rule(self):
        # A takeaway is allowed to say whatever the meeting said. The
        # sanitizer only governs the type whose job is describing a human.
        content = {
            "patches": [
                {"type": "takeaway", "value": {"text": "The vendor demo was sloppy"}},
                {"type": "commitment", "value": {"text": "Lazy loading lands Friday"}},
            ]
        }
        before = json.dumps(content, sort_keys=True)
        sanitize_behavior_observations(content)
        assert json.dumps(content, sort_keys=True) == before

    def test_the_denylist_is_word_bounded(self):
        assert character_word_in("difficulty scaling the queue") is None
        assert character_word_in("smarter routing", CHARACTER_TRAIT_WORDS) is None
        assert character_word_in("smart routing", CHARACTER_TRAIT_WORDS) == "smart"

    def test_the_lens_denylist_is_unchanged_by_the_extension(self):
        # The shipped follow-through lens declines on CHARACTER_WORDS and
        # only those. Widening what it refuses is a live surface change
        # that has nothing to do with delivery records.
        assert character_word_in("Comes across as defensive") is None
        assert character_word_in("Comes across as defensive", CHARACTER_TRAIT_WORDS) == "defensive"
        assert "unreliable" in CHARACTER_WORDS and "unreliable" not in CHARACTER_TRAIT_WORDS

    def test_the_type_set_is_declared(self):
        assert BEHAVIOR_OBSERVATION_TYPES == {BEHAVIOR}


# ============================================================
# The facet decision, and what it keeps the type out of
# ============================================================


class TestFacetPlacement:
    def test_episode_keeps_observations_out_of_project_less_recall(self):
        # Episode, not Attribute, is the load-bearing half of the design:
        # universal_recall_types admits a non project scoped type only
        # when its facet is a freshness facet. Behavioral observations
        # about third parties must not arrive in a recall block that has
        # no project context.
        assert "Episode" not in FRESHNESS_FACETS
        rows = [{
            "type_key": BEHAVIOR, "facet": "Episode",
            "is_completable": False, "project_scoped": False,
            # 90 days: the permanence to TTL mapping for "quarter" lives
            # in routers/app_schemas.py, which cannot be imported in this
            # environment (it pulls fastapi and asyncpg).
            "default_ttl_days": 90,
        }]
        runtime = build_type_runtime(rows)
        assert BEHAVIOR not in runtime.universal_recall_types
        assert BEHAVIOR not in runtime.freshness_tracked_types
        assert BEHAVIOR not in runtime.project_scoped_types
        # Not completable, so it can never join an owe ledger, the open
        # counts, or the signals block, all of which gate on this set.
        assert not runtime.is_completable(BEHAVIOR)
        # It does decay: 90 days without a fresh observation, which is
        # guardrail 4 of the design expressed as a TTL.
        assert BEHAVIOR in runtime.decaying_types

    def test_manifest_declares_the_facet_and_permanence_that_produce_that(self):
        behavior = next(
            pt for pt in MANIFEST["patch_types"] if pt["domain_type"] == BEHAVIOR
        )
        assert behavior["facet"] == "Episode"
        assert behavior["permanence"] == "quarter"
        assert behavior["completable"] is False
        assert behavior["self_only"] is False


# ============================================================
# Wiring to the lenses, and keeping the readiness numbers honest
# ============================================================


class TestLensWiring:
    def test_the_person_rule_reads_observations(self):
        rule = person_insight_rule(MANIFEST)
        assert rule is not None
        assert BEHAVIOR in rule["from_types"]

    def test_observations_are_connectable(self):
        # A type absent from every label's from_types/to_types has all its
        # edges dropped by enforce_connection_vocabulary, which would take
        # the owns edge with them.
        owns = next(
            lb for lb in MANIFEST["connection_labels"] if lb["label"] == "owns"
        )
        assert BEHAVIOR in owns["to_types"]
        assert owns["from_types"] == ["person"]

    def test_readiness_counts_observations_for_model_lenses_only(self):
        rule = person_insight_rule(MANIFEST)
        rows = [
            {"patch_id": f"p{i}", "origin_id": f"m{i}", "status": "active"}
            for i in range(4)
        ]
        readiness = build_insight_readiness(
            rows, [], today=datetime.date(2026, 8, 13),
            min_patches=rule["min_patches"], min_meetings=rule["min_meetings"],
        )
        by_lens = {entry["lens"]: entry for entry in readiness["lenses"]}
        # The model lenses count what the cluster SQL counts, so adding
        # observations to from_types moves these numbers, correctly.
        assert by_lens["how_they_decide"]["items_observed"] == 4
        assert by_lens["how_they_decide"]["meetings_observed"] == 4
        # The computed lens counts items with a due date that has come
        # due. An observation has no deadline, so it must not inflate a
        # delivery record that would then be quoted in a claim.
        assert by_lens["how_they_follow_through"]["items_observed"] == 0
        assert by_lens["how_they_follow_through"]["meetings_observed"] == 0
