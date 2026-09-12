# Standardizing how action items are written

## The question, as Scott asked it

> "something else I wanted to socialize with you is this idea that we
> might want to incorporate this standardization into how we draft our
> action items so that they're easier for humans to understand I want
> you to consider this and help me understand if somehow incorporating
> this standard or these rules into how were creating action items
> would help us long-term  ASD STE – 100"

ASD-STE100 is Simplified Technical English: a controlled language for
aerospace maintenance manuals, roughly 900 approved words each locked to
one meaning, plus about sixty-five writing rules (short sentences, active
voice, one instruction per sentence, no synonyms). It exists so a
non-native technician cannot misread a repair procedure.

The question arrived out of the duplicate-undertakings work (doc 23),
where two records of one job had defeated lexical similarity.

## Where we landed

**The standard's RULES are sound and CANNOT LIVE IN THE PROMPT.** Asking
a model to normalize a commitment produces a refusal rate that swings
from 0% to 65% on identical input with only prompt wording changing, so
the safety property is a mood of the prompt rather than a judgment about
the data. If normalization ships, the model proposes and CODE decides:
the same shape as `enforce_person_ownership`, `enforce_role_holder` and
`sanitize_behavior_observations`, all of which exist because a rule
stated to a model is a preference and a rule enforced in code is a
contract.

Nothing shipped. This experiment gates work rather than following it.

## What shipped as a result

Nothing in `src/`. Two scripts and this entry:

- `scripts/measure_commitment_normalization.py`, the harness.
- `docs/experiments/2026-09-10-commitment-normalization-fidelity.md`,
  the 49-row review sheet, generated but **deliberately left unmarked**
  once the runs showed the text was not safe to score.

It also changed doc 23's open design: the description-combining merge
Scott asked for has exactly this failure mode, so it cannot be a prompt
instruction either.

## The numbers, in full

Same 49 open commitments, same model (`claude-sonnet-4-6`, passed as a
per-call override), production temperature. Only the prompt changed.

### Trigram similarity on pairs whose answer Scott gave

The dedup judge never looks below 0.35, so the question was whether
normalization lifts known duplicates over that floor while leaving known
separate work under it.

| pair | verdict | before | run 2 (split) | run 3 (no split) | run 4 (+verb) |
| --- | --- | --- | --- | --- | --- |
| `db45b39d` / `84b99a40` Portuguese | duplicate | 0.304 | 0.595 | 0.490 | 0.485 |
| `23721690` / `b133913f` Steven | duplicate | 0.338 | 0.352 | 0.347 | 0.352 |
| `9ab83c51` / `3ddab0d8` Cigna | separate | 0.244 | 0.269 | 0.269 | 0.268 |
| `9ab83c51` / `ce9ceed2` Cigna | separate | 0.166 | 0.171 | 0.171 | 0.176 |
| `23721690` / `a6f4792c` control | separate | 0.243 | 0.223 | 0.223 | 0.277 |
| `3ddab0d8` / `ce9ceed2` | Scott unsure | 0.174 | 0.201 | 0.201 | 0.201 |

Duplicates move up, separate work does not. **But the harder duplicate
lands at 0.347 to 0.352 against a 0.35 floor**, which is noise, and the
gap between the highest separate pair (0.277) and the lowest duplicate
(0.347) is 0.070 on five points. Trigram alone still cannot carry this.

### Refusal rate, which is the actual finding

| run | prompt change | normalized | refused |
| --- | --- | --- | --- |
| 2 | purpose slot, splitting on | 46 | 3 |
| 3 | splitting removed | 45 | 4 |
| 4 | verb agreement rule added | 49 | **0** |
| 5 | refusal rule strengthened | 17 | **32** |

