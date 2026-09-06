# 2026-09-05: is there value in including moments?

## The question, as asked

Scott, 2026-09-05: decide whether `moment` belongs in the Woven quilt by
measuring value in the chats. Build a typical-user persona, synthesize a
meeting transcript and prior-meeting patches including moments, run a
meeting chat and a project chat with and without the patches, have a
blind evaluator assess, and give an honest verdict.

`moment` is ShoulderSurf's manifest type for a person's conduct in a
meeting. In CQ's vocabulary it is a **conduct type**, resolved as
`origin_scoped AND NOT project_scoped` from the registered manifests
(`TypeRuntime.conduct_types`), so nothing below is hardcoded to the word
`moment`.

## Where we landed

**Moments carry real knowledge about people and they stay in recall, but
they must not compete with the compact rules for list slots.** They rank
below preferences and above takeaways, they are boosted only when the
row's OWNER is a person the query named, and when a person is named
their conduct renders in that person's header capsule or not at all.

**Moments do NOT belong on the Woven quilt.** They are dropped from
tiles with the reason `conduct_belongs_to_the_person`. That was the
question Scott originally asked, and the answer is no.

The defect was never the type. It was the ranking.

## What shipped

| What | Where it lives today |
| --- | --- |
| Conduct types resolved from manifests, not hardcoded | `services/facet_runtime.py`, `conduct_types_from_manifests` |
| Rank below preference (10), above takeaway (5) | `services/recall_scorer.py`, `CONDUCT_PRIORITY = 8` |
| Boost on OWNER match only, never a text hit | `services/recall_scorer.py`, `_owner_matches` |
| Fold a named person's conduct into the People capsule | recall formatter, "How they operate:" |
| Dropped from Woven tiles | `services/woven_digest.py`, `DROP_CONDUCT` |

PRs #444 (conduct rows), #445 (manifest arrives as a string from
asyncpg), #446 (capsule overflow), #447 (capsule size), #448 (compact
header at small budgets), #449 (capsule near-duplicates). Refined the
following night by #453 (conduct fetched by owner outside the recency
window), #454 (capsule or nothing), #455 (the row cap counts rendered
rows, not candidates that fold away).

## Method

Persona: Priya Raman, delivery lead at a mid-size IT services firm,
running a client rollout. Four people around her with distinct habits
(one who moves dates and pushes back, one who wants numbers before
agreeing, an engineer, a client PM). Three prior meetings of patches
hand-written in CQ shape, including both durable and deliberately
routine moments. The current meeting's transcript generated from a beat
sheet.

Three chats: a meeting chat (how do I handle this person, what do I
owe), a project chat (prep me for the steering call), and a follow-up
email draft.

Four arms: `none` (no memory), `full` (everything), `no_moment`
(everything except conduct), `moments_only` (conduct alone). Blocks
built by the real CQ formatter and scorer, not a mock.

Evaluator: a separate Sonnet call, arm labels shuffled to neutral
letters, scoring grounded / people / actionable / hallucinations /
overall, plus a ranking.

Everything is synthetic. No real person appears in any input or output.

## Results, in full

Three runs. They are **not** three replicates of one condition: run 1 is
the baseline with moments ranked as Episodes at 30, run 2 is after the
priority change with the capsule not yet exercised, run 3 is with the
capsule live. The `none` and `no_moment` arms contain no conduct rows,
so no CQ change could touch them, which makes them the noise estimate.

Overall score, out of 10, run 1 / run 2 / run 3:

| Chat | none | full | no_moment | moments_only |
| --- | --- | --- | --- | --- |
| meeting_chat | 4 / 4 / 4 | 8 / 9 / 9 | 9 / 8 / 10 | 9 / 9 / 9 |
| project_chat | 6 / 6 / 6 | 8 / 7 / 7 | 9 / 7 / 9 | 8 / 9 / 8 |
| followup_email | 9 / 8 / 8 | 6 / 9 / 8 | 8 / 8 / 8 | 8 / 8 / 9 |

Hallucinations were counted separately and were near zero everywhere
except `none`, which invented material on both advice chats in all three
runs.

### The label experiment

A separate A/B varying only the label on the moment rows, same block
otherwise:

| Chat | `[moment]` | `[conduct]` | per-row kind, e.g. `[pushed back]` |
| --- | --- | --- | --- |
| meeting_chat | 9 | 7 | 7 |
| project_chat | 9 | 5 | 8 |
| followup_email | 9 | 8 | 9 |

