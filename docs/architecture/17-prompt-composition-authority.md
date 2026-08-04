# 17: Prompt Composition Authority (v1, ratified 2026-08-03)

Companion to doc 15. Doc 15 governs what ContextQuilt sends and how the
gateway injects it. This doc governs who decides where any of it lands in
the prompt, and who is allowed to change that.

It exists because the same question keeps getting re-litigated one surface
at a time. Written down once, it is a lookup rather than a debate.

Client symbol names are deliberately omitted here; this repo is public.
The behaviors are described so the rule is checkable without publishing
another team's internals.

## 1. The rule

GhostPour owns the prompt composition recipe. ShoulderSurf executes it,
for every model lane: the on-device foundation model, ShoulderSurf AI, a
user's own key, and models that do not exist yet.

**The test:** if moving a section from one turn to the other requires
shipping a ShoulderSurf build, the pattern is broken.

## 2. Three concerns, not one

The recipe covers three separable things, and conflating them has already
produced one wrong design.

- **Inclusion.** Which sections exist at all for a given prompt.
- **Order.** The sequence sections appear in.
- **Placement.** System block or user turn.

Order and placement are GP-owned and served today. **Inclusion is not.**
It currently lives client-side as a per-prompt-mode configuration: seven
named slots, each tri-state, where absent inherits the global default and
an explicit false is meaningfully different from absent. One shipped mode
sets all seven off, meaning a prompt that deliberately receives nothing.

That configuration is scoped per prompt mode, not per surface, so a
per-surface served slot cannot express it. Inclusion is therefore in scope
for the rule in section 1 and simply not yet delivered. It is the known
gap, not an exception. Populating a per-surface slot as a placement
mechanism and calling the job done would leave inclusion with no
server-side owner, which is the specific outcome this doc exists to
prevent.

## 3. Recipe and contents are different layers

The **recipe** is inclusion, order, and placement. The **contents** are the
text inside a given section, for example the global system instructions.
Only contents are ever user-editable, and only on some lanes.

Editability is not one flag. The client carries two locks with different
scopes, and the separation is deliberate:

| Lane | Recipe | Global system instructions | Other prompt families (summary, consolidation, analysis) |
| --- | --- | --- | --- |
| On-device foundation model | GP-served | Managed, not editable | Editable today |
| ShoulderSurf AI | GP-served | Managed, not editable | Locked |
| User's own key (BYOK) | GP-served | GP-supplied, editable | Editable |

The recipe is never user-editable on any lane. A BYOK user may rewrite the
instruction text, because it is their key, their model, and their bill.
They may not decide that the meeting summary moves to the user turn.

**Do not collapse the two locks into a single served flag.** A per-lane
`instructions_editable` maps onto the narrower lock, the one covering the
global instructions section. The broader lock also gates the summary,
consolidation, and analysis prompts, and on the on-device lane those are
user-customizable today. One flag would silently take that away. Either
serve two flags with distinct scopes, or serve the narrow one and state
explicitly that the broad lock stays client-side for now.

## 4. Lane resolution is data

The mapping from API format to lane, and the editability flags, are served
alongside the recipe. Adding a lane must never require a build.

Deriving either one from a string comparison in the client is the
anti-pattern this doc retires. A client-side check that happens to agree
with the served value today is still wrong, because it stops agreeing the
moment a lane is added and nobody notices until a user reports it.

## 5. Offline and cold start

The client bundles the default lane's recipe and its default instruction
text. If the served envelope is unreachable, the bundled recipe is used
unchanged. The client never synthesizes a recipe.

The bundled snapshot is a fallback cache, never authority. Served
configuration wins whenever it is reachable. The default lane must be
present in the bundle so that an unknown model still resolves with no
network.

## 6. Served config evolution

These rules bind anyone serving configuration to the iOS client, which
includes ContextQuilt: registered manifests via `GET /v1/schema`, the
manifest templates, and every quilt payload that reaches the device
through the gateway.

The constraint that makes them non-negotiable: builds frozen in the field
decode payloads strictly and whole-file, and builds older than 2026-08-02
have no decode-failure telemetry at all. An old build cannot tolerate a
shape change and cannot report that it failed.

1. **Additive only.** Never remove, rename, or retype a field in a served
   config. Unknown keys are ignored, so adding is safe for every build
   ever shipped. Removing is safe for none of them.
2. **Every entry in a collection carries every key its siblings carry**,
   even when semantically empty. One entry missing a key the type declares
   non-optional throws, and the throw discards the whole file. CQ already
   practices this on the people surface, where absent counts are served as
   null rather than omitted.
3. **Never narrow a type.** Widening a vocabulary is fine, because
   consumers should tolerate unknown values. Changing an int to a string
   bricks every build at once.
4. **Quiet is not a signal** for anything older than 2026-08-02. Those
   builds cannot report a decode failure, so evidence that a change landed
   safely has to come from somewhere other than the absence of alerts.
5. **Locale coverage matches across siblings.** Every served config covers
   the same locale set its siblings cover, or declares which it does not.
   This is rule 2 at file granularity and it hides the same way: nothing
   throws, and some fraction of users silently receives another locale's
   content. Found live with three different locale sets in one stack: CQ's
   recall labels cover five (en, es, fr, pt, ja), tier copy four, and the
   protected instruction text three with no Japanese file at all. So a
   Japanese user reads CQ's section labels in Japanese next to an
   entirely English instruction block. Nothing threw.

Rule 4 is the same lesson doc 15's defect ledger records twice, arriving
from a third direction.

## 7. What this means for ContextQuilt

