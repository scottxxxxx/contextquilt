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
