# Every CQ deploy costs ~30 seconds of 502 on GP-backed routes

**Date:** 2026-09-14
**Teams:** CQ, GhostPour, Bifrost, the N-400 auditor (Fable)

## Where we landed

**A ContextQuilt deploy takes every CQ-backed route through GhostPour
down for about 30 seconds, and 17 to 24 of those seconds are AFTER
Docker reports the container started.** The docker swap is about 10
seconds of it; the rest is uvicorn starting four workers, each importing
the module graph independently, before anything binds. It is the same
with or without a migration. GP's connect retry was sized for the swap
and covers exactly that; it cannot cover the boot.

## The question, as asked

GhostPour had a connect-retry (their #953) that had never been exercised
because no GP-to-CQ call had ever been in flight during a CQ restart.
The question was theirs: does the retry work when a call crosses a
restart? It turned into a second question once the first was answered:
what is CQ doing between "container started" and "accepting
connections", and is it the migrations?

## What shipped as a result

Nothing in code. This is a measurement, and the fixes it implies are
open decisions (see "What this does not settle"). What it did change
immediately: GP stopped attributing the gap to migrations, and the
practice of sending a deploy notice BEFORE the merge is what made the
measurement possible at all.

## The numbers, in full

Six restarts, from merging PRs #480 through #485 in order, each waited to
completion so none was superseded. `StartedAt` is from `docker inspect`
inside the box; the outage columns are GP's outbound log, corroborated
by the auditor's independent 2-second client probe.

| restart | PR | StartedAt (UTC) | schema | outage | after StartedAt | 502s |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | #480 | 16:40:46.639 | migration 48 | — | — | **contaminated** |
| 2 | #481 | 16:44:14.142 | none | ~32s | ~20s | 8 |
| 3 | #482 | 16:47:43.302 | none | ~29s | ~17s | 8 |
| 4 | #483 | 16:53:29.566 | none | ~34s | ~24s | 9 |
| 5 | #484 | 16:56:36.670 | none | ~30s | ~21s | 8 |
| 6 | #485 | 17:25:03.132 | migration 49 | ~27s | ~18s | 7 |

**Restart 1 is unusable and that is the entry's most reusable lesson.**
GhostPour's own container was redeploying (their #971) from 16:40:35 to
16:41:22, so CQ was down and GP was down in the same span. A 502 at the
edge cannot distinguish them, from either end. Only a `/health` read
against GP, which never touches CQ, separated the two; the auditor did
that and corrected their own report before it travelled.

**The migration deploy was the SHORTEST of the five clean samples.**
Migration 49 rode restart 6 at ~27s. The hypothesis that the gap was
schema work is dead, and the reason is structural: `deploy.yml` runs
migrations in a separate one-shot container that exits BEFORE the app
container is recreated.

Inside the boot, from `docker logs -t`:

| | restart 5 (#484) | restart 6 (#485) |
| --- | --- | --- |
| container StartedAt | 16:56:36.670 | 17:25:03.132 |
| "Uvicorn running" (master, socket bound) | +2.8s | +2.4s |
| first worker process | +17.2s | — |
| "Application startup complete" | +20.3s (all four by +20.3) | +16.8s (first) |

Failure shapes, from GP's log:

- The **first** failure of each restart is a ~190ms 502: a live
  keep-alive connection reset when the old container dies. httpx retries
  connect establishment only, so this one is not retried and the first
  caller after any deploy eats a 502 regardless of retry budget. The
  auditor saw this on 1 of 5 restarts at the client, because it depends
  on a live connection existing at the kill second.
- Every **later** failure is ~1.6 to 2.0s: GP's three connect retries
  (0 / 0.5 / 1s backoff) burning. Constant across all five windows,
  1.69 to 1.97s, no widening, which is the fixed schedule and nothing
  else in the path.
- Exactly **one** call per restart is rescued: the boundary call that
  begins before the port is serving and succeeds mid-call (~2.0s against
  a 30 to 115ms steady state).

Bifrost, unprompted, from the edge: across five weeks "GP up, CQ
unreachable" is the dominant source of residual 5xx on that edge, ahead
of GP's own deploys. The edge retry chain does not intercept, because
`proxy_intercept_errors` is off and a 502 GP returns passes straight
through.

## The offset rule (the part a reader will get wrong)

Downtime does NOT bracket `StartedAt` symmetrically. It starts ~10
seconds BEFORE it, because compose stops the old container first, and
ends ~20 seconds AFTER it, because `StartedAt` is when the container
began and not when the app was listening. A reader who assumes symmetry
mis-attributes both ends.

## How to re-run it

1. Have a loop running INTO the first merge, not started after it. This
   is the whole reason four earlier attempts measured nothing:
   `GET https://cz.shouldersurf.com/v1/people/<user_id>` with a user
   bearer and NO `X-App-ID` header (an absent app id resolves to the
   default app's matrix, where `people` is enabled), every ~2 seconds.
2. Merge, wait for the deploy to complete before merging the next, so no
   run is superseded by the concurrency group.
3. Inside: `docker inspect -f '{{.State.StartedAt}}' contextquilt` and
   `docker logs -t contextquilt | grep -E 'Uvicorn running|Application startup complete'`.
4. Check GP's `/health` during any window before trusting it; a GP
   deploy inside the window makes the sample unusable.

## What would falsify it

- A deploy whose gap is materially shorter than 27s with four workers
  and no other change: the import-time explanation would be wrong.
- Workers reaching "Application startup complete" within a few seconds
  of `StartedAt` while GP still sees 502s for 20 seconds: the gap would
  be somewhere else entirely (proxy, DNS, health-gated routing).
- ⚠ **The open contradiction, unresolved at time of writing.** Uvicorn's
  master binds the listening socket BEFORE forking workers and printed
  "Uvicorn running" at +2.4 to +2.8s. If the socket is bound then, a
  connect after that second should be ACCEPTED and hang, not refused.
  GP read the failures as refused connects for the full 17 to 24s. Both
  stories fit the observable and they cannot both be true. The exception
  class in GP's log settles it: `ConnectError` / `ConnectTimeout` means
  the port really was closed; `ReadTimeout` means it was open the whole
  time, no connect retry of any size would ever have helped, and the
  fix is a read timeout or CQ's worker count rather than GP's backoff.
  Nobody has opened that log yet.

## What this does not settle

The fix. Three candidates, and only the first removes the cause:

1. **Fewer uvicorn workers, or preload-and-fork** so the import happens
   once in the master. Changes how prod runs; Scott's call.
2. A bigger connect retry at GP. Hides it, and does nothing at all if
   the contradiction above resolves read-side.
3. The edge intercepting GP's 502s. Hides it and costs GP's error
   bodies (Bifrost).