**0% to 65% on identical input.** Run 4 is the one that matters: a rule
about verb conjugation, touching nothing about refusal, switched
refusal off entirely. Run 5 overcorrected past the level it was told
("refusing on a third of the input would be a fine outcome" produced two
thirds) and refused `9ab83c51`, "Build a new spreadsheet with Cigna demo
test scenarios", where "build" to "create" is about as safe a mapping as
exists.

### What run 4 produced once refusal was off

Both were correctly refused in run 3:

- `6dd69e85` original "**Measure** Nugget's food intake ... ";
  normalized "Scott **validates** ... **by ongoing for 4-6 weeks.**"
  A changed verb class and a duration forced into a deadline slot.
- `d50af253` original "... **before** mid-week publication decision";
  normalized "... **for** mid-week publication decision, **by mid next
  week.**" A constraint turned into a purpose, plus an invented deadline.

### Two prompt rules that were wrong, and why

**Splitting.** Run 2 turned one record into two when the original joined
two things with "and". Scott caught it reviewing the sheet. STE's "one
instruction per sentence" is a rule for AUTHORING, where the writer owns
the content; these are records of what somebody said, so splitting
asserts a structure the speaker never gave and creates an obligation
nobody stated separately. It also **flattered the measurement**:
Portuguese scored 0.595 with splitting and 0.490 without, and the gain
came from deleting "and Apple archive submission" off one side. Reporting
it as evidence for normalization would have been evidence for
truncation.

**One verb-object slot.** Run 1 (superseded, not tabled above) collapsed
each commitment to a single verb and object. The Steven pair then landed
on opposite halves, one keeping the trip and dropping the purpose, the
other the reverse, and similarity FELL from 0.338 to 0.290. Adding a
purpose slot fixed it. Both normalizations were individually faithful;
the shape was wrong.

## What this experiment could NOT settle

- **Fidelity was never scored.** The 49-row sheet exists and is unmarked.
  Only Scott can say whether a normalized form says the same thing, and
  by the time a run was clean enough to mark, the refusal instability
  had made the exercise pointless.
- **Whether a mechanical checker works.** The proposal (discard a
  rewrite that adds a deadline phrase, drops a scope token, or changes
  verb class) is untested. It is the obvious next step and it is not
  evidence yet.
- **Whether normalization helps READABILITY**, which is what Scott
  actually asked about. The normalized forms read more clearly to me and
  that is an opinion, not a measurement.
- **Anything about non-English.** Extraction writes in the user's
  language from `metadata.language`; STE is English-only by
  construction. No non-English commitment was tested.
- **n=5 on the similarity table.** Five pairs, all from one account.

## How to re-run it

```bash
# on prod, read-only plus LLM calls, writes nothing
sudo docker cp scripts/measure_commitment_normalization.py contextquilt:/tmp/norm.py
sudo docker exec -w /app -e PYTHONPATH=/app/src contextquilt \
  python /tmp/norm.py --subject-key user:<uuid> --sample 50 --model claude-sonnet-4-6
```

The known pairs are pinned in `KNOWN_DUPLICATES`, `KNOWN_SEPARATE` and
`KNOWN_OPEN` at the top of the script, so the sample always contains
them. `left(patch_id::text,8)` is the id form used throughout.

To reproduce the refusal swing, edit only the refusal paragraph in
`NORMALIZE_SYSTEM` and rerun. Nothing else needs to change.

## What would falsify the conclusion

- **A mechanical checker holds the discard rate steady across prompt
  edits.** That is the whole claim: if the rate becomes a property of
  the data rather than the wording, prompt instability stops mattering
  and normalization becomes safe to build on.
- **Refusal rate turns out stable at temperature 0**, or with a model
  that rejects sampling. Every run here was at the production 0.1.
- **A marked fidelity sheet comes back clean at a rate high enough to
  tolerate**, which would mean the instability is in refusals only and
  not in the rewrites themselves.
- **Trigram separation holds on a larger sample.** Five pairs cannot
  establish a threshold; fifty might show the 0.070 gap is real and
  widens.