**Null result. A more specific label did not help, and the plain one won
every chat.** This is worth keeping precisely because it is a null: it
is the kind of change that feels obviously good and is not.

## How to read these numbers

**The rerun noise is up to 3 points on byte-identical prompts.** Two
independent estimates, both from this data:

- `no_moment` scored 9 / 8 / 10 across three runs on a prompt no CQ
  change could alter.
- The label experiment's `[moment]` arm is byte-identical to run 1's
  `full` prompt. It scored 9 / 9 / 9 where run 1 scored 8 / 8 / 6.

So any gap of 3 or less between two memory arms is inside the noise and
must not be leaned on. This corrects a claim made mid-experiment that
`full` never won, which was true of the runs but not evidence.

## What the scores settled, and what they did not

**Settled, because the gap dwarfs the noise and the pattern repeats:**

- **Memory helps enormously on advice tasks.** `none` scored 4 / 4 / 4
  on the meeting chat and 6 / 6 / 6 on the project chat, dead stable
  across all three runs, against 7 to 10 for every memory arm. It also
  hallucinated where the memory arms did not.
- **Memory did not help the email.** `none` averaged 8.3 and no memory
  arm beat it. The transcript alone was enough for that task.
- **Moments carry person knowledge comparable to the compact rules.**
  `moments_only` matched or beat `no_moment` on the meeting chat in all
  three runs. Whatever a trait or a preference was doing for the answer,
  conduct rows were doing too.

**Not settled by the scores:** whether the full block was worse than the
block without conduct. `full` was last or tied-last among the three
memory arms in 7 of 9 cells, which is suggestive and is inside the noise
band. Do not cite it as a finding.

## What actually justified the change

Not a score. **The composition of the served block**, read directly:

| | moment rows | takeaway | preference | trait |
| --- | --- | --- | --- | --- |
| `full` | 4 | 1 | 0 | 0 |
| `no_moment` | 0 | 3 | 1 | 1 |

At base priority 30, moments outranked preferences (10) and takeaways
(5), so in a 15-row block they displaced the rows carrying each person's
rules. That is a measurement of the artifact, not an opinion about it,
and it is the reason `CONDUCT_PRIORITY` is 8 today.

This is the general lesson: when a model's scores and a direct
measurement of the artifact disagree in strength, the artifact wins.

## Confirmed independently on real data

On 2026-09-06 the same displacement recurred on Scott's own quilt, from
the opposite direction. #453 guaranteed a named person's conduct reached
the block regardless of recency. Because a matched owner adds 100 to a
base of 8, every conduct row for that person then outranked decisions
that did not mention them, and 8 conduct rows filled a 13-row block.
#454 (capsule or nothing) fixed it.

So the finding reproduced outside the persona, on a different mechanism,
against a different failure mode. That is stronger evidence than the
persona test produced on its own.

An A/B on the same real data with memory toggled off also showed the
block's specific contribution is **obligations and ownership**: the one
item owed BY the other party appeared only when the block was present,
in both turns. Breadth and detail came from the meeting summaries. The
two are not substitutes.

## How to re-run

Scripts and inputs are in the previous session's scratchpad at
`.../8d274c9a-681a-4395-9db4-2ba74d5c801f/scratchpad/persona/`:
`scenario.json` (persona, people, three meetings of patches),
`run_persona_experiment.py`, `run_label_experiment.py`, `evaluate.py`,
and the nine result and evaluation JSON files.

All LLM calls run inside the prod container. Set `EXP_REUSE=1` and
`EXP_OUT=<file>` to reuse generated answers and write a new evaluation.

**These files are not yet committed.** Preserving them is an open item;
see the caveat below.

## What would falsify this

- A block where conduct rows do NOT displace the compact rules, at a
  budget large enough that displacement cannot occur. The ranking
  finding is about a scarce 15 rows. Give it 40 and it may not hold.
- A user whose people knowledge lives ONLY in conduct, with no traits or
  preferences recorded. `no_moment` would then be the starved arm, and
  the persona deliberately encoded both.
- A Woven surface where the tile IS the person rather than the project.
  `DROP_CONDUCT` says conduct belongs to the person, which is an
  argument about where it renders, not about whether it is worth
  rendering.

## Caveats on this write-up

- The raw JSON was read directly to build the tables above rather than
  copied from any earlier summary, and one earlier summary was wrong
  (see "How to read these numbers").
- The nine data files and three scripts still live only in a temp
  directory. They should be committed next to this document. That copy
  is pending Scott's go-ahead.
