# 22. Material the user CONSUMES

**Status: PROPOSAL. Nothing here is built. Scott asked for it on
2026-09-02 after reading a meeting whose every patch was a `behavior`.**

## What he saw

A 12 minute recording of a tech podcast about Apple's leadership
transition. Five patches stored, all `behavior`, all about the two
hosts. His question was whether that is expected and whether it makes
sense. It is expected. It does not make sense.

## What actually happened, measured

The main extraction stored ZERO patches for that meeting. Not a
takeaway, not an event, not a person. The five tiles are the behavior
lane's output after the classifier threw out seven of its twelve
candidates as Apple history rather than conduct. So the wall is not
showing a behavior-heavy meeting. It is showing the only lane that
produced anything.

That is the designed behavior of two calls with different jobs. The
behavior call asks one narrow question, how did named people conduct
themselves, and two named hosts answer it. The main extraction is built
around participation: commitments with a named owner, decisions that
hold until superseded, blockers, projects the user owns. A podcast has
none of that for the listener, so the model declined, which is the
correct answer to the question it was asked.

**The population, from `extraction_metrics`, calls on transcripts of
4,000 characters or more:**

| | calls | zero patches | rate |
| --- | --- | --- | --- |
| no `(you)` marker | 333 | 62 | 18.6% |
| `(you)` marker present | 195 | 17 | 8.7% |

Last 30 days the gap is wider, 22.2% against 8.8%. Of 79 zero-patch
calls, 20 extracted entities anyway: the model read the transcript,
found people in it, and declined to type anything. That is a decline,
not a parse failure.

**On Scott's own corpus, 234 origins:** 22 are 100% behavior, 95 are
mixed, 117 have no behavior at all. By producing lane, 30 origins are
behavior-only.

## The second harm, which is worse and already realised

Consumed material puts people in the People surface. `Leo` has 15
`person_appearances` rows at `speaker` capacity. `Paris Martineau` has
6. Those are podcast hosts. CQ therefore believes the user was in a
room with Leo Laporte fifteen times, and doc 16's whole presence model
rests on the claim that an appearance means exactly that. Doc 16 §5.13
says a served name may assert only what was observed. "Last met" over a
podcast host is a false claim in the surface built to avoid false
claims.

The Apple episode did NOT do this, and only by accident: entities are
written from the main extraction's entities array, which was empty
because the extraction declined. A podcast that yields one commitment
gets its hosts into the roster; one that yields nothing does not. That
is worse than either outcome consistently.

## Why this cannot be inferred

The obvious signal is the missing `(you)` marker, and it is not
sufficient. 73 of 142 substantial meetings arrive unmarked, including
real work meetings where the marker simply failed, and 5 substantial
MARKED meetings still yielded nothing (08-31 audit). Scott's own
medical appointment, 16,952 characters with a marker and real content,
produced zero. Inferring "this is a podcast" from an absent marker
would mislabel work meetings as listening and vice versa, and the
mislabel would be invisible, since both look like a quiet meeting.

**The material kind is known at capture and nowhere else.** The app
knows whether the user pressed record on a meeting they are in or on
something playing. CQ cannot recover it from the transcript, and doc 19
rule 6 applies: check what your instrument resolves through.

## Options

**A. Nothing.** Consumed material keeps producing behavior about
strangers and keeps seeding the roster inconsistently. Rejected in the
writing of this doc: the People harm is a false claim, not a gap.

**B. Declare and suppress.** The app sends `metadata.material_kind`
(`meeting` | `listening`, absent means `meeting`, so today's behavior is
unchanged). On `listening`, the behavior lane does not run and no
person entity or appearance is written. Cheap, honest, loses the
content entirely.

**C. Declare and re-aim (recommended).** Same flag, but `listening`
routes to an extraction whose types are honest for material the user
did not participate in. The candidate set, from what the podcast
actually contained:

- `takeaway`, the evaluative lesson, which is what a listener keeps.
- `event`, an external occurrence with context implications.
- `artifact`, a named thing that exists and can be opened.
- Possibly a new type for the source itself, so a claim can cite where
  it came from.

And the exclusions, which are the point: no `commitment` (nobody owes
the listener anything and nothing belongs in a ledger), no `behavior`
(conduct of a stranger the user will never meet), no person entity or
appearance for a voice on a recording, no `decision` (the listener
decided nothing), no `project`.

**D. Declare, re-aim, and separate the roster.** C, plus voices on
consumed material become a distinct entity kind that the People surface
never shows. More faithful, and more work: it needs an entity type, a
read filter, and a decision about what to do with `Leo` and `Paris
Martineau` who are already in there.

## Recommendation

C, with the flag defaulting to `meeting` so nothing changes for
existing callers, and B's suppressions applied inside it: `listening`
never writes a person entity, an appearance, a commitment or a
behavior. D is the right eventual shape but needs the read-side work
doc 18 deliberately avoided, and should wait until C has shown what
consumed material actually yields.

## What it would touch

CQ: a manifest-level or metadata-level route in the worker, a second
prompt, and a gate on `_extract_behavior_observations` and
`store_entities`. SS: send the flag at capture, which they already do
for `language`. GP: `capture-transcript` is the ONE proxy route that
hand-enumerates its keyword arguments and drops unmodelled top-level
keys, and its `metadata` passes only keys in
`CAPTURE_METADATA_ALLOWLIST` (GP's enumeration, 2026-09-02). So this
flag is a GP code change and deploy, not an additive field. That is the
sequencing constraint: GP first, then CQ, then the app.

**Already in the corpus and not addressed here:** the appearances for
`Leo`, `Paris Martineau` and anyone else who has only ever been a voice
on a recording. Removing them is a data decision for Scott, and the
same question the reassign-speaker cleanup got wrong once already, so
it is deliberately not bundled with a prospective fix.
