# 14: Consolidation — The "Sleep" Pass

## Why

Human memories are reprocessed offline: repeated episodic experiences
generalize into durable knowledge. CQ's stored patches were inert — six
takeaways about the same topic never became the trait a human would have
formed by morning. Consolidation is the active-enrichment loop that
closes this gap: it finds clusters of related episode-grade patches and
synthesizes one higher-order patch per cluster, with full provenance.

Runs on the cold path (worker loop, default every 24h) — the zero-latency
read path is untouched.

## How

1. **Rules come from manifests, nowhere else.** A manifest opts in with:

   ```json
   "consolidation_rules": [
     {"from_types": ["takeaway"], "produce_type": "trait",
      "min_patches": 3, "guidance": "Only stable habits, never one-off moods."}
   ]
   ```

   `from_types`/`produce_type` must be declared patch types (validator-
   enforced). No rules → no consolidation for that app's patches, so the
   code ships inert until an app opts in — same rollout shape as cues.
   Env kill switch on top: `CQ_CONSOLIDATION_ENABLED`.

2. **Clusters form on shared cues.** The associative-retrieval cue index
   (doc 04) doubles as the clustering key: a cluster is (user, app,
   type ∈ from_types, shared cue) with ≥ min_patches active members
   inside a 180-day window. Deterministic, one GROUP BY, language-
   agnostic. Patches with no cues never cluster — extraction quality
   feeds consolidation quality.

3. **Synthesis is one LLM call per cluster**, capped at 3 clusters/user
   and 20 users/app per cycle. The prompt shows the source texts and
   asks for ONE durable statement — or `skip: true` when the
   observations don't genuinely converge. Refusal, parse failure, or a
   degenerate statement skips the cluster; consolidation never forces
   an insight and never touches sources.

4. **Provenance is mandatory.** The derived patch carries:
   - `origin_mode = 'derived'`, `source_prompt = 'consolidation'`
   - `source_patch_ids` (lineage array)
   - `value.source_cue` — also the **idempotency stamp**: one
     consolidation per (user, app, produce_type, cue), enforced in the
     cluster query itself
   - an `informs` connection (`consolidated_into`) from every source

   A bad generalization is traceable and deletable; deleting it (or its
   sources — connections cascade) is always safe.

## The corpus the profile pass reads

The person-clustered rule (`cluster: "person"`) is what produces the 16a
insight cards, and two of its three lenses ask a model to infer HOW a
person behaves. They kept declining, and the cause was measured against
production rather than guessed at. It was not sampling: the live model
declined identically on the best evidenced declined person at ten sources
and at his full 39 source window, with the same stated reason both times,
that the observations are all task assignments and status updates with no
pattern of decision making across meetings. It was not volume either: one
declined person owns more patches than a person carrying two cards.

It was the corpus. CQ stored what happened and never stored how anyone
behaved, so every pass re-inferred behavior from task text and mostly
failed. The fix is capture, not prompting: the `behavior` type records one
observed instance of how a named participant conducted themselves, owned
by that person, so the evidence accumulates per person between passes.

Three properties are load bearing and each one is a place this quietly
breaks:

- **It reaches a person only through the ownership edge.** The cluster
  query walks `owns` from the person patch, so the type has to be in
  `PERSON_OWNED_ACTION_TYPES` and in the manifest's `owns` to_types. A
  manifest-only type is extracted, stored, and never seen by any lens.
- **It must not dedup, and it must carry its origin.** See
  `collapse_duplicates` and `origin_scoped` in the manifest reference. A
  collapsed observation destroys a receipt, and an origin-null one is
  invisible to a query that counts distinct meetings.
- **Episode facet, deliberately.** A non project scoped type joins
  `universal_recall_types` only when its facet is a freshness facet, so
  Episode keeps observations about third parties out of every recall
  block that has no project context.

Guardrail 12b (cite observable behavior, never character) now applies at
capture as well as at the claim, via `sanitize_behavior_observations`. The
denylist behind it is English only while extraction writes in the language
of the meeting, so for other languages the manifest guidance is the whole
guarantee.

## Deliberate v1 limits

- No refresh: a consolidated cue is never re-synthesized when its
  cluster grows. Revisit with a staleness rule once real derived
  patches exist to study.
- Sources are not archived or down-weighted after consolidation — the
  derived patch competes in recall on its own (produce_type priority +
  freshness). Source lifecycle coupling is a follow-up.
- Confidence is fixed at 0.7; no per-cluster confidence model yet.

Implementation: `services/consolidation.py` (pure logic) +
`worker.consolidation_loop` (I/O). Patent note: this loop is an
embodiment of the provisional's active-enrichment claims.
