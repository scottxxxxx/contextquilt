# 18. App memory isolation: separate subjects, no shared people

**Status: DECIDED 2026-08-07.** Applies to ShoulderSurf and Tech Rehearsal,
and to any third app unless this doc is revisited.

**The decision.** Each app gets its own subject space. No shared person
objects. No `app_id` column, no per-app read filters, and nothing built to
keep convergence cheap. If two apps ever need to share what they know about
the same human, that is a migration at the time, not a capability carried in
advance.

This overrides the reading of the project premise that says one quilt per
human necessarily spans every app. It does not overturn the premise itself:
CQ still solves memory fragmentation *within* an app's world, across
sessions and devices and model providers. It declines to solve it *between*
two products that happen to share an owner.

---

## 1. Why this is not the obvious answer

The obvious answer is the other one. CQ exists to fix cross-platform memory
fragmentation, so two apps sharing a memory layer sharing their memory looks
like the thesis working. The first instinct on this question, recorded here
because the reversal is the useful part, was **one identity space with a
per-app read filter**, on the logic that it keeps future convergence a
config change rather than a migration.

That reasoning optimizes for an option whose value was never measured. When
measured, it is close to nothing.

---

## 2. What the data actually supports

Measured on production, 2026-08-07, on the only real user.

**The user's own record is dense.** 287 decisions, 159 projects, 70
constraints, 50 goals, 46 traits, 40 preferences, 28 takeaways, 20 roles.
Roughly 700 patches describing how this person works, what they decided, and
what they shipped.

**What CQ knows about third parties is a name.** 174 person patches carrying
text and nothing else, **zero** preferences attributed to a person
(`held_by`), **zero** org memberships (`member_of`), **zero** reporting
lines (`reports_to`), and two role patches that join to a person at all.

So a shared person object would hand the second app a name, a meeting count,
and a ledger of work items. For the use cases that motivated sharing
(rehearsing a raise, preparing for an interview) the useful payload would be
how that person decides and what they weigh. Those are exactly the zero-row
fields, and they are not arriving soon: the extractor rarely volunteers
third-party attribution, and a sanitizer removes it when it does (see the
drop-logging work behind `_is_real_person_owner`).

**Conclusion: the valuable overlap between these two apps is the USER, not
the people around them.**

---

## 3. The structural argument, which holds regardless of the data

Even if third-party knowledge were rich, a shared write space between these
two apps would be wrong, for a reason that no filter fixes.

**Rehearsal speech is counterfactual.** "I'll ask for fifteen percent," said
while practising, is not a commitment. It is a sentence the user tried on.
A shared store would let the meeting-capture app render practice as fact,
inside the exact surface whose whole promise is that it remembers what
really happened.

That is a category problem, not a scoping problem. A memory layer that
cannot distinguish rehearsed from real corrupts the thing it exists to
protect. Any future sharing between a capture app and a rehearsal app must
therefore be **one-directional by construction**, and the cheapest
construction that guarantees direction is separate stores.

Note also that the value is asymmetric even before counterfactuals: the
rehearsal app gains from the capture app's record, and the capture app gains
essentially nothing in return.

---

## 4. What we are building instead

Nothing, for now. When the second app wants context, it gets **a one-way
read, not shared storage**: a scoped read of the user's own professional
record, their traits, preferences, goals, constraints, decisions and
projects, requested at the moment it is needed.

That has four properties the shared space does not:

