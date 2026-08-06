"""
Give the user's existing open commitments a counterparty.

Background
----------

Manifest v9 adds the `owed_to` connection label, which is the only thing
in the vocabulary that can say the (you) speaker owes a named person
something. It fills FORWARD only. Everything already in the quilt was
extracted under a vocabulary that had no counterparty at all, so on the
day v9 registers, the People ledger's left column is empty for a user
whose entire history of obligations predates the label.

That is not a cosmetic gap. `capabilities.you_owe` flips to available the
moment the app's manifest declares `owed_to`, and from then on an empty
`you_owe` means "nothing outstanding" rather than "CQ cannot tell". Ship
the label without this backfill and every person card confidently says
the user owes them nothing.

What it does
------------

For each ACTIVE completable that belongs to the USER (`is_self_owned`, the
same predicate the read surface uses), one batched LLM call per user picks
the person it is owed to from a CLOSED list of that user's known people,
or answers null. A verdict becomes an `owed_to` connection from the item
to that person's `person` patch.

Conservative by construction, in four places:

  - **Closed candidate list.** The judge answers with a name from the
    user's own people or with nothing. A backfill has no business creating
    person patches, so an unresolvable name is discarded, not synthesized.
  - **Null is the default.** The judge prompt says so and
    `parse_counterparty_verdicts` enforces it: malformed, out of range, or
    off-list all resolve to None. A missing counterparty leaves the item
    where it already is; a wrong one tells the user they owe something to
    somebody they do not.
  - **The live sanitizer has the last word.** Every proposed edge is run
    through `enforce_owed_to_counterparty` before it is written, so the
    backfill cannot produce a shape the forward path would have rejected
    (owed to its own owner, owed to the user, owed to a placeholder). The
    rules are imported, never restated, so the two cannot drift.
  - **Only items the user owns.** A third party's obligation is not the
    user's ledger. `is_self_owned` is an inclusion: an owner string CQ
    cannot resolve stays out.

The lifecycle side effect, on purpose
-------------------------------------

Writing the edge bumps `updated_at` on the ITEM, not on the person. Quilt
connections are fetched outgoing-only (`WHERE pc.from_patch_id = ANY(...)`)
and `owed_to` runs item to person, so the item is the from-side and the
only channel this correction has. That is the standing rule from the
08-05 propagation fix.

Commitments anchor decay on `GREATEST(updated_at, deadline_date)`, so the
bump does extend their life. Accepted deliberately: these are the user's
own OPEN obligations, we really did modify them, and quietly decaying
something the user still owes somebody is the worse failure.

A correction to backfill_owner_edge_agreement.py's note
-------------------------------------------------------

That script reports its (you)-speaker guard as inert because
`profiles.display_name` would not resolve. The reason is the join, not the
data: it joins `profiles.user_id` against `patch_subjects.subject_key`,
which carries the `user:` prefix. `profiles.user_id` holds the bare id and
resolves fine when joined against that. This script strips the prefix and
the guard is load bearing here.

Read-only by default; `--apply` writes. One transaction per item.

Usage
-----

    python scripts/backfill_owed_to.py                    # dry run, all users
    python scripts/backfill_owed_to.py --user <uuid>      # one user
    python scripts/backfill_owed_to.py --apply            # writes
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from contextquilt.services.counterparty import (  # noqa: E402
    COUNTERPARTY_JUDGE_SCHEMA,
    COUNTERPARTY_JUDGE_SYSTEM,
    MAX_JUDGE_ITEMS,
    build_counterparty_content,
    parse_counterparty_verdicts,
)
from contextquilt.services.extraction_schema import (  # noqa: E402
    enforce_owed_to_counterparty,
)
from contextquilt.services.people_identity import is_self_owned  # noqa: E402

COMPLETABLES = ["commitment", "blocker"]

# Subjects that hold at least one open completable, with the display name
# behind the (you) guard. Joined on the bare id, see the module docstring.
SUBJECTS = """
    SELECT ps.subject_key,
           p.display_name AS user_label,
           count(*)       AS open_items
      FROM context_patches cp
      JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
      LEFT JOIN profiles p
             ON p.user_id = regexp_replace(ps.subject_key, '^user:', '')
     WHERE cp.patch_type = ANY($1::text[])
       AND COALESCE(cp.status, 'active') = 'active'
     GROUP BY ps.subject_key, p.display_name
     ORDER BY ps.subject_key
"""

OPEN_ITEMS = """
    SELECT cp.patch_id,
           cp.patch_type,
           cp.value->>'text'  AS text,
           cp.value->>'owner' AS owner
      FROM context_patches cp
      JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
     WHERE ps.subject_key = $1
       AND cp.patch_type = ANY($2::text[])
       AND COALESCE(cp.status, 'active') = 'active'
     ORDER BY cp.created_at, cp.patch_id
"""

# The closed candidate list. Person PATCHES, not entities: the edge has to
# land on a patch, and a person entity with no patch behind it has nothing
# to connect to.
PERSON_PATCHES = """
    SELECT cp.patch_id, cp.value->>'text' AS name
      FROM context_patches cp
      JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
     WHERE ps.subject_key = $1
       AND cp.patch_type = 'person'
       AND COALESCE(cp.status, 'active') = 'active'
     ORDER BY cp.value->>'text'
"""

EXISTING_EDGE = """
    SELECT 1 FROM patch_connections
     WHERE from_patch_id = $1 AND to_patch_id = $2
       AND connection_label = 'owed_to'
       AND COALESCE(status, 'active') = 'active'
     LIMIT 1
