# The Quilt: Patch Types & Connections

## Patch Types at a Glance

```
 STICKY (never expire)                COMPLETABLE (30d TTL)        DECAYING
 ~~~~~~~~~~~~~~~~~~~~~~               ~~~~~~~~~~~~~~~~~~~~~        ~~~~~~~~
 +------------+  +------------+       +--------------+             +------------+
 |   trait     |  | preference |       |  commitment  |-- done? --> | (archived) |
 | "I'm a     |  | "I prefer  |       |  "Bob will   |             +------------+
 |  visual     |  |  async     |       |   deliver by |
 |  learner"   |  |  comms"    |       |   June 15"   |             +------------+
 +------------+  +------------+       +--------------+             |  takeaway   | 14d
                                                                   | "Team liked |
 +------------+  +------------+       +--------------+             |  the demo"  |
 |  identity   |  |    role    |       |   blocker    |-- done? --> +------------+
 | "VP of Eng  |  | "Lead on   |       | "No staging  |
 |  at Acme"   |  |  Widget    |       |  environment"|             +------------+
 +------------+  |  2.0"       |       +--------------+             | experience  | 30d
                  +------------+                                    +------------+
 +------------+  +------------+
 |   person    |  |  project   |
 | "Bob        |  | "Widget    |
 |  Martinez,  |  |  2.0"      |
 |  frontend"  |  +------------+
 +------------+
 +------------+
 |  decision   |
 | "Go with    |
 |  WebSockets"|
 +------------+
```

## Connection Vocabulary

These are the labeled edges between patches. Each label maps to a **structural role** that drives lifecycle behavior.

```
                              ROLE: parent (cascade on archive)
                    ┌─────────────────────────────────────────┐
                    │                                         ▼
               belongs_to                               +---------+
  decision ─────────────────────────────────────────>   | project |
  commitment ───────────────────────────────────────>   |         |
  blocker ──────────────────────────────────────────>   +---------+
  takeaway ─────────────────────────────────────────>
  role ─────────────────────────────────────────────>


                              ROLE: informs (traversal only, no lifecycle)
                    ┌─────────────────────────────────────────┐
                    │                                         │
               works_on                                       ▼
  person ───────────────────────────────────────────>   +---------+
                                                        | project |
               owns                                     +---------+
  person ───────────────────────────────────────────>   commitment
  person ───────────────────────────────────────────>   blocker
  person ───────────────────────────────────────────>   decision

               motivated_by
  decision ─────────────────────────────────────────>   preference
  decision ─────────────────────────────────────────>   takeaway


                              ROLE: depends_on (can't complete until cleared)

               blocked_by
  commitment ───────────────────────────────────────>   blocker


                              ROLE: resolves (completing this frees target)

               unblocks
  blocker ──────────────────────────────────────────>   commitment


                              ROLE: replaces (archive the old)

               supersedes
  decision ─────────────────────────────────────────>   decision
```

## Example: A Real Quilt Fragment

```
                        +------------------+
                        |  PROJECT         |
                        |  "Widget 2.0"    |
                        +------------------+
                       / | belongs_to  ▲  ▲ \
                      /  |            |  |  \
          works_on   /   |            |  |   \ belongs_to
                    /    |            |  |    \
  +-----------+   /  +----------+ +----------+  +----------+
  |  PERSON   |--'   | DECISION | | COMMIT   |  | BLOCKER  |
  |  "Bob     |      | "Go with | | "Bob     |  | "No      |
  |  Martinez"|      |  WS"     | |  deliver |  |  staging" |
  +-----------+      +----------+ |  June 15"|  +----------+
       |               |     |    +----------+       |
       |  owns         |     |         ▲             |
       '---------->----'     |         | blocked_by  |
       |                     |         '-------------'
       |  owns               | motivated_by         |
       '------------> commit |                       | unblocks
                             |    +-----------+      |
                             '--> | TAKEAWAY  |      |
                                  | "Team     | <----'
                                  |  liked    |
                                  |  the demo"|
                                  +-----------+

                +-----------+          +-----------+
                | PREFERENCE|          | IDENTITY  |
                | "prefers  | <-----   | "VP Eng   |
                |  async"   |  motiv.  |  at Acme" |
                +-----------+          +-----------+

                +-----------+
                |   TRAIT   |
                | "visual   |
                |  learner" |
                +-----------+
```

## Connection Roles Summary

| Role | Lifecycle Effect | Labels |
|------|-----------------|--------|
| **parent** | Archive parent --> cascade to children | `belongs_to` |
| **depends_on** | Can't complete until dependency clears | `blocked_by` |
| **resolves** | Completing this frees the target | `unblocks` |
| **replaces** | Archive the old, keep the new | `supersedes` |
| **informs** | Traversal only, no side effects | `works_on`, `owns`, `motivated_by` |

## Patch Lifecycle

```
  Created          Active            Completed/Decayed      Archived
  (inferred    --> (in quilt,    --> (TTL expired or    --> (hidden from
   or declared)     in recall)       marked done)           quilt & recall)
```

- **Sticky** types (trait, preference, identity, person, project, decision): stay active forever
- **Completable** types (commitment, blocker): can be marked done, 30-day TTL
- **Decaying** types (takeaway 14d, experience 30d): auto-archive after TTL
