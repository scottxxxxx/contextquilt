"""Judge stored behavior rows with the LIVE classifier; archive or retype.

`behavior_classifier` runs at INGEST from the day it ships. Everything
stored before it was typed by the model that wrote it, asked only about
conduct, and a judge shown the manifest's own definitions put 59% of a
120-row sample in another type (2026-09-01). This applies the same
instrument to history, so the live path and the backfill can never
disagree about what a behavior is.

THE DRY RUN IS THE MEASUREMENT. It prints, per app, how many rows the
classifier keeps as behavior, how many it converts to preference and
how many it assigns to a type the main extraction owns, with samples
of each, and it writes every verdict to `--out` so `--apply --from`
can act on exactly what was reviewed without judging again.

ARCHIVES, NEVER DELETES: `status='archived'`, `archive_cause='cleanup'`,
`archive_detail='classified_<type>'`, so the verdict is traceable on the
row and the row leaves clients through the delta-sync `deleted` array.
Preference verdicts RETYPE and attach `held_by` to an EXISTING person
patch only, exactly as backfill_behavior_sanitize does.

    python scripts/backfill_behavior_classify.py                 # dry run, all users
    python scripts/backfill_behavior_classify.py --user <id> --sample 120 --seed 7
    python scripts/backfill_behavior_classify.py --apply --from verdicts.json
"""

import argparse
import asyncio
import json
import os
import random
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import asyncpg  # noqa: E402

from contextquilt.services import behavior_classifier as bc  # noqa: E402

SELECT = """
    SELECT cp.patch_id::text AS patch_id, cp.value, ps.subject_key,
           acl.app_id::text AS app_id
      FROM context_patches cp
      JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
      LEFT JOIN LATERAL (
           SELECT app_id FROM context_patch_acl a
            WHERE a.patch_id = cp.patch_id
            ORDER BY a.can_write DESC, app_id LIMIT 1) acl ON TRUE
     WHERE COALESCE(cp.status, 'active') = 'active'
       AND cp.patch_type = 'moment'
       AND COALESCE(cp.value->>'text', '') <> ''
"""


def _value(raw):
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            out = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return out if isinstance(out, dict) else {}
    return {}


async def _manifests(pool) -> dict:
    """Latest manifest per app, only for apps that declare behavior."""
    rows = await pool.fetch(
        "SELECT DISTINCT ON (app_id) app_id::text AS app_id, version, manifest "
        "  FROM app_schemas ORDER BY app_id, version DESC")
    out = {}
    for r in rows:
        m = r["manifest"]
        m = json.loads(m) if isinstance(m, str) else m
        if isinstance(m, dict):
            m["_registry_version"] = r["version"]
        if any(isinstance(t, dict) and t.get("domain_type") == "moment"
               for t in (m or {}).get("patch_types", [])):
            out[r["app_id"]] = m
    return out


