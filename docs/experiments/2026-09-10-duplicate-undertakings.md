# Can a judge find two commitments that are one undertaking?

## The question, as Scott asked it

Doc 23's open question 3, and his answer to it:

> "Offline precision run first, before any code. If it doesn't hold up
> on the 3,510 pairs, I don't want the feature."

The feature under test: serve `possible_duplicate_of` on a commitment
naming other open commitments CQ judges to be the same piece of work, so
a client can offer a merge. The motivating case was two of Scott's own
open action items for one job, which trigram similarity could not see
(0.304, below the 0.35 dedup floor).

## Where we landed

**The judge is stable enough to build on, at 90% self-agreement, and the
design is inverted for the user's own items.** It finds both known
positives, keeps them across reruns, and refuses an adversarial control.
But precision is BIMODAL BY OWNER: right on other people's work, wrong
on the reader's own. And the one merge Scott would accept is
structurally impossible for the design as specified, because it only
ever compares against OTHER meetings and his case is two rows from the
SAME meeting.

Nothing shipped. Doc 23 amended 2026-09-12 with the result.

## What shipped as a result

Nothing in `src/`.

- `scripts/precision_run_duplicate_undertakings.py`, the harness.
- `docs/experiments/2026-09-10-duplicate-undertakings-precision.md`, the
  18-row review sheet, carrying Scott's verbal rulings. **Per-row boxes
  are still unmarked**, so there is no scored precision number.
- `docs/architecture/23-duplicate-undertakings.md`, amended.

## The numbers, in full

49 to 51 judge calls depending on the window, `claude-sonnet-4-6` passed
as a per-call override, production temperature 0.1.

### The run that counts: directional, matching the design

One call per meeting per owner/project with prior open work, showing
that meeting's new commitments against the same owner's earlier open
ones. Run TWICE on identical input.

| | |
|---|---|
| judge calls | 51 |
| call failures | 0 |
| flagged, run 1 | 19 |
| flagged, run 2 | 19 |
| **stable across both** | **18 of 20 distinct pairs (90%)** |
| only run 1 | 1 |
| only run 2 | 1 |

Recall against the known set:

| pair | result |
|---|---|
| `db45b39d` / `84b99a40` Portuguese | FOUND, stable |
| `23721690` / `b133913f` Steven | FOUND, stable |
| `a6f4792c` adversarial control | not flagged (correct) |

The control shares "Steven" and "mother" with both halves of the Steven
pair and is different work by a different person. A judge matching on
vocabulary flags it.

### The run that did not count, kept because it nearly entered the record

The first harness showed the model a whole owner/project group at once,
up to 51 commitments, asking which PAIRS were the same undertaking:
1,275 combinations in one call.

| model | run | flagged |
|---|---|---|
| Haiku 4.5 | 1 | 23 |
| Sonnet | 1 | 18 |
| Sonnet | 2 | 14 |
| Sonnet | 3 | 23 |

Two identical Sonnet runs agreed on **11 of 26** distinct pairs (42%),
and Haiku overlapped Sonnet on essentially nothing. That 42% was a fair
measurement of a task nobody proposes to ship. **A harness that does not
match the design measures something nobody asked about**, and this one
was about to be reported as a property of the feature.

### Sizing, which killed one design before it was written

| | |
|---|---|
| open commitments | 383 |
| owner/project groups | 95 |
| candidate pairs inside them | **3,510** |
| already collapse (>= 0.6) | 0 |
| gray zone the existing judge sees (0.35 to 0.6) | 12 |
| **invisible today (< 0.35)** | **3,498** |
| average open commitments per group | 4.0 |
| p95 | 20 |
| largest (Suresh on ABM) | 51 |

A pairwise design is quadratic and already 3,510 on one account.
Per-new-item against the open set is bounded and fits one call.

### Cost

32 of the last 89 meetings produced commitments, so the proposed sixth
per-meeting call fires about **once a day**, not on every ingest.

## What Scott ruled

Given verbally while reading the 18. Recorded in the sheet.

- **Precision is bimodal by owner.** All 11 non-self pairs correct; the
  7 that are his read wrong.
- **Owner is tolerance, not truth.** On somebody else's item he wants
  the gist, so over-merging is cheap. On his own he has to act on it.
- **The discriminator is same-named-deliverable versus
  different-slices-of-one-effort.** Portuguese names one deliverable
  twice; the Cigna rows name P1 against P2 and P3. Confirmed with the
  rows in front of him after he did not recognise the pair by name.
- **The merge must COMBINE descriptions**, not keep one and discard the
  other.
- **Pair 10 unsure**, left open.

## What this experiment could NOT settle

- **Precision has no number.** The sheet is unmarked per row. "11 of 11
  correct on other people's items" is his verbal summary, not a scored
  tally.
- **Whether a same-meeting candidate set works.** It is the one merge he
  would accept and it was never tested, because the harness inherited
  the design's exclusion of same-meeting pairs.
- **Whether the combining merge is safe.** The normalization experiment
  of the same day says a model rewriting commitment text cannot hold its
  safety property from a prompt. Untested here.
- **Anything beyond one account**, and one that is explicitly test data.
- **The tension in his own rulings**: Cigna items are separate work, and
  the two same-meeting Cigna rows might combine. Both recorded.

## How to re-run it

```bash
# read-only against Postgres, plus LLM calls; writes nothing
sudo docker cp scripts/precision_run_duplicate_undertakings.py contextquilt:/tmp/pr.py
sudo docker exec -w /app -e PYTHONPATH=/app/src contextquilt \
  python /tmp/pr.py --subject-key user:<uuid> --days 30 --model claude-sonnet-4-6
```

It runs the whole window twice and prints self-agreement, the stable
set, the unstable set, and recall against `KNOWN_POSITIVES` and
`KNOWN_NEGATIVE` pinned at the top of the script.

To reproduce the 42% result, revert `build_calls` to group-at-once and
ask for pairs rather than directional matches.

## What would falsify the conclusion

- **Self-agreement drops below roughly 80% on a larger window or another
  account.** 90% on 20 pairs from one account is not a stable property
  yet.
- **A marked sheet shows the stable set is mostly wrong.** Doc 23's own
  falsification case: the affordance then trains people to ignore it and
  the sixth call buys nothing.
- **The same-meeting candidate set turns out to be noisy**, which would
  mean the merge Scott wants cannot be offered safely even once the
  design allows it.
- **A second account shows precision is NOT bimodal by owner**, which
  would make the two-threshold rule a quirk of one person's data rather
  than a property of how people record their own work.
