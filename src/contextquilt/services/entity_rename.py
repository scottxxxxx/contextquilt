"""Renaming an entity that is not a person, and what happens when the
name is already taken.

THE CASE (doc 24, open item 2). Speech-to-text heard "Netomi" as
"Mitomi", so the company entity `61317fa9` is named Mitomi. Scott
corrected it in chat on 2026-09-12: the correction worked exactly as
designed, archived the old org patch and landed a declared one saying
"The company name is Netomi", and the ENTITY was untouched, because
corrections operate on `context_patches` and never reach `entities`.
So every future extraction saying "Netomi" mints a SECOND company
rather than resolving to the first, and every stored description that
names the company still says Mitomi.

`POST /v1/people/{u}/{entity_id}/rename` cannot do this: it is gated on
the person entity type throughout and 404s for a company. Doc 24 left
the org rename unbuilt and named the trap for whoever built it, which
is the whole reason this module exists:

  `entities` IS UNIQUE ON (user_id, name, entity_type). A rename onto a
  name that already exists for that type COLLIDES, so the operation is
  not "rename" at all. It is "rename, or merge when the target is
  already there", and a caller cannot know which in advance.

So the plan is computed from what is actually in the table and reported
before anything is written. Three outcomes:

  NOOP    the entity already has this name.
  RENAME  no other entity of this type holds the name. The row is
          renamed and the OLD NAME BECOMES AN ALIAS, so the mis-heard
          spelling still resolves: five months of transcripts say
          "Mitomi" and they are not wrong about what was said.
  MERGE   another entity of this type already holds the name. The row
          being renamed is folded INTO it: aliases repoint, its name
          becomes an alias, relationships repoint (skipping edges the
          survivor already has, because `relationships` is unique on
          user+from+to+type and a blind UPDATE would throw), and the
          loser keeps its row with `merged_into` set. A forward pointer,
          never a delete, the same shape `merge_people` uses and the
          same reason: recall resolves through the pointer, and a
          deleted row cannot.

WHY ADMIN-GATED RATHER THAN AN APP ROUTE. A new app-facing verb is a
two-sided release with GhostPour deploying first, and this is a repair
for data already on disk, not a feature the app asks for. The admin key
reaches it directly on the box. If it ever becomes a product affordance
("this company's name is wrong"), the mechanics are here and the route
is a thin caller.

WHAT IT DOES NOT DO, and this is deliberate: it does not touch
`entities.description`, any patch text, or any `entity_descriptions`
row. A description saying "HR lead for North America at Mitomi" still
says Mitomi afterwards. Mechanical substitution of the old name inside
stored text is a separate, auditable operation (doc 24 again) and
mixing the two would make one irreversible-looking write out of two
decisions. The response says so on the wire rather than in this
docstring, because a caller cannot read comments.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

NOOP = "noop"
RENAME = "rename"
MERGE = "merge"

TEXT_UNTOUCHED = (
    "Stored text is not rewritten: descriptions, patch text and the "
    "description series still contain the old name wherever a meeting "
    "said it. Only the entity's name and its aliases change."
)

# The row being renamed. Merged-away rows are refused: renaming a
# forward pointer moves a name nothing resolves to.
SUBJECT_SQL = """
    SELECT entity_id, name, entity_type, merged_into, mention_count
    FROM entities
    WHERE user_id = $1 AND entity_id = $2::uuid
"""

# Does another LIVE entity of this type already hold the target name?
# Case-insensitive, because "netomi" and "Netomi" are the same company
# and the unique index is on the exact name: an insensitive match that
# the index would not have caught is still a duplicate a human means to
# collapse.
TARGET_SQL = """
    SELECT entity_id, name, mention_count
    FROM entities
    WHERE user_id = $1 AND entity_type = $2
      AND LOWER(name) = LOWER($3)
      AND entity_id <> $4::uuid
      AND merged_into IS NULL
    ORDER BY mention_count DESC NULLS LAST, entity_id
    LIMIT 1