async def judge_rows(rows, manifest, llm) -> dict:
    """{patch_id: verdict or None}, batched at bc.MAX_ITEMS."""
    names = bc.classifier_types(manifest)
    system = bc.build_classifier_system(manifest)
    verdicts, cost = {}, 0.0
    for i in range(0, len(rows), bc.MAX_ITEMS):
        batch = rows[i:i + bc.MAX_ITEMS]
        patches = [{"type": "moment", "value": _value(r["value"])} for r in batch]
        try:
            resp = await llm.extract(system_prompt=system,
                                     user_content=bc.build_classifier_content(patches),
                                     model=bc.model_override())
            cost += float(getattr(resp, "cost_usd", 0.0) or 0.0)
            got = bc.parse_classifier_verdicts(resp.content, len(batch), names)
        except Exception as exc:  # fail open: None = keep
            print(f"  batch {i} failed, kept: {str(exc)[:120]}")
            got = [None] * len(batch)
        for r, v in zip(batch, got):
            verdicts[r["patch_id"]] = v
    print(f"  judge cost ${cost:.4f}")
    return verdicts


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--user", help="restrict to one user_id")
    ap.add_argument("--sample", type=int, default=0, help="judge N random rows")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="/tmp/behavior_classify_verdicts.json")
    ap.add_argument("--from", dest="from_file",
                    help="apply verdicts from a prior dry run instead of judging")
    ap.add_argument("--app-id", help="judge with THIS app's latest manifest for "
                    "every row (default: the manifest of the app holding the "
                    "row's write ACL). Two apps can hold manifests; the one "
                    "that writes is the one whose vocabulary the row was "
                    "extracted under (found 2026-09-01).")
    args = ap.parse_args()

    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=3)
    sql, params = SELECT, []
    if args.user:
        params.append(f"user:{args.user}")
        sql += f"       AND ps.subject_key = ${len(params)}\n"
    # Deterministic order so --seed reproduces one sample across runs
    # and across judges; without it two runs compare different rows.
    sql += "     ORDER BY cp.patch_id\n"
    rows = [dict(r) for r in await pool.fetch(sql, *params)]
    print(f"{len(rows)} active {bc.KEEP_TYPE} rows")
    if not rows:
        # NAMES ITSELF. This script matched zero rows for a day after the
        # `behavior` -> `moment` rename, because the type literal here was
        # missed while src/ and tests/ were updated. "Nothing to judge" and
        # "pointed at a type name that no longer exists" produced the same
        # cheerful output, which is the failure this codebase spent the
        # week correcting elsewhere.
        total = await pool.fetchval("SELECT count(*) FROM context_patches")
        types = await pool.fetch(
            "SELECT patch_type, count(*) AS n FROM context_patches "
            "WHERE COALESCE(status,'active')='active' GROUP BY 1 ORDER BY 2 DESC LIMIT 8")
        print(f"  NOTHING MATCHED. {total} patches exist overall, so this ran "
              f"against real data and no row carries patch_type={bc.KEEP_TYPE!r}.")
        print("  active types present: "
              + ", ".join(f"{r['patch_type']}({r['n']})" for r in types))
        return 0
    if args.sample:
        random.Random(args.seed).shuffle(rows)
        rows = rows[:args.sample]
        print(f"sampled {len(rows)} (seed {args.seed})")

    if args.from_file:
        verdicts = json.load(open(args.from_file))["verdicts"]
        print(f"loaded {len(verdicts)} verdicts from {args.from_file}")
    else:
        from contextquilt.services.llm_client_anthropic import AnthropicLLMClient
        llm = AnthropicLLMClient()
        manifests = await _manifests(pool)
        if not manifests:
            print("no registered manifest declares behavior; nothing to judge")
            await pool.close()
            return 1
        verdicts = {}
        by_app: dict = {}
        for r in rows:
            by_app.setdefault(r["app_id"], []).append(r)
        for app_id, app_rows in by_app.items():
            if args.app_id:
                manifest = manifests.get(args.app_id)
                if not manifest:
                    print(f"--app-id {args.app_id} has no manifest declaring behavior")
                    await pool.close()
                    return 1
            else:
                manifest = manifests.get(app_id) or next(iter(manifests.values()))
            print(f"app {app_id}: {len(app_rows)} rows, judged with manifest of "
                  f"{args.app_id or app_id}: body v{manifest.get('version')}, "
                  f"registry row v{manifest.get('_registry_version')}")
            verdicts.update(await judge_rows(app_rows, manifest, llm))
        json.dump({"verdicts": verdicts,
                   "rows": [{"patch_id": r["patch_id"],
                             "text": (_value(r["value"]).get("text") or "")[:200],
                             "owner": _value(r["value"]).get("owner")} for r in rows]},
                  open(args.out, "w"), indent=1)
        print(f"wrote {args.out}")

    by_id = {r["patch_id"]: r for r in rows}
    hist = Counter((verdicts.get(pid) or "none") for pid in by_id)
    judged = sum(1 for pid in by_id if verdicts.get(pid))
    kept = sum(1 for pid in by_id if verdicts.get(pid) in (None, bc.KEEP_TYPE))
    print(f"\nverdicts: {dict(hist.most_common())}")
    print(f"still behavior: {hist[bc.KEEP_TYPE]}/{judged} judged = "
          f"{100*hist[bc.KEEP_TYPE]/max(judged,1):.1f}%   (kept incl. unjudged: {kept})")
    to_retype = [pid for pid in by_id if verdicts.get(pid) in bc.CONVERTIBLE_TYPES]
    to_archive = [pid for pid in by_id
                  if verdicts.get(pid) not in (None, bc.KEEP_TYPE)
                  and verdicts.get(pid) not in bc.CONVERTIBLE_TYPES]
    print(f"would retype {len(to_retype)}, would archive {len(to_archive)}")
    print("\nsamples by verdict:")
    shown: Counter = Counter()
    for pid, r in by_id.items():
        v = verdicts.get(pid)
        if v in (None, bc.KEEP_TYPE) or shown[v] >= 4:
            continue
        shown[v] += 1
        val = _value(r["value"])
        print(f"  [{v}] owner={val.get('owner')!r}\n      {(val.get('text') or '')[:100]}")

    if not args.apply:
        print("\nDRY RUN. Re-run with --apply --from <out> to write.\n")
        await pool.close()
        return 0

    converted = 0
    for pid in to_retype:
        r = by_id[pid]
        owner = _value(r["value"]).get("owner")
        await pool.execute(
            "UPDATE context_patches SET patch_type = 'preference', updated_at = NOW() "
            " WHERE patch_id = $1 AND COALESCE(status,'active') = 'active'", pid)
        if owner:
            target = await pool.fetchval(
                """SELECT cp.patch_id FROM context_patches cp
                     JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
                    WHERE cp.patch_type = 'person'
                      AND COALESCE(cp.status,'active') = 'active'
                      AND lower(cp.value->>'text') = lower($1)
                      AND ps.subject_key = $2
                    LIMIT 1""", owner, r["subject_key"])
            if target:
                await pool.execute(
                    """INSERT INTO patch_connections
                         (from_patch_id, to_patch_id, connection_role, connection_label)
                       VALUES ($1, $2, 'informs', 'held_by')
                       ON CONFLICT DO NOTHING""", pid, target)
        converted += 1
    print(f"RETYPED {converted}")

    done = 0
    for pid in to_archive:
        await pool.execute(
            """
            UPDATE context_patches
               SET status = 'archived',
                   updated_at = NOW(),
                   value = jsonb_set(
                       jsonb_set(COALESCE(value, '{}'::jsonb),
                                 '{archive_cause}', '"cleanup"'::jsonb, true),
                       '{archive_detail}', to_jsonb($2::text), true)
             WHERE patch_id = $1
               AND COALESCE(status, 'active') = 'active'
            """, pid, f"classified_{verdicts[pid]}")
        done += 1
    print(f"ARCHIVED {done} (archive_cause=cleanup, archive_detail=classified_<type>).")
    await pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