"""

# Same ON CONFLICT shape as the live insert paths. The unique index spans
# (from, to, role) regardless of status, so an archived row would otherwise
# swallow the insert and stay archived forever.
INSERT_EDGE = """
    INSERT INTO patch_connections
        (from_patch_id, to_patch_id, connection_role, connection_label)
    VALUES ($1::uuid, $2::uuid, 'informs', 'owed_to')
    ON CONFLICT (from_patch_id, to_patch_id, connection_role)
    DO UPDATE SET status = 'active'
     WHERE patch_connections.status <> 'active'
"""


def _survives_sanitizer(item, name, user_label) -> bool:
    """Run the proposed edge through the live forward-path sanitizer.

    Rebuilds the shape the worker would have handed it (an item patch with
    one `owed_to` edge) and checks the edge is still there afterwards. The
    backfill must not be able to write something extraction would refuse.
    """
    content = {
        "patches": [
            {
                "type": item["patch_type"],
                "value": {"text": item["text"], "owner": item["owner"]},
                "connects_to": [
                    {
                        "target_text": name,
                        "target_type": "person",
                        "role": "informs",
                        "label": "owed_to",
                    }
                ],
            }
        ]
    }
    enforce_owed_to_counterparty(content, user_label=user_label)
    return bool(content["patches"][0]["connects_to"])


async def run_subject(conn, llm, subject_key, user_label, apply):
    items = await conn.fetch(OPEN_ITEMS, subject_key, COMPLETABLES)
    mine = [r for r in items if is_self_owned(r["owner"], user_label)]
    people = await conn.fetch(PERSON_PATCHES, subject_key)

    print(f"\n{subject_key}  (display_name={user_label!r})")
    print(f"  open completables: {len(items)}   the user's own: {len(mine)}"
          f"   candidate people: {len(people)}")

    if not mine or not people:
        print("  nothing to judge")
        return 0, 0

    names = [r["name"] for r in people if (r["name"] or "").strip()]
    patch_by_name = {r["name"].strip().lower(): r["patch_id"] for r in people if r["name"]}

    verdicts: list = []
    for i in range(0, len(mine), MAX_JUDGE_ITEMS):
        batch = mine[i:i + MAX_JUDGE_ITEMS]
        try:
            resp = await llm.extract(
                system_prompt=COUNTERPARTY_JUDGE_SYSTEM,
                user_content=build_counterparty_content(
                    [r["text"] for r in batch], names
                ),
                json_schema=COUNTERPARTY_JUDGE_SCHEMA,
            )
            verdicts.extend(
                parse_counterparty_verdicts(resp.content, len(batch), names)
            )
        except Exception as exc:
            # A judge failure writes nothing for this batch. Same posture
            # as semantic dedup: the degraded state is today's state.
            print(f"  JUDGE FAILED on batch {i}: {type(exc).__name__}: {str(exc)[:160]}")
            verdicts.extend([None] * len(batch))

    written = vetoed = 0
    for item, name in zip(mine, verdicts):
        if not name:
            continue
        if not _survives_sanitizer(item, name, user_label):
            vetoed += 1
            print(f"  VETO   {(item['text'] or '')[:64]!r}")
            print(f"         judge said {name!r}; the live sanitizer rejects it")
            continue
        target = patch_by_name.get(name.strip().lower())
        if target is None:
            vetoed += 1
            continue
        if await conn.fetchval(EXISTING_EDGE, item["patch_id"], target):
            continue

        print(f"  {'WRITE ' if apply else 'WOULD '} {(item['text'] or '')[:64]!r}")
        print(f"         owner={item['owner']!r}  owed_to={name!r}")
        if apply:
            async with conn.transaction():
                await conn.execute(INSERT_EDGE, item["patch_id"], target)
                # The item is the from-side of the edge and connections are
                # fetched outgoing-only, so this is the only channel the
                # new fact has. See the module docstring on decay.
                await conn.execute(
                    "UPDATE context_patches SET updated_at = NOW() WHERE patch_id = $1",
                    item["patch_id"],
                )
        written += 1

    print(f"  {'wrote' if apply else 'would write'}: {written}   vetoed: {vetoed}")
    return written, vetoed


async def main(apply: bool, only_user: str | None) -> None:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is required", file=sys.stderr)
        sys.exit(1)

    from contextquilt.services.llm_client_anthropic import AnthropicLLMClient
    llm = AnthropicLLMClient()

    conn = await asyncpg.connect(dsn)
    try:
        subjects = await conn.fetch(SUBJECTS, COMPLETABLES)
        if only_user:
            want = only_user if only_user.startswith("user:") else f"user:{only_user}"
            subjects = [s for s in subjects if s["subject_key"] == want]
            if not subjects:
                print(f"no open completables for {want}")
                return

        print(f"{'APPLY' if apply else 'DRY RUN'}: {len(subjects)} subject(s) with open items")

        total_written = total_vetoed = 0
        for s in subjects:
            w, v = await run_subject(
                conn, llm, s["subject_key"], s["user_label"], apply
            )
            total_written += w
            total_vetoed += v

        print("\n" + "-" * 60)
        print(f"edges {'written' if apply else 'that would be written'}: {total_written}")
        print(f"proposals vetoed:                     {total_vetoed}")
        if not apply:
            print("\nDry run. Re-run with --apply to write.")
    finally:
        await conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write the edges (default: dry run)")
    ap.add_argument("--user", default=None, help="limit to one user id or subject key")
    args = ap.parse_args()
    asyncio.run(main(args.apply, args.user))