"""

RENAME_SQL = """
    UPDATE entities SET name = $3, last_seen_at = last_seen_at
     WHERE user_id = $1 AND entity_id = $2::uuid
"""

# The old spelling still resolves. ON CONFLICT DO UPDATE rather than DO
# NOTHING: if that surface form pointed elsewhere, this is the operator
# saying where it actually belongs.
ALIAS_SQL = """
    INSERT INTO entity_aliases (user_id, entity_id, alias, source)
    VALUES ($1, $2::uuid, $3, $4)
    ON CONFLICT (user_id, LOWER(alias))
    DO UPDATE SET entity_id = EXCLUDED.entity_id, source = EXCLUDED.source
"""

ALIASES_REPOINT_SQL = """
    UPDATE entity_aliases SET entity_id = $1::uuid
     WHERE user_id = $2 AND entity_id = $3::uuid
"""

MARK_MERGED_SQL = """
    UPDATE entities SET merged_into = $1::uuid, merged_at = NOW()
     WHERE user_id = $2 AND entity_id = $3::uuid
"""

RELATIONSHIP_REPOINT_SQL = """
    UPDATE relationships r SET {column} = $1::uuid
     WHERE r.user_id = $2 AND r.{column} = $3::uuid
       AND NOT EXISTS (
           SELECT 1 FROM relationships r2
            WHERE r2.user_id = r.user_id
              AND r2.{column} = $1::uuid
              AND r2.{other} = r.{other}
              AND r2.relationship_type = r.relationship_type
       )
"""

RELATIONSHIP_CLEANUP_SQL = """
    DELETE FROM relationships
     WHERE user_id = $1
       AND (from_entity_id = $2::uuid OR to_entity_id = $2::uuid)
"""

SELF_LOOP_CLEANUP_SQL = """
    DELETE FROM relationships
     WHERE user_id = $1 AND from_entity_id = $2::uuid AND to_entity_id = $2::uuid
"""


def clean_name(raw: Any) -> str:
    return raw.strip() if isinstance(raw, str) else ""


def plan(
    subject: Optional[Mapping[str, Any]],
    target: Optional[Mapping[str, Any]],
    new_name: str,
) -> Dict[str, Any]:
    """What this rename would do, decided from the rows themselves.

    Returns {"action", "reason", ...}. `action` is NOOP, RENAME or
    MERGE; a refusal raises ValueError, because "cannot" and "would do
    nothing" are different answers and a caller that conflates them
    reports success for a missing entity.
    """
    name = clean_name(new_name)
    if not name:
        raise ValueError("new_name is required")
    if subject is None:
        raise ValueError("entity not found for this user")
    if subject.get("merged_into"):
        raise ValueError(
            "this entity was already merged into another; rename the survivor"
        )

    current = clean_name(subject.get("name"))
    if current.lower() == name.lower() and target is None:
        return {
            "action": NOOP,
            "reason": "the entity already has this name",
            "name": current,
        }

    if target is None:
        return {
            "action": RENAME,
            "reason": "no other entity of this type holds that name",
            "from": current,
            "to": name,
        }

    return {
        "action": MERGE,
        "reason": (
            "another entity of this type already holds that name, and "
            "entities is unique on (user_id, name, entity_type), so the "
            "rename is a merge into it"
        ),
        "from": current,
        "to": clean_name(target.get("name")) or name,
        "survivor_entity_id": str(target.get("entity_id")),
    }


def response(plan_result: Mapping[str, Any], *, applied: bool,
             aliases_repointed: int = 0, relationships_repointed: int = 0) -> Dict[str, Any]:
    """The body, identical in shape for preview and apply so a caller can
    compare the two. `applied` is the only field that changes meaning."""
    body = dict(plan_result)
    body["applied"] = bool(applied)
    body["aliases_repointed"] = int(aliases_repointed)
    body["relationships_repointed"] = int(relationships_repointed)
    body["limits"] = TEXT_UNTOUCHED
    return body
