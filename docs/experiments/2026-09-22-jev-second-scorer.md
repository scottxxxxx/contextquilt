# Jev as a second scorer: where a typed judgment beats the judge we pay for

**Where we landed.** On the three judgments measured, Jev wins outright on
the character guardrail (100% against the shipped denylist's 56%, and it is
the only option that reads Spanish or Portuguese at all), ties every arm on
dedup at a twentieth of Sonnet's cost, and is NOT good enough to replace the
behavior classifier, because it typed the manifest's own example of a
non choice as a moment. Haiku 4.5 is the loser of the whole table: worse than
Jev on type assignment, unstable across reps, and five times the price.
Nothing shipped yet.

## The question, as asked

Scott, 2026-09-22: "what can we do to make our system better? Can we use Jev
to help us in any way? Let's think of some tests using synthetic data to see
what Jev can do for our classification and evaluation tasks that it is
designed to work on... Then do a few simple tests to evaluate the accuracy,
speed and cost difference between the way we use GPT Models today, versus
using Jev."

## What Jev is, for the reader who has not met it

TypeSafe's System One model. It returns a typed answer and nothing else: a
`noul` (probability a condition holds), a `choice` with a confidence, or a
`score`. No prose, so it cannot extract a value, normalize a date or write a
summary. Billed on input only, $0.042 per million tokens, because there is no
decoding to meter. Its published jaggedness list is worth reading before
designing anything with it: unreliable at arithmetic and tallying, reads
dates as text rather than ordered quantities, degrades on multi hop
indirection, and treats large irrelevant state as a distractor. Doc 19.1
already forbids the model to count; this is the same rule arriving from the
vendor.

## The arms

The baseline arm imports CQ's OWN prompt builders (`build_classifier_system`,
`DEDUP_JUDGE_SYSTEM`, `sanitize_behavior_observations`) and CQ's own parsers
(`parse_classifier_verdicts`, `parse_dedup_verdicts`), so the prompt under
test is the shipped prompt rather than a paraphrase of it. It posts to
api.anthropic.com over raw HTTP, which is how `llm_client_anthropic` calls
it. **It is a reproduction of the prompt, the model and the parser, not a
call through the worker.** No database, no sanitizer chain, no batching cap,
no manifest variance. Every number here is proved on that reproduction.

Fixtures are synthetic and authored for this test, balanced by construction,
with ground truth written from the manifest's own definitions.

## The numbers, in full

### Task 1. Conduct versus character, guardrail 12b

16 items, 8 conduct and 8 character, 8 of the 16 in Spanish or Portuguese.
The shipped guardrail is not a model: it is `character_word_in` over 29
English adjectives (`follow_through.CHARACTER_WORDS` plus
`CHARACTER_TRAIT_WORDS`), matched word bounded.

| arm | accuracy, 3 reps | wall | cost |
| --- | --- | --- | --- |
| shipped denylist | 56% 56% 56% | 0.00s | $0 |
| Jev | 100% 100% 100% | 4.95s (0.31s per item) | $0.000267 |

The denylist caught 1 of 8 character statements: the one containing
"defensive", which is on the list. It missed "detail oriented", "dismissive",
"impatient", and all four non English items.

**The caveat that decides what this is worth:** I wrote the fixtures. Had I
drawn the adjectives from the list, the denylist would score 100%. What is
measured is generalization beyond a closed list, which is the live question
for a system that ingests Portuguese, not a claim about its production
accuracy.

### Task 2. Semantic dedup, the gray zone judge

16 pairs, 8 the same fact and 8 not, including near misses built from the
judge prompt's own examples ("go live on the 16th" against "the 18th") and
one cross language pair.

| arm | accuracy, 3 reps | wall | cost |
| --- | --- | --- | --- |
| Sonnet 4.6 | 100% 100% 100% | 2.79s | $0.005943 |
| Haiku 4.5 | 100% 100% 100% | 1.77s | $0.002200 |
| Jev | 100% 94% 100% | 4.96s | $0.000273 |

Every arm can do this. Jev flipped one pair on one rep, which is the only
instability it showed anywhere. Sonnet costs 21.8x Jev here, Haiku 8.1x.

### Task 3. Type assignment, the behavior classifier's job

20 items across moment, commitment, event, takeaway, preference and decision.
Scored type only: `parse_classifier_verdicts` rewrites a kept moment to the
string "routine" when the stakes gate fires, which is a drop decision and not
a type, and Jev was never asked the stakes question.

| arm | accuracy, 3 reps | wall | cost |
| --- | --- | --- | --- |
| Sonnet 4.6 | 85% 90% 85% | 6.24s | $0.012194 |
| Haiku 4.5 | 65% 65% 80% | 2.45s | $0.004549 |
| Jev | 75% 75% 75% | 6.73s | $0.000943 |

Sonnet costs 12.9x Jev on this task, Haiku 4.8x.

**Haiku is the finding nobody asked for.** It is worse than Jev on the same
items while costing about five times more, and it swung 15 points across
three reps on identical input. The 2026-08-13 person analyzer experiment
found Haiku failing invisibly on a neighbouring judgment. This is the same
shape, and the instability is what makes it dangerous: a single run would
have reported 80% and looked fine.