**Cache boundary.** The gateway marks the system block cacheable and
splits at the CQ recall boundary when recall is present. So CQ's recall
block does not merely sit inside the cache, it defines where the
breakpoint is. The consequence generalizes past CQ: on a lane carrying no
recall block, the entire system prompt is one prefix, and a single
per-turn byte inside it costs the whole prefix on every send. Anything
that varies per turn belongs after the boundary or in the user turn. This
is why doc 15 item 6 (CQ recall output is byte stable within a UTC day) is
load bearing well beyond CQ's own render cache.

**What item 6 does and does not promise.** It has been read once as a
claim that the recall block is invariant across turns, and it is not.
Recall is ranked against the question: the scorer takes the query text and
boosts patches whose matched entities appear in it, and CQ's own render
cache is keyed on a hash of the request text plus metadata. Item 6 says
that *time* is not a source of variation, because the scorer buckets `now`
to the UTC day so a freshness penalty cannot step by the second. So the
promise is determinism for a repeated identical request within a day, not
invariance across different questions.

The practical consequence: recall is per-turn volatile and belongs after
the cache boundary. Its identity is the whole request shape, which is
user, question text, and metadata (project scope, locale, token budget,
signal flags), quantized to the UTC day. A boundary can never be placed
above recall on the strength of user or project alone, because asking a
different question in the same project changes the block.

**Served instructions are model-facing content.** They are subject to the
same review CQ applies to its own prompt strings. Two interactions to
re-check whenever the served instruction text changes:

- A literal reply string baked into an instruction overrides CQ's language
  anchoring (`output_language`, doc 02). Instructions should state the
  behavior, not a fixed English sentence, or be localized per lane.
- A blanket "do not hedge about limited context" instruction can suppress
  the metamemory gap claims doc 15 item 4 requires. Hedging about a thin
  transcript and stating a known memory gap are different behaviors and
  the instruction text has to distinguish them explicitly. A model reading
  quickly will not.

## 8. Verification

Parity and completeness claims get made against captured bytes, never
against the source that generates them.

Two failures produced that rule. A tier catalog entry was given only the
keys it needed to render, which was true about the product and irrelevant
to the decoder. An envelope section described the shipped template rather
than the wire, which was true about the source and irrelevant to what
actually arrived. Both were correct about what shows up and wrong about
what the payload is, and both failed silently.

The envelope carries a per-surface verification flag (adopting now, byte
diffed against wire, describes intent and unverified). Flipping a flag
without doing the diff is itself the regression, and there is a test that
says so.

**Presence is not consumption.** The same rule pointed the other way, at
data travelling client to server rather than server to client. A field
being accepted by a hop proves only that the hop tolerated it, never that
anything read it.

Traced on the capture path 2026-08-03. The gateway does not allowlist
metadata keys and does not forward the object wholesale; it enumerates
every field by hand, twice, once in the route handler and again inside a
service function with a closed parameter list and no `**kwargs` that
rebuilds its own outbound metadata. A correctly nested new key is accepted
by the request model, whose metadata is typed as an open dict, and then
read by nothing. That is worse than an allowlist, because an allowlist is
at least a single list somebody can audit. Adding one key takes three
separate edits, and missing any one produces the identical symptom:
the field never arrives, forever, with no error anywhere.

So a field is integrated only when every hop that must read it is shown to
read it. The precedent is the `language` key, which had to be threaded
through all three places, and whose incident note records only the request
model behavior and therefore understates what a new key costs.

This is why doc 15 item 8 makes the gateway diff its own leg of the
three-way test rather than folding it into an end-to-end check. Confirming
a key reaches CQ's socket tests the client leg and the last gateway edit
together; only the gateway's own outbound body localises a break to the
hop that dropped it.

## 9. Relationship to doc 15

A composition change that alters bytes in the ProjectChat system block is
a change to the doc 15 context flow, and takes the standing three-way test
(SS device sends, GP diff, CQ socket) on both shapes before it ships, per
doc 15 item 8.

Doc 15 is locked. This doc is ratified but expected to gain lanes.

## 10. Open items as of ratification

- **Inclusion has no server-side owner** and sits on a different axis
  (per prompt mode) than the served per-surface slots. Deciding whether
  that slot covers content selection as well as placement is the open
  design question, and shipping placement alone leaves inclusion
  client-authored.
- **Two lock scopes must not become one served flag.** See section 3.
- **Placement authority: settled 2026-08-03.** The model capabilities
  config carries a placement field of its own, so two served configs could
  each assert placement. The envelope now declares itself sole owner and
  names the other field as deprecated. Note that section 6 rule 1 forbids
  deleting it, so it is deprecated in place, and two tests pin the
  arrangement: the declaration itself, and that the deprecated field stays
  present and uniform, since it vanishing breaks every shipped build and
  it varying means something started reading it again. Residual: "no
  shipped build consumes the value" is still an assumption. The field
  being declared non-optional proves only that the key must be present.
  The per-build manifest below can settle it.
- **The model capabilities config is whole-file strict** with eight
  non-optional fields, so one entry missing any of them discards the file
  on every build including current, and it gates a shipped UI element.
  Hold it to section 6 rule 2 until the client-side fix ships.
- ShoulderSurf has offered a machine-readable manifest of required keys
  per shipped build, derived from their types rather than hand-written.
  CQ should consume it too, for the surfaces listed in section 6.
- GhostPour owes a compact instruction variant for the on-device lane.
  The cloud default costs roughly 8% of that lane's usable budget before
  any transcript arrives, and includes an image rule the lane cannot use.
- GhostPour owes the missing Japanese instruction file. Until it exists,
  those users receive the full English instruction block. See section 6
  rule 5.
- Order of sections within the user turn on the session surfaces is not
  asserted, pending a byte diff. Treat the served order there as
  undiffed, not as ground truth.
