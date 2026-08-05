"""
Remove `owns` edges that assert ownership the extraction cannot justify.

Background
----------

`owns` is the only person-to-item label ShoulderSurf's manifest defines.
Any other involvement the extractor recognises but cannot name (a
counterparty, someone supplying a precondition) therefore lands as a
second owner, and nothing downstream can tell it from the real one.

The case that surfaced this (ABM, meeting of 2026-07-28): the commitment
"Configure IP whitelisting for new non-prod environment; coordinate with
Pradeep on turnaround time once IP address is provided" carried
`value.owner = "Pradeep"` plus `owns` edges from BOTH Pradeep and
Jayanth. The transcript is unambiguous that Pradeep does the whitelisting
and Jayanth supplies the IP address it waits on. The app rendered both as
owners because that is exactly what was stored.

`enforce_owner_edge_agreement` closes the forward path. This backfills the
rows written before it existed.

What it does
------------

For every active commitment / blocker / decision / goal carrying MORE THAN
ONE `owns` edge, it deletes the edges whose person is not the patch's
stated `value.owner`.

The decision rules are imported from the live sanitizer module
(`_split_compound_owner`, `_is_real_person_owner`) rather than restated
here, so the backfill cannot drift from the forward path.

Conservatism, in the same spirit as the sanitizer plus one rule the
sanitizer does not need:

  - A patch with no usable stated owner is skipped entirely. With no
    stated owner there is nothing to filter against, and dropping edges
    would discard the patch's only ownership signal.
  - Compound owners ("Srikanth/Ella") keep every matching edge.
  - **Never orphan.** If applying the rule would leave a patch with zero
    `owns` edges, the patch is skipped and reported. On the forward path
    `enforce_person_ownership` runs first and guarantees the owner's edge
    exists, so the sanitizer cannot orphan anything. Historical rows carry
    no such guarantee: an owner string of "Product team" against a person
    patch named "Product team (Yogaramya)" fails an exact match, and
    deleting on that basis would destroy the association rather than
    correct it.

Known weakness: the (you)-speaker guard is inert here
-----------------------------------------------------

`_is_real_person_owner` takes a `user_label` so an item owned by the (you)
speaker is left alone. On the forward path the worker is handed that label
by the caller. A backfill has no such caller, so this script looks it up
from `profiles.display_name` by subject key, and that lookup does not
resolve on current prod data (subject keys look like `user:<uuid>`; the
join comes back NULL). The guard is therefore decorative rather than
load bearing.

It does not matter, because the never-orphan rule below covers the case
it was meant to catch. An item owned by the (you) speaker with edges from
two other people would compute an allowed set of just the speaker, mark
both edges doomed, reach zero survivors, and be skipped. Verified on the
production run: every item that was acted on had a real named third-party
owner, and no `Scott`-owned item was touched. Fix the lookup if this
script is ever reused against data where that coincidence does not hold.

Archival, and the bump that makes it visible
--------------------------------------------

Migration 32 gave `patch_connections` a status column, so removals archive
rather than delete and stay auditable. The first run of this script
predated that and hard-deleted 14 edges, which turned out to be
unreconstructable afterwards. That is the reason the column exists.

Archival alone does not reach anyone. An archived edge and a deleted edge
look identical to a client, because both stop appearing in the payload.
The delta filters on `updated_at` and connections are fetched
outgoing-only, so a removal is only visible once the FROM-side patch is
touched. This script therefore does both: archive the edge, then bump the
person patch that owned it.

Bumping the from-side patch is cheap here. Person patches anchor decay on
plain `updated_at` with no deadline interaction, and the record genuinely
did change.

Read-only by default; `--apply` writes. One transaction per patch.

Usage
-----

    python scripts/backfill_owner_edge_agreement.py            # dry run
    python scripts/backfill_owner_edge_agreement.py --apply    # writes
"""

import argparse
import asyncio
import os
import sys

import asyncpg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.contextquilt.services.extraction_schema import (  # noqa: E402
    PERSON_OWNED_ACTION_TYPES,
    _is_real_person_owner,
    _split_compound_owner,
)

