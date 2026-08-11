"""owned_by_self on quilt completables + self_entity_id on insights.

The obligation-surfacing amendment (2026-08-11): the client renders
"you owe N on this project" from its synced store, so WHOSE item
something is must be computed exactly once, server-side, with the same
rule everywhere. These guards pin the tri-state honesty (null is
cannot-tell, false is someone else's, never collapsed), the shared
rule, and the degrade contract for lagging DBs.
"""

import re
from pathlib import Path

MAIN = (Path(__file__).resolve().parents[2] / "src" / "main.py").read_text()


def test_model_carries_the_field_with_null_default():
    m = re.search(r"class QuiltPatchResponse.*?class MeetingGroup", MAIN, re.DOTALL)
    assert m and "owned_by_self: Optional[bool] = None" in m.group(0)


def test_null_covers_no_ego_and_non_completables():
    """Null must mean cannot-tell: no self entity for the user, or a
    patch type ownership does not apply to. False is a different claim
    (someone else's) and the two must never collapse."""
    body = MAIN.split("def _owned_by_self")[1].split("for row in rows:")[0]
    assert "self_entity_id is None" in body
    assert "patch_type not in completable" in body
    assert "return None" in body


def test_same_rule_as_the_insights_rate():
    """Edge-first owns resolution, owner-text fallback, ownerless on the
    user's own quilt counts as theirs (reassign-speaker's to_self
    contract). The quilt chips and the follow-up rate must share this
    verdict or the surfaces drift."""
    body = MAIN.split("def _owned_by_self")[1].split("for row in rows:")[0]
    assert "owner_text_by_item" in body
    assert 'resolve_owner_entity(value.get("owner"))' in body
    assert re.search(r"return not value\.get\(.owner.\)", body)


def test_quilt_route_survives_a_pre_migration_db():
    """The self-entity lookup is guarded: on a DB without migration 35
    (the MCP deployment lags), owned_by_self serves null everywhere and
    the core quilt route must never 500 over it."""
    guard = MAIN.split("_self_row = await db_pool.fetchval")[1].split("def _owned_by_self")[0]
    assert "except Exception" in guard
    assert "self_entity_id = None" in guard


def test_insights_serves_the_self_entity_id():
    """SS asked for one bit of knowledge the client lacks: which entity
    is the user. Every insights return carries it explicitly, null only
    when no ego link exists."""
    body = MAIN.split("def quilt_insights")[1].split(
        '@app.delete("/v1/quilt/{user_id}/patches/{patch_id}"'
    )[0]
    assert body.count('"self_entity_id"') == 3
    assert '"self_entity_id": None' in body
    assert body.count('"self_entity_id": str(self_entity)') == 2
