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
