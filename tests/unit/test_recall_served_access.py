"""The route bumps what it SERVED, and says so on the wire (doc 25, finding 1).

Source-read, because main.py cannot import without fastapi and asyncpg.
The executing siblings are the formatter tests (what "served" means) and
test_recall_access_upsert_db.py (what the bump writes).
"""

from pathlib import Path

MAIN = (Path(__file__).resolve().parents[2] / "src" / "main.py").read_text()


def _recall_route() -> str:
    start = MAIN.index('@app.post("/v1/recall"')
    end = MAIN.index('@app.get("/v1/profile/{user_id}"')
    return MAIN[start:end]


def test_the_response_model_carries_served_ids_beside_matched():
    model = MAIN[MAIN.index("class RecallResponse"):][:1500]
    assert "matched_patch_ids: List[str] = []" in model, "the old field must stay"
    assert "served_patch_ids: List[str] = []" in model


def test_the_full_lane_bumps_served_ids_not_the_candidate_list():
    route = _recall_route()
    tail = route[route.rindex("_bump_patch_access("):]
    assert "served_patch_ids if RECALL_ACCESS_SERVED_ONLY else matched_patch_ids" in tail


def test_the_cache_hit_bumps_served_ids_and_falls_back_for_old_blobs():
    route = _recall_route()
    hit = route[route.index("if cached_blob:"):route.index("# Step 1:")]
    assert 'cached.get("served_patch_ids")' in hit
    assert 'cached.get("matched_patch_ids", [])' in hit, "an old blob must not bump nothing"
    assert "served_patch_ids=cached_served" in hit


def test_both_render_cache_blobs_carry_served_ids():
    route = _recall_route()
    assert route.count('"served_patch_ids":') == 2, "people lane and full lane"


def test_the_flat_formatter_is_the_served_variant():
    route = _recall_route()
    assert "format_flat_ranked_served(" in route
    assert "format_flat_ranked_with_stats(" not in route


def test_grouped_output_falls_back_to_the_candidate_list():
    route = _recall_route()
    assert "if served_patch_ids is None:" in route
    assert "served_patch_ids = matched_patch_ids" in route


def test_matched_patch_ids_is_untouched_on_the_wire():
    """ShoulderSurf's gated teaser reads its first five through a GP
    header; SS asked that it not move. The stale second scorer stays
    until they switch to served ids."""
    route = _recall_route()
    assert "matched_patch_ids = [pid for _, _, pid in scored]" in route
    assert "matched_patch_ids=matched_patch_ids" in route


def test_the_kill_switch_defaults_to_served_only():
    line = [l for l in MAIN.splitlines() if l.startswith("RECALL_ACCESS_SERVED_ONLY")][0]
    assert 'os.getenv("CQ_RECALL_ACCESS_SERVED_ONLY", "1")' in line


def test_the_bump_is_an_upsert_joined_to_context_patches():
    """Finding 3: two API lanes create patches with no metrics row, and
    an UPDATE could never reach them."""
    body = MAIN[MAIN.index("async def _bump_patch_access"):][:2500]
    assert "INSERT INTO patch_usage_metrics" in body
    assert "ON CONFLICT (patch_id) DO UPDATE" in body
    assert "JOIN context_patches cp ON cp.patch_id = ids.patch_id" in body
    assert "UPDATE patch_usage_metrics\n" not in body, "the old no-op statement is gone"
