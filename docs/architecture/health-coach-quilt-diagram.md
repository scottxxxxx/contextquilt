# Health Coach Quilt — Connected Patch Diagram

## The Patches

```
┌─────────────────────────────────────────┐
│  TRAIT                                  │
│  "Morning person, prefers early         │
│   workouts"                             │
│                                         │
│  persistence: sticky                    │
│  project_scoped: no                     │
│  completable: no                        │
└─────────────────────────────────────────┘

┌─────────────────────────────────────────┐
│  PREFERENCE                             │
│  "Doesn't like running,                 │
│   prefers swimming"                     │
│                                         │
│  persistence: sticky                    │
│  project_scoped: no                     │
│  completable: no                        │
└─────────────────────────────────────────┘

┌─────────────────────────────────────────┐
│  CONDITION                              │
│  "Diagnosed with Type 2 diabetes"       │
│  diagnosed: March 2025                  │
│  status: active                         │
│                                         │
│  persistence: sticky                    │
│  project_scoped: no                     │
│  completable: no (conditions persist)   │
└─────────────────────────────────────────┘

┌─────────────────────────────────────────┐
│  GOAL                                   │
│  "Lose 20 lbs by September"             │
│  target: -20 lbs                        │
│  deadline: September 2026               │
│  status: active                         │
│                                         │
│  persistence: completable               │
│  project_scoped: no                     │
│  completable: yes                       │
└─────────────────────────────────────────┘

┌─────────────────────────────────────────┐
│  METRIC                                 │
│  "A1C: 7.2"                             │
│  measured: Feb 2026                     │
│  value: 7.2                             │
│                                         │
│  persistence: sticky                    │
│  project_scoped: no                     │
│  completable: no                        │
└─────────────────────────────────────────┘

┌─────────────────────────────────────────┐
│  INTERVENTION                           │
│  "Started metformin 500mg daily"        │
│  started: March 2025                    │
│  status: active                         │
│                                         │
│  persistence: sticky                    │
│  project_scoped: no                     │
│  completable: yes (can be discontinued) │
└─────────────────────────────────────────┘

┌─────────────────────────────────────────┐
│  OBSERVATION                            │
│  "Reported knee pain after hiking"      │
│  observed: last week                    │
│                                         │
│  persistence: decaying                  │
│  project_scoped: no                     │
│  completable: no                        │
└─────────────────────────────────────────┘
```


## The Connections (Role + Label)

```
                        ┌───────────────────┐
                        │      TRAIT        │
                        │  "Morning person, │
                        │ prefers early     │
                        │   workouts"       │
                        └────────┬──────────┘
                                 │
                          role: INFORMS
                        label: "shapes"
                                 │
                                 ▼
┌──────────────┐         ┌───────────────────┐
│ OBSERVATION  │         │    PREFERENCE     │
│ "Knee pain   │         │  "Doesn't like    │
│  after       │────────►│   running,        │
│  hiking"     │         │   prefers         │
│              │ role:   │   swimming"       │
│              │ INFORMS │                   │
│              │ label:  └────────┬──────────┘
│              │"reinforces"     │
└──────────────┘                 │
                          role: INFORMS
                        label: "constrains"
                                 │
                                 ▼
                        ┌───────────────────┐
                        │      GOAL         │
                        │  "Lose 20 lbs     │
                        │   by September"   │
                        └──┬──────────┬─────┘
                           │          │
                           │          │
                  role: DEPENDS_ON    │ role: DEPENDS_ON
                 label: "tracked_by"  │ label: "requires"
                           │          │
                           ▼          ▼
              ┌─────────────────┐  ┌───────────────────┐
              │     METRIC      │  │   INTERVENTION    │
              │  "A1C: 7.2"    │  │  "Metformin       │
              │   Feb 2026      │  │   500mg daily"    │
              └────────┬────────┘  └────────┬──────────┘
                       │                    │
                       │                    │
                role: INFORMS          role: RESOLVES
              label: "indicates"     label: "treats"
                       │                    │
                       ▼                    ▼
                      ┌──────────────────────┐
                      │     CONDITION        │
                      │  "Type 2 diabetes"   │
                      │   diagnosed Mar 2025 │
                      └──────────────────────┘
```


## Reading the Diagram

### Connections explained:

```
TRAIT:"Morning person"
  ──INFORMS/"shapes"──►  PREFERENCE:"Prefers swimming"
     Being a morning person shapes workout preferences.
     CQ action: traversal only. When recalling preferences,
     the trait provides context for why.

OBSERVATION:"Knee pain"
  ──INFORMS/"reinforces"──►  PREFERENCE:"Prefers swimming"
     The knee pain reinforces why running is avoided.
     CQ action: traversal only. Strengthens the preference
     during recall — this isn't arbitrary, there's a reason.

PREFERENCE:"Prefers swimming"
  ──INFORMS/"constrains"──►  GOAL:"Lose 20 lbs"
     The preference constrains how the goal can be achieved.
     CQ action: traversal only. When the coach suggests a
     workout plan, it knows running is off the table.

GOAL:"Lose 20 lbs"
  ──DEPENDS_ON/"tracked_by"──►  METRIC:"A1C: 7.2"
     The goal's progress is tracked by this metric.
     CQ action: the goal can't be marked complete while
     the metric is outside target range.

GOAL:"Lose 20 lbs"
  ──DEPENDS_ON/"requires"──►  INTERVENTION:"Metformin 500mg"
     The goal depends on this intervention being active.
     CQ action: if the intervention is discontinued,
     flag the goal as at-risk.

METRIC:"A1C: 7.2"
  ──INFORMS/"indicates"──►  CONDITION:"Type 2 diabetes"
     The metric indicates the state of the condition.
     CQ action: traversal only. When recalling the condition,
     the most recent metric provides current context.

INTERVENTION:"Metformin"
  ──RESOLVES/"treats"──►  CONDITION:"Type 2 diabetes"
     The intervention is treating the condition.
     CQ action: if the condition is ever marked "managed",
     check if all resolving interventions contributed.
```


## What Happens Over Time

### New metric reading arrives (April 2026: A1C drops to 6.8):

```
NEW: METRIC "A1C: 6.8, April 2026"
  ──REPLACES/"updates"──►  METRIC "A1C: 7.2, Feb 2026"

CQ lifecycle:
  - role: REPLACES → archive the old metric
  - The DEPENDS_ON connection from GOAL now points to the new metric
  - Old metric preserved in history, not in active quilt
```

### User reports knee pain resolved:

```
OBSERVATION "Knee pain after hiking"
  → status changes to "archived" (decayed or manually dismissed)

CQ lifecycle:
  - The INFORMS/"reinforces" connection becomes inactive
  - PREFERENCE "prefers swimming" still stands on its own
  - But next time coach recalls, the reinforcing evidence is gone
  - Coach might gently re-suggest trying short runs
```

### User hits goal weight:

```
GOAL "Lose 20 lbs" → status: completed

CQ lifecycle:
  - role: DEPENDS_ON connections checked — all dependencies satisfied
  - Goal archived as completed
  - Connected metric and intervention are NOT archived
    (they still relate to the active condition)
  - Only the goal itself moves out of the active quilt
```


## The Five Roles Summary

```
┌────────────┬──────────────────────────────────────────────────┐
│   ROLE     │  WHAT CQ DOES                                   │
├────────────┼──────────────────────────────────────────────────┤
│ PARENT     │  Cascade: archive parent → archive children     │
│            │  Labels: belongs_to, part_of, raised_in         │
├────────────┼──────────────────────────────────────────────────┤
│ DEPENDS_ON │  Block: can't complete until dependencies met   │
│            │  Labels: requires, tracked_by, blocked_by       │
├────────────┼──────────────────────────────────────────────────┤
│ RESOLVES   │  Satisfy: completing this can satisfy target    │
│            │  Labels: treats, addresses, fulfills            │
├────────────┼──────────────────────────────────────────────────┤
│ REPLACES   │  Swap: archive the old, keep the new            │
│            │  Labels: updates, supersedes, corrects          │
├────────────┼──────────────────────────────────────────────────┤
│ INFORMS    │  Traverse only — no lifecycle side effects      │
│            │  Labels: reinforces, shapes, constrains,        │
│            │  indicates, explains, motivated_by              │
└────────────┴──────────────────────────────────────────────────┘
```

The roles are CQ's machinery. The labels are the app's vocabulary.
Five roles handle every domain we've tested:
  - Project management (ShoulderSurf)
  - Health coaching
  - Sales enablement
  - And any future app that registers its own types and labels
