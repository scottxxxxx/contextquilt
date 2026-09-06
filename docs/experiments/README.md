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