MULTI_OWNER_ITEMS = """
    SELECT t.patch_id,
           t.patch_type,
           t.value->>'text'  AS text,
           t.value->>'owner' AS owner,
           p.display_name    AS user_label
      FROM patch_connections pc
      JOIN context_patches f ON f.patch_id = pc.from_patch_id
      JOIN context_patches t ON t.patch_id = pc.to_patch_id
      JOIN patch_subjects ps ON ps.patch_id = t.patch_id
      LEFT JOIN profiles p ON p.user_id = ps.subject_key
     WHERE pc.connection_label = 'owns'
       AND f.patch_type = 'person'
       AND t.patch_type = ANY($1::text[])
       AND COALESCE(t.status, 'active') = 'active'
     GROUP BY t.patch_id, t.patch_type, t.value, p.display_name
    HAVING count(*) > 1
     ORDER BY t.patch_id
"""

OWNER_EDGES = """
    SELECT pc.connection_id,
           pc.from_patch_id,
           f.value->>'text' AS person,
           pc.created_at
      FROM patch_connections pc
      JOIN context_patches f ON f.patch_id = pc.from_patch_id
     WHERE pc.to_patch_id = $1
       AND pc.connection_label = 'owns'
       AND f.patch_type = 'person'
     ORDER BY pc.created_at, pc.connection_id
"""


def _norm(s):
    return (s or "").strip().lower()


async def main(apply: bool) -> int:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    items = await conn.fetch(MULTI_OWNER_ITEMS, list(PERSON_OWNED_ACTION_TYPES))
    print(f"multi-owner active action items: {len(items)}\n")

    deleted = kept = skipped_no_owner = skipped_would_orphan = 0

    for it in items:
        edges = await conn.fetch(OWNER_EDGES, it["patch_id"])
        allowed = {
            _norm(name)
            for name in _split_compound_owner(it["owner"])
            if _is_real_person_owner(name, it["user_label"])
        }
        head = f'[{it["patch_type"]}] {(it["text"] or "")[:64]!r}'
        people = ", ".join(e["person"] or "?" for e in edges)

        if not allowed:
            skipped_no_owner += 1
            print(f"SKIP  no usable stated owner  {head}")
            print(f"      owner={it['owner']!r} edges=[{people}]\n")
            continue

        doomed = [e for e in edges if _norm(e["person"]) not in allowed]
        survivors = len(edges) - len(doomed)

        if not doomed:
            kept += len(edges)
            continue

        if survivors == 0:
            skipped_would_orphan += 1
            print(f"SKIP  would orphan            {head}")
            print(f"      owner={it['owner']!r} edges=[{people}]")
            print("      no edge matches the stated owner; leaving all in place\n")
            continue

        print(f"{'DELETE' if apply else 'WOULD DELETE'}  {head}")
        print(f"      stated owner: {it['owner']!r}")
        for e in doomed:
            print(f"      drop edge from {e['person']!r} (created {e['created_at']})")
        print(f"      keeping {survivors} edge(s)\n")

        if apply:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE patch_connections SET status = 'archived' "
                    "WHERE connection_id = ANY($1::uuid[])",
                    [e["connection_id"] for e in doomed],
                )
                # Bump the from-side patch so the removal actually reaches
                # clients. Archiving is for us; the delta filters on
                # updated_at and connections are fetched outgoing-only, so
                # without this the correction is invisible forever.
                await conn.execute(
                    "UPDATE context_patches SET updated_at = NOW() "
                    "WHERE patch_id = ANY($1::uuid[])",
                    list({e["from_patch_id"] for e in doomed}),
                )
        deleted += len(doomed)
        kept += survivors

    print("-" * 60)
    print(f"edges {'deleted' if apply else 'that would be deleted'}: {deleted}")
    print(f"edges kept:                          {kept}")
    print(f"items skipped, no usable owner:      {skipped_no_owner}")
    print(f"items skipped, would orphan:         {skipped_would_orphan}")
    if not apply:
        print("\nDRY RUN. Re-run with --apply to write.")
    await conn.close()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write; default is a dry run")
    sys.exit(asyncio.run(main(ap.parse_args().apply)))