**Jev's disqualifying miss.** "Looked up the account balance when asked" came
back `moment`, stably, on every rep. That sentence is the manifest's own
explicit example of what is not a choice, and catching exactly that drift is
why the classifier exists. Sonnet got it right every time.

**A label of mine was wrong.** Both Sonnet and Jev independently typed "Teams
data collection is capped by the Purview API" as `constraint` against my
`takeaway`. Two independent scorers agreeing against the author is the author
being wrong: the manifest has a `constraint` type and that sentence is one.
Correcting it moves every arm up one: Sonnet 90/95/90, Jev 80/80/80, Haiku
70/70/85. The uncorrected numbers are the ones in the tables above.

**The decision versus commitment line is a prompt problem, not a model
problem.** Both Sonnet and Jev miss "Vaultline will present the deployment pitch
to the advisory team" the same way.

## Two harness bugs caught before they became findings

Recorded because each produced a clean, publishable looking, wrong number.

1. The first dedup run scored the baseline at exactly 50%, with all eight
   "same fact" pairs wrong and all eight "different" pairs right. That is the
   signature of a parse failure, not a judgment. The model's raw answer was
   perfect; `parse_dedup_verdicts` takes the ALREADY PARSED dict because the
   production client json loads first, and handing it a string made it fail
   open to all False. Failing open is correct behavior (a judge failure must
   never lose a memory) and it is indistinguishable from a confident wrong
   answer on a scoreboard.
2. The first type run counted "routine" as a type miss, which penalised the
   baseline for answering a stakes question the other arm was never shown.

Both were found by printing the raw response instead of the score. This is
the rescorer wiring trap the Jev skill warns about, from the other direction.

## What this could NOT settle

- **Nothing was measured on real rows.** Every prod read in the session was
  refused by the auto mode classifier, so the fixtures are synthetic and the
  distribution is mine, not production's.
- **Not the worker code path.** No database, no sanitizer chain, no batching
  cap, no real manifest variance, no re ingest.
- **n is 16, 16 and 20.** A five point difference is one item. The rep to rep
  spread is the only noise estimate here and it is small, but the sample is
  too.
- **The latency column is unfair to Jev by construction.** The model arms are
  one batched call over all items; the Jev arm is N sequential calls at about
  0.33s each. Any real integration would fire them concurrently. Read the per
  item figure, not the wall clock.
- **Nothing about long or adversarial state**, which is where the jaggedness
  page says Jev degrades.

## Recommendations, none of them shipped

1. **Character guardrail: the strongest candidate.** It is the only place
   where the incumbent is a regex rather than a model, and the multilingual
   gap is structural rather than a tuning miss.
2. **Dedup judge: the clean cost swap**, but watch the flip. A wrong merge
   silently loses a memory, which is why the prompt says to answer FALSE when
   unsure, and a probability threshold is a weaker instrument for that
   asymmetry than the prompt is.
3. **Type assignment: a cascade, not a replacement.** Jev types every item,
   and only the rows where it disagrees with the judge go to Sonnet. That is
   the extraction cascade pattern from TypeSafe's own docs, and it keeps
   Sonnet on exactly the items that produced Jev's disqualifying miss.
4. **Never: dates, counting, or prose.** Deadline resolution, question
   attribution and speaker turns are all out by the vendor's own limits, and
   `who_they_are` needs prose Jev cannot write.
5. **Not on the read path.** 0.33s against a 10ms cache hit target is thirty
   times over budget, and the zero latency architecture is the patent claim.

## How to re-run it

```
BENCH_KEY_FILE=/path/to/anthropic.key \
  .venv/bin/python tests/benchmark/jev_vs_today.py all 3
BENCH_MODEL=claude-haiku-4-5 BENCH_KEY_FILE=... \
  .venv/bin/python tests/benchmark/jev_vs_today.py types 3
```

Needs `TYPESAFE_API_KEY` in `.env` for the Jev arm. Fixtures and ground truth
are in `tests/benchmark/jev_vs_today_data.py`. Total spend for every number in
this file was under ten cents.

Note that a working Anthropic key is NOT in the repo's `.env`: the local file
carries template placeholders (18 and 14 characters on 2026-09-22) and prod
reads from Secret Manager, so the baseline arm needs a key supplied.

## What would falsify it

- **The character win.** Run the same question over real non English behavior
  rows from prod. If the denylist's miss rate there is nothing like 7 of 8,
  the gap is an artifact of fixtures I chose and the recommendation is weaker
  than it looks.
- **The cascade.** If Jev's disagreement rate with the shipped classifier on
  real rows is high, the cascade sends most items to Sonnet anyway and saves
  nothing. The cost case needs the disagreement rate, and this experiment did
  not measure it.
- **The dedup swap.** If Jev's flip rate on real gray zone pairs is worse
  than the judge's, the saving is not worth it at any price, because the
  failure it buys is a silently lost memory.
- **Haiku.** If Haiku's instability does not reproduce on a larger sample, it
  is noise on 20 items rather than a property, and the 08-13 result stands
  alone rather than being confirmed.
