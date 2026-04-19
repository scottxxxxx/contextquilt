# Taxonomy Validation — Test Artifacts

Supporting evidence for `docs/memos/patch-taxonomy-simplification.md`.

## Purpose

Four controlled tests run against Claude Haiku 4.5 to empirically validate the memory-patch taxonomy. Two tests audit the current 11-type ShoulderSurf schema; two are adversarial tests against the proposed 6-facet generic model.

## Methodology

- **Model:** `claude-haiku-4-5-20251001`
- **Temperature:** 0
- **Runs per test:** 2-3
- **Structure varies by test** — see individual files.

## Tests

| Test | Query class | File |
|---|---|---|
| Test 1 | Post-meeting message draft (SS) | [`test1_post_meeting_draft.md`](test1_post_meeting_draft.md) |
| Test 2 | Pre-1:1 prep (SS) | [`test2_pre_meeting_prep.md`](test2_pre_meeting_prep.md) |
| Test 3 | Grief/mental health — adversarial classification | [`test3_adversarial_grief.md`](test3_adversarial_grief.md) |
| Test 4 | Couples therapy — adversarial multi-party | [`test4_adversarial_couples.md`](test4_adversarial_couples.md) |

## Summary

See [`SUMMARY.md`](SUMMARY.md) for the cross-test synthesis that drives the memo's recommendations.

## How to rerun

All four tests are fully reproducible. Copy the prompts into any Haiku 4.5 client at temp=0 and compare outputs. No code required.

## Key findings

1. 6-facet top-level taxonomy holds across 5 radically different domains (meeting, healthcare, legal, career, grief, couples).
2. Query-scoped recall dramatically outperforms category-dumped recall on memory utilization.
3. Multi-party memory is an ownership-model concern, not a facet concern — deferred to v2.
4. Anti-hallucination: bare variants gracefully decline rather than fabricating — a positioning lever for regulated verticals.
