# 24. The frozen description

**Status: DECISION, and the smallest part of it ships with this doc.
Scott corrected a person's description in the app on 2026-09-12, the
correction worked exactly as designed, and the card did not change.**

## What he did, and what happened

He opened Jillian Cunningham, said the description was wrong, and
corrected the company name from "Mitomi" to "Netomi".

The correction worked. At 07:35:32Z it created patch `97733afd`, an
`org` patch, `origin_mode: declared`, `source_prompt: correction`,
saying "The company name is Netomi". It archived the old "Mitomi" org
patch with `archive_cause: corrected`, `correction_source: user_chat`,
and stamped `corrected_by` at the new row. Every part of the correction
machinery did its job.

The card still reads:

> HR lead for North America at Mitomi, overseeing ~70 people across US
> and Canada

Because that sentence is not a patch. It is `entities.description`, a
column on the entity row, and corrections operate on
`context_patches`. `dismiss_descriptions` already says so in its own
docstring: "chat corrections operate on `context_patches` and never
arrive here." The gap was known and written down; what was not known is
how much of the roster lives on the wrong side of it.

## The number that reframes this

Measured 2026-09-12 on the largest account:

| | |
|---|---|
| person entities | 412 |
| carrying a frozen `entities.description` | 345 |
| with ANY row in `entity_descriptions` | **34** |
| frozen description and NO series row | **312** |
| all entity types carrying a description | 1,117 of 1,279 |

**The description series covers 8% of the roster.** `described_as`,
`changed_from`, `history` and the dismiss affordance built on them are
dark for roughly nine people in ten. Migration 39 built the replacement
and the replacement never took over.

Jillian's person detail serves `described_as: {"current": null,
"iterations": 0, "history": []}` while the top-level `description`
field carries the stale sentence. `degraded` is empty, so nothing failed
to compute. She is the normal case.

**Zero descriptions have ever been dismissed, by anyone, across all 155
series rows.** The affordance has never run on real data.

## Three problems, deliberately kept apart

**1. Identity.** The company entity `61317fa9` is still named "Mitomi"
with no alias to "Netomi". That is wrong independently of any
description text and it compounds: a future extraction saying "Netomi"
mints a second company rather than resolving to the first.

**2. A frozen sentence with no write path.** `entities.description` is
one meeting's sentence, stored once, embedding facts that change (a
company name, a headcount, a role). Nothing can edit it, mark it wrong,
or record that it was ever questioned.

**3. The structural hole.** The 312 people with a frozen description and
no series row cannot be told they are wrong by any route CQ exposes.

## What ships now, and why it is the smallest thing

**DISMISS MUST COVER THE FROZEN DESCRIPTION.** When a user dismisses a
person's descriptions and that person has no series rows, the frozen
sentence is materialised into the series first (a true record of what a
meeting said), then dismissed with the rest. The read path suppresses
the frozen column when every live description for that person has been
dismissed.

Three reasons this is the right first move rather than a new route:

- **It uses a route the app already has.** A new route is a two-sided
  release with GhostPour deploying first (ops rule, learned when
  `uncomplete` was declared live while 404ing from every device).
  `POST /v1/people/{user}/{entity_id}/descriptions/dismiss` exists and
  is already proxied, so Scott can drive this from the app today, which
  is how he wants to know it works.
- **It makes the dismissal honest for the 312.** Today dismissing a
  person with no series rows marks nothing and changes nothing, which
  is a silent no-op on a destructive-sounding affordance.
- **It starts the migration migration 39 intended**, one person at a
  time, at the moment a human says the text is wrong. No backfill of
  345 rows guessing at what anybody thinks.

**THE ROW IS RETAINED, dismissed rather than deleted.** The meeting did
say it. What is wrong is treating it as true of the person. Same
argument that shaped `shelve` and the archive-never-delete rule: a
tombstone would make "the user rejected this" indistinguishable from
"this was never observed".

## What does NOT ship, and why

**A model rewriting the description.** Scott's instinct was that a cheap
model could repair the sentence, and it could. But the normalization
experiment of 2026-09-10 measured that exact class of rewrite: a model
rewriting text that sits next to stored facts could not hold its safety
property from a prompt, with the refusal rate swinging 3, 4, 0, 32 out
of 49 across five runs on identical input, wording only. A rule about
verb conjugation switched refusal off and that run invented a deadline.
If a rewrite ever ships here it is model proposes, CODE decides, and
that is not needed to make his correction visible.

**An org rename route.** `POST /v1/people/{u}/{entity_id}/rename` is
gated on the person entity type throughout and 404s for a company. A
company rename is the correct fix for problem 1 and it is a NEW verb
for the client, so it needs GP first and a decision about what renaming
a company does to every description that names it.

**Mechanical name substitution across descriptions.** Replacing the old
name wherever a description literally contains it is cheap, auditable
and needs no model. It belongs with the org rename, not before it.

**Switching the card to read the series.** That is the migration. This
doc does the repair and leaves the migration open, because the card
reading a series that is empty for 92% of people would show nothing for
most of the roster.

## Open

1. **Does ShoulderSurf render the dismiss affordance at all?** CQ's half
   shipped in #358 and zero dismissals exist account-wide, so it is
   either built and unused or never built. Ask before assuming the app
   path exists.
2. **Company rename**, problem 1, unbuilt.
3. **Whether the card should eventually read the series rather than the
   frozen column**, which is the difference between a repair and a
   migration.
4. **Nothing regenerates a suppressed description.** After dismissal the
   card shows no description until a later meeting writes one. That is
   honest and it may read as a hole.
