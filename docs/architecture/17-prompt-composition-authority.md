# 17: Prompt Composition Authority (v1, ratified 2026-08-03)

Companion to doc 15. Doc 15 governs what ContextQuilt sends and how the
gateway injects it. This doc governs who decides where any of it lands in
the prompt, and who is allowed to change that.

It exists because the same question keeps getting re-litigated one surface
at a time. Written down once, it is a lookup rather than a debate.

## 1. The rule

GhostPour owns the prompt composition recipe. ShoulderSurf executes it.

The recipe covers which sections exist, what order they appear in, and
whether each section lands in the system block or the user turn. It is
served configuration, not client code, and it applies to every model lane:
the on-device foundation model, ShoulderSurf AI, a user's own key, and
models that do not exist yet.

**The test:** if moving a section from one turn to the other requires
shipping a ShoulderSurf build, the pattern is broken.

## 2. Two layers: recipe and contents

The **recipe** is placement and ordering. The **contents** are the text
inside a given section, for example the global system instructions.

They have different rules, and conflating them is the mistake this section
exists to prevent.

| Lane | Recipe | Instruction contents | User may edit contents |
| --- | --- | --- | --- |
| On-device foundation model | GP-served | GP-served | No |
| ShoulderSurf AI | GP-served | GP-served | No |
| User's own key (BYOK) | GP-served | GP-supplied as the starting point | Yes |

The recipe is never user-editable on any lane. A BYOK user may rewrite the
instruction text, because it is their key, their model, and their bill.
They may not decide that the meeting summary moves to the user turn.

## 3. Lane resolution is data

The mapping from API format to lane, and the editability flag for each
lane, are served alongside the recipe. Adding a lane must never require a
build.

Deriving either one from a string comparison in the client is the
anti-pattern this doc retires. A client-side check that happens to agree
with the served value today is still wrong, because it stops agreeing the
moment a lane is added and nobody notices until a user reports it.

## 4. Offline and cold start

The client bundles the default lane's recipe and its default instruction
text. If the served envelope is unreachable, the bundled recipe is used
unchanged. The client never synthesizes a recipe.

The bundled snapshot is a fallback cache, never authority. Served
configuration wins whenever it is reachable. The default lane must be
present in the bundle so that an unknown model still resolves with no
network.

## 5. What this means for ContextQuilt

Two consequences land on CQ directly.

**Cache boundary.** The gateway marks the system block cacheable and
splits at the CQ recall boundary when recall is present. So CQ's recall
block does not merely sit inside the cache, it defines where the
breakpoint is. The consequence generalizes past CQ: on a lane carrying no
recall block, the entire system prompt is one prefix, and a single
per-turn byte inside it costs the whole prefix on every send. Anything
that varies per turn belongs after the boundary or in the user turn. This
is why doc 15 item 6 (CQ recall output is byte stable within a UTC day) is
load bearing well beyond CQ's own render cache.

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

## 6. Verification

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

## 7. Relationship to doc 15

A composition change that alters bytes in the ProjectChat system block is
a change to the doc 15 context flow, and takes the standing three-way test
(SS device sends, GP diff, CQ socket) on both shapes before it ships, per
doc 15 item 8.

Doc 15 is locked. This doc is ratified but expected to gain lanes.

## 8. Open items as of ratification

- ShoulderSurf owes an inventory of what the client-side override slot
  governs today, before the served per-surface overrides can be
  populated. Until then the client-side path stays in place; removing it
  before the served replacement is populated and verified silently drops
  whatever it does now.
- GhostPour owes a compact instruction variant for the on-device lane.
  The cloud default costs roughly 8% of that lane's usable budget before
  any transcript arrives, and includes an image rule the lane cannot use.
- Order of sections within the user turn on the session surfaces is not
  asserted, pending a byte diff. Treat the served order there as
  undiffed, not as ground truth.
