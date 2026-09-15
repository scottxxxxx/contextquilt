# Experiment log

**Why this exists.** On 2026-09-06 Scott asked whether there was value
in including moments, said we had tested it and could not recall where
we landed, and said we should be documenting these tests. He was right
on all three counts. The answer existed, but it took four lookups
across three systems to reassemble: a private memory file, a code
comment, an `claude.ai` artifact URL, and a scratchpad directory that
gets wiped. None of those is the repo, and the repo is the only one of
them that a future reader will think to open.

A test that changed the code and cannot be found afterwards gets re-run,
or worse, gets argued about from memory. This directory is where the
answer lives.

## What an entry must contain

Not a lab report. Six things, because each one was missing from
something we tried to reuse later:

1. **The question**, in the words it was asked in. Questions drift into
   different questions between the asking and the answering.
2. **Where we landed**, at the top, in a sentence. Anyone rereading this
   is looking for the ruling, not the journey.
3. **What shipped as a result**, by PR number and by the file that
   carries it today. A conclusion with no landing site is an opinion.
4. **The numbers, in full**, including the ones that do not support the
   conclusion. A table with the losing arm deleted cannot be re-read
   later by somebody with a different question.
5. **How to re-run it**, with the scripts and the inputs named. A result
   nobody can reproduce degrades into a claim within about a month.
6. **What would falsify it.** Written before the result stops being
   fresh, because afterwards nobody can remember what would have
   changed their mind.

## The measurement rules these tests are held to

Learned the expensive way, mostly in the same week:

- **A single model call is a hypothesis, not a finding.** Run it enough
  times to see the spread before reporting a number. One classifier
  call was reported to Scott as fact and eleven later calls said the
  opposite.
- **Measure the rerun noise on identical inputs, in the same run.** An
  arm the change cannot touch is the cheapest noise estimate available,
  and without it every gap looks real.
- **Score gaps are the weakest evidence in the file.** Prefer a
  measurement of the artifact itself (what the block actually contained)
  over a model's opinion of the artifact.
- **State what the experiment could NOT settle.** Every entry here has
  a section for it.

## Index

| Date | Entry | Question | Where it landed |
| --- | --- | --- | --- |
| 2026-09-05 | [moment value](2026-09-05-moment-value.md) | Do moments (conduct rows) add value in chat, and do they belong on the Woven quilt? | Keep in recall, reranked and folded into the person's capsule. Dropped from Woven. Shipped #444 to #449, refined #453 to #455. |
| 2026-09-10 | [duplicate undertakings](2026-09-10-duplicate-undertakings.md) | Can a judge find two commitments that are one undertaking, reliably enough to offer a merge? | 90% self-agreement, both known positives stable, control not flagged. Precision bimodal by owner. The one merge Scott would accept is structurally impossible for the design. Nothing shipped. |
| 2026-09-14 | [deploy boot gap](2026-09-14-deploy-boot-gap.md) | Does GP's connect retry cover a CQ restart, and what is CQ doing between container start and accepting connections? | Every CQ deploy is ~30s of 502 on GP-backed routes, 17-24s of it AFTER docker reports the container started: four uvicorn workers importing the graph, not migrations. The retry covers the swap and cannot cover the boot. Nothing shipped; the fix is open. |
| 2026-09-14 | [ingest duplicates](2026-09-14-ingest-duplicates.md) | Do the duplicated origins on the ingest stream carry the recovery marker, and why is latest-wins the right tie-break? | Zero of 387 repeats were ever marked. 102 of 278 groups were never duplicates (analysis + transcript), 61 byte-identical, 149 genuinely different transcripts with an unnamed producer. Latest-wins would have deleted the longer transcript; shipped #486. |
| 2026-09-10 | [commitment normalization](2026-09-10-commitment-normalization.md) | Would incorporating a controlled-language standard (ASD-STE100) into how action items are drafted help long term? | The rules are sound and cannot live in the prompt: refusal rate swung 0% to 65% on identical input with only wording changing. Model proposes, code decides. Nothing shipped. |