- No shared write space, so counterfactual content cannot flow backwards.
- No cross-app entities, so there is no leak surface to maintain.
- It is explicit, which makes it a product moment ("bring your work context
  into this rehearsal") rather than a surprise. For a memory product, a
  surprise about what it knows is the worst kind.
- It reuses recall, which already exists.

Even the narrow overlap case, rehearsing a conversation with somebody the
capture app knows, is better served this way. What helps is the user's own
record of working with that person, which is a scoped read. It is not a
shared entity.

---

## 5. The mechanism

**The isolation boundary is `subject_key`.** Every patch read in the
codebase already filters on it, so isolation costs zero read changes. Apps
write and read under their own subject; a human using two apps has two
subjects.

**`entity_id` stays the person key inside each space.** That is unchanged
and free, and it is what makes any future convergence a merge rather than a
rewrite.

**Convergence, if ever wanted, is not blocked.** `patch_subjects` is
`PRIMARY KEY (patch_id, subject_key)`, a many-to-many by design, so one
app's patches can later be exposed under another's subject by INSERTing
rows, and withdrawn by deleting them. Person identity across spaces would
need a migration, because `merge` is scoped to one `user_id`. That migration
is the price, and it is only paid if the need ever becomes real.

### What we deliberately did NOT build

No `app_id` on any memory table. No per-app read filters. No cross-space
person link table. Each of those is a permanent maintenance cost with a
silent cross-app leak as its failure mode, bought against a benefit measured
at "a name and a meeting count".

**The trade, stated plainly:** keeping the door open costs a filter on every
person-exposing read, forever. Opening the door later costs one migration we
will probably never run. We are choosing to pay the migration if the need
arrives, rather than the filter every day until it does.

---

## 6. Consequences to hold

1. **Isolation is currently a convention, not a guarantee.** No memory table
   is app-scoped, so separation depends on callers using distinct subjects.
   Today that holds by accident: the second app has only ever written to a
   dedicated smoke subject. Before it integrates for real, the subject
   namespace has to be an explicit contract with the gateway, which mints
   the identity. Optionally CQ records which app first wrote a subject and
   refuses a second app's writes to it, which turns the convention into a
   guarantee cheaply.

2. **A second app's People surface is a separate question, and probably a
   smaller one.** The current People surface is bound to one app's
   vocabulary (see the facet-runtime debt). Before a second app can use it,
   that has to be manifest-derived rather than hardcoded. But it is worth
   asking first whether the second app needs People at all: in interview
   rehearsal the counterpart is usually hypothetical, and in a difficult
   personal conversation the capture app knows nothing about them. That
   makes the second app's roster small, fresh and genuinely its own, which
   is one more sign these were never the same object.

3. **Revisit this doc if third-party knowledge stops being just a name.** If
   `held_by`, `member_of` and `reports_to` ever carry real volume, the
   measurement in section 2 changes and the decision deserves re-running.
   Section 3 does not change: the counterfactual argument is independent of
   how rich the data gets.

## 7. The app registry, as of 2026-09-01

Four rows in `applications`. Two are live tenants, one is test traffic,
one is history.

| app_id | name | what it is |
| --- | --- | --- |
| `886a527b-1d8f-46e1-aadc-d4b05e16256e` | ShoulderSurf | the SS tenant since the 08-07 split; the only meeting ingester; owns every SS patch including the pre-split history (see below) |
| `bc6efb4c-2854-49c0-9e8d-437c99610588` | Tech Rehearsal | structured ingest; dormant since 07-15 |
| `a3be3fee-8788-4483-a278-6f475490d149` | e2e-test | GhostPour's smoke tests, `user:smoke-*` subjects; never a real user |
| `930824d3-2ccb-4869-b3f0-0ed2693f183f` | ghostpour | HISTORY. The identity SS traffic wrote under from 04-01 until the split, with an ingest tail to 08-12. Absent from GhostPour's config; nothing can mint a token for it |

GhostPour holds the same two live ids for shouldersurf and
techrehearsal (checked against their `config/apps.yml` on 2026-09-01,
not against anyone's memory). N-400 Helper is a GhostPour tenant with NO
ContextQuilt app_id on either side; when it asks, CQ mints one through
admin registration and sends it to GP BEFORE the first write, so the
state "exists on one side only" never occurs.

**Ghostpour was retired twice, and the second retirement had to be
done by hand.** It stopped ingesting on 08-12, but 2,671 patches (1,595
active, 1,076 archived, 16 subjects) still carried it as their owning
app in `context_patch_acl`, and the worker's derived writes (profile
pass, consolidation) kept stamping it onto whatever they built from
those rows. Retired as an ingest identity, live as a storage one.
Nothing in CQ reads by app_id (section 5 chose subject spaces over
per-app read filters), so nobody lost a row; the first consumer to add
such a filter would have lost 1,595 silently. On 2026-09-01, at Scott's
decision, ownership of all 2,671 rows moved to the ShoulderSurf id in
one UPDATE; ghostpour now holds zero ACL rows. Its `applications` row
and its 14 `app_schemas` rows stay, because they are the record of what
was registered and when. `extraction_metrics`, `alignment_events` and
`tier_signals` keep the historical app_id, because those are records of
what happened rather than ownership.

**The manifest goes to the app that ingests.** `_resolve_extraction_prompt`
reads the INGESTING app's latest `app_schemas` row. On 2026-09-01 three
manifest versions were registered on ghostpour and answered 200, and
the worker kept extracting SS meetings under the previous wording for
three weeks (doc 19.6 carries the receipt). `scripts/register_ss_schema.py`
now refuses an app whose latest origin-bound ingest is more than 14 days
behind the newest ingest anywhere, unless forced.

**GhostPour's default-app rule, stated here in GP's own words so the
consequence is on record on this side too.** A MISSING, BLANK, literal
`"unknown"` or UNRECOGNISED `X-App-ID` resolves to `shouldersurf`, logs
a warning and never 404s. It fails open deliberately: ShoulderSurf
shipped to TestFlight for months before per-app config existed and
older builds in the field may send no header. The consequence: there is
no "unattributed" bucket. Unattributed traffic lands in ShoulderSurf's
namespace.

**One shared identity, three apps.** All three tenants sit under one
Apple developer team, so Sign in with Apple issues one subject per user
across all of them and app_id is the only tenant boundary that exists.
CQ's subject spaces are per app by construction (section 5), so one
human is three separate memories, which is the intended shape.
