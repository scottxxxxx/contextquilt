"""
Verifies that PR #77 (identification_source consumer) is working end-to-end
against live traffic.

Reads recent extraction_metrics rows and reports:
  - count of rows since cutoff
  - breakdown of identification_source values (None / "enrollment" /
    "user_confirmation" / "none" / etc.)
  - breakdown of user_identified values
  - a few sample rows showing the new fields populated alongside the
    existing owner_speaker_label / owner_marker_present

Read-only. Safe to run against prod.

    DATABASE_URL=postgresql://...  python scripts/verify_identification_source.py [--hours N]
"""

import argparse
import asyncio
import os
from collections import Counter

import asyncpg

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/context_quilt"
)


async def verify(hours: int) -> None:
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        total = await conn.fetchval(
            """
            SELECT COUNT(*) FROM extraction_metrics
            WHERE created_at > NOW() - ($1 || ' hours')::INTERVAL
            """,
            str(hours),
        )
        print(f"Rows in last {hours}h: {total}")

        if total == 0:
            print("No extraction activity in the window. Nothing to verify.")
            return

        rows = await conn.fetch(
            """
            SELECT
                created_at, app_id, model, owner_speaker_label,
                owner_marker_present, user_identified, identification_source
            FROM extraction_metrics
            WHERE created_at > NOW() - ($1 || ' hours')::INTERVAL
            ORDER BY created_at DESC
            """,
            str(hours),
        )

        src_counts = Counter(r["identification_source"] for r in rows)
        ident_counts = Counter(r["user_identified"] for r in rows)
        owner_counts = Counter(bool(r["owner_speaker_label"]) for r in rows)

        print()
        print("identification_source breakdown:")
        for k, v in sorted(src_counts.items(), key=lambda kv: (-kv[1], str(kv[0]))):
            label = "<NULL>" if k is None else repr(k)
            print(f"  {label:30}  {v}")

        print()
        print("user_identified breakdown:")
        for k, v in sorted(ident_counts.items(), key=lambda kv: (-kv[1], str(kv[0]))):
            label = "<NULL>" if k is None else repr(k)
            print(f"  {label:10}  {v}")

        print()
        print("owner_speaker_label populated:")
        for k, v in owner_counts.items():
            print(f"  {k}  {v}")

        # Sample 5 most recent rows that have ANY of the new fields populated
        sampled = [
            r for r in rows
            if r["identification_source"] is not None or r["user_identified"] is not None
        ][:5]

        print()
        if sampled:
            print(f"Sample rows with new fields populated ({len(sampled)} of {total}):")
            for r in sampled:
                print(
                    f"  {r['created_at'].isoformat()}  "
                    f"app={r['app_id']}  "
                    f"src={r['identification_source']!r}  "
                    f"identified={r['user_identified']}  "
                    f"owner_label={r['owner_speaker_label']!r}  "
                    f"marker_present={r['owner_marker_present']}"
                )
        else:
            print(
                "No rows in the window have identification_source or user_identified "
                "populated. Either the migration (init-db/17) has not run on this "
                "database, OR the worker code consuming the fields is not deployed, "
                "OR no upstream traffic in the window carried the new metadata."
            )

        # Final verdict
        new_field_present = any(
            r["identification_source"] is not None or r["user_identified"] is not None
            for r in rows
        )
        print()
        if new_field_present:
            print(
                "PASS: PR #77 consumer is writing the new fields against live traffic."
            )
        else:
            print(
                "INDETERMINATE: no new-field rows. See note above; this does not by "
                "itself prove the consumer is broken."
            )
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--hours",
        type=int,
        default=24,
        help="Lookback window in hours (default: 24)",
    )
    args = parser.parse_args()
    asyncio.run(verify(args.hours))


if __name__ == "__main__":
    main()
