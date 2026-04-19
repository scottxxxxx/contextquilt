# ShoulderSurf Memory UI — Three-Tier Grouping (Design Sketch)

**Status:** Design sketch for SS iOS team to design against
**Depends on:** `docs/memos/patch-taxonomy-simplification.md` (6-facet model + 13-type SS schema)
**Target:** Post-launch v2 UX enhancement — not blocking App Store submission

---

## Purpose

The SS memory browser currently shows all patches in a flat list (or grouped by type). As users accumulate hundreds of patches across projects, that becomes hard to navigate. This doc sketches a three-tier grouping that matches how humans mentally organize memory, so users can find what they need without scanning every patch.

This is a **design sketch**, not a spec. SS designers should take this as a starting frame, not a mandate. The goal is to align on the mental model so the final UX lands well.

---

## The three tiers

Every SS patch type maps to exactly one tier.

### Tier 1 — **About You**
Long-term, self-facing. Rarely changes. Shapes *how* the app talks to you and what it recommends.

| SS type | Why it's here |
|---|---|
| `trait` | Behavioral pattern ("tends to over-explain") |
| `preference` | Lean or value ("prefers async standups") |

Volume: low (sparse by design — these are the durable facts of who you are).

### Tier 2 — **Your World**
The entities and targets that define your life right now. Medium-term; changes across seasons of life.

| SS type | Why it's here |
|---|---|
| `person` | Named individuals in your life |
| `org` | Companies, schools, venues, firms |
| `project` | Any unit of ongoing work |
| `role` | Someone's function on a project |
| `goal` | What you're steering toward |
| `constraint` | Rules and boundaries you operate under |

Volume: moderate. Usually browsed by project to see "who's involved, what's the target, what's the boundary."

### Tier 3 — **Activity**
The time-bound things: what happened, what's happening, what's been agreed. Short-to-medium term; high churn.

| SS type | Why it's here |
|---|---|
| `decision` | Calls that were made |
| `commitment` | Promises in flight |
| `blocker` | Current impediments |
| `event` | External occurrences |
| `takeaway` | Evaluative lessons |

Volume: highest. This is where most patches accumulate.

---

## How this maps to the existing 6-facet model

The tiers are user-facing; the facets are backend. The mapping is clean:

| Tier | Facets in this tier |
|---|---|
| About You | Attribute (self-only) + Affinity |
| Your World | Connection + Intention + Constraint |
| Activity | Episode |

The LLM still gets facet-structured recall on the backend. The tier grouping is purely UX surfacing.

---

## Proposed SS navigation structure

```
Your Memory
│
├─ About You
│    ├─ Traits
│    └─ Preferences
│
├─ Your World
│    ├─ People  (list of person patches)
│    ├─ Organizations  (list of org patches)
│    ├─ Projects  (list of project patches; each is tappable → enters project view)
│    ├─ Goals  (list of goals, grouped by project when applicable)
│    └─ Constraints  (list of constraints, grouped by project when applicable)
│
└─ Activity
     ├─ Open Commitments  (active, with deadlines surfaced)
     ├─ Active Blockers  (current impediments)
     ├─ Recent Decisions  (last 30 days)
     ├─ Recent Events  (external occurrences)
     └─ Takeaways  (evaluative lessons, last 30 days)
```

**Alternative: project-first navigation.** Tap a project in Tier 2 → see all that project's patches grouped into the three tiers (About You patches that motivated this project, Your World sub-entities involved, Activity items on this project).

Both layouts make sense. The question for SS design is: do users browse by project first (and see tiers within), or by tier first (and filter by project)? Probably project-first for active work; tier-first for exploration.

---

## Copy guidance

| Tier | User-facing label options |
|---|---|
| About You | "About You" / "About Me" / "Your Style" |
| Your World | "Your World" / "Who & What" / "People & Projects" |
| Activity | "Activity" / "What's Happening" / "Open Items" |

Keep the three-tier naming consistent once chosen. The underlying facet names (Attribute, Affinity, etc.) never appear in the UI.

---

## Interaction patterns worth considering

### Empty states

- **Tier 1 empty:** "As you talk about yourself in meetings, we'll collect what makes you tick — communication style, preferences, values." Low pressure to fill it.
- **Tier 2 empty:** "People, projects, and goals from your meetings will live here." This is the scaffolding tier.
- **Tier 3 empty:** "Decisions, commitments, and things worth remembering will show up here after your first few meetings." Most users will see content here first.

### Filtering

Within each tier, users should be able to filter by:
- Project (especially relevant for Tier 2 and 3)
- Recency
- Status (active / completed / archived — relevant mainly for commitments and blockers)

### The connection picker (already designed)

When editing a patch, the connection picker (see `docs/memos/ss-team-parallel-work.md`) filters available labels by source-type. This doesn't change with the tier grouping — it's orthogonal.

### Search

Search cuts across all three tiers by default. Optional filter: "search only in About You" / "search only in Activity."

### The "pin" affordance

Permanence override (see `docs/memos/patch-taxonomy-simplification.md`) is a per-patch control. Visually a pin/star icon on the patch card. Applies regardless of tier.

---

## What this is NOT

- **Not a replacement for project-centric browsing.** SS users think in projects. The three tiers are a secondary organizing axis; project is still primary.
- **Not a locked schema change.** This is a display grouping, not a data model change. Moving patches between tiers is just a re-grouping, not a migration.
- **Not a launch requirement.** SS can ship with any grouping and add this in a post-launch update. The underlying schema is stable regardless.

---

## Open questions for SS design

1. **Project-first or tier-first default view?** Both make sense; product instinct call.
2. **Should Tier 1 be front-and-center or buried in a settings-like screen?** It's low-volume, high-value context. Could be a "profile card" rather than a browsable list.
3. **How to signal permanence visually?** Pinned patches could look different. Patches near end-of-life (about to decay) could have a soft visual treatment. Not critical for v1.
4. **When a patch spans tiers conceptually** (e.g., a role patch is in Tier 2 but describes a person in Tier 2 too) — how do we avoid feeling redundant when browsing? One approach: don't show role patches in the top-level "People" list; surface them as annotations on the person they describe.

---

## Implementation cost estimate for SS

- **View layer only.** No API changes.
- Tier mapping is a switch on `patch_type` — single lookup table.
- Existing navigation stays; three-tier view is an additional tab or a re-organization of the existing memory view.
- Rough estimate: 1-2 sprint-weeks for a clean implementation once design is settled.
