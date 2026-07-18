"""
Metamemory signals for recall output.

Recall silently returns nothing when the store has nothing on a topic.
The downstream LLM cannot distinguish "checked and absent" from "never
checked", and fills that silence with fluent confabulation. These
helpers turn known absences into explicit lines appended to the
context block:

  (no stored memory about: Priya, Falcon Redesign)
  (no stored project memory for "Falcon")
  (memory checked: nothing stored matched this request)

Opt-in per request via metadata.memory_signals (truthy). Signal lines
are deliberately English — like the flat-mode patch markers, they are
LLM-facing, not user-facing.

Everything here must be a pure function of (request text, entity
index, scope): no clock, no randomness, no I/O. The rendered block
has to stay byte-stable within a UTC day for upstream prompt caching.

Precision over recall for the gap claim itself: a false "(no stored
memory about Sarah)" when the index knows "Sarah Abrams" is worse
than silence, so a candidate mention is suppressed whenever ANY of
its words overlaps a word of ANY known entity name or alias. The
capitalization heuristic is Latin-script/English-leaning and will
over-trigger on languages that capitalize common nouns (e.g. German);
the per-line cap bounds that noise, and the flag stays off unless the
caller asks.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set

MAX_UNMATCHED_MENTIONS = 3

# Capitalized-word runs. A "word" swallows interior CamelCase segments
# ("HubSpot" is one word, not a "Hub" fragment), while ALLCAPS shouting
# still doesn't trigger (second char must be lowercase).
_WORD = r"[A-Z][a-z0-9'’\-]+(?:[A-Z][a-z0-9'’\-]+)*"
_MENTION_RUN = re.compile(rf"\b{_WORD}(?:[ \t]+{_WORD})*")

# GP's wire text for follow-up turns is the whole conversation history,
# with the live turn introduced by a fixed marker. Gap claims must be
# about what the user is asking NOW — candidates harvested from prior
# answers are echo artifacts (that is how "Engage" and "Complete" from
# a bulleted answer once starved out the real gap in the question).
_WIRE_MARKERS = ("Current question:", "User question:")

# Words that start sentences/phrases without naming anything. A run's
# leading stopwords are stripped ("The Falcon Redesign" → "Falcon
# Redesign"); a candidate that is nothing but stopwords is dropped.
_STOPWORDS = {
    "a", "an", "the", "this", "that", "these", "those",
    "i", "we", "he", "she", "it", "they", "you", "my", "our", "your",
    "his", "her", "its", "their", "me", "us", "him", "them",
    "if", "when", "what", "who", "whom", "whose", "why", "how", "where", "which",
    "do", "does", "did", "is", "are", "was", "were", "be", "been", "being",
    "can", "could", "will", "would", "should", "shall", "may", "might", "must",
    "let", "let's", "please", "also", "and", "but", "or", "so", "then", "now",
    "not", "no", "yes", "ok", "okay", "hi", "hey", "hello", "thanks", "thank",
    "today", "tomorrow", "yesterday", "here", "there",
    # Imperative/request verbs that lead runs like "Ask Priya" or
    # "Ping Marcus" — strip the verb, keep the name.
    "ask", "tell", "call", "email", "ping", "text", "message", "remind",
    "send", "meet", "contact", "check", "follow", "loop", "get", "give",
    "make", "take", "put", "keep", "move", "set", "add", "remove", "find",
    "schedule", "book", "invite", "review", "update", "share", "show",
    "bring", "talk", "see", "say", "said", "help", "start", "stop", "try",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
}

# Includes markdown list/heading leaders: a single capitalized word
# opening a bullet ("- Engage with…") is ordinary sentence casing, not
# a name signal.
_SENTENCE_BOUNDARY = ".!?\n:;\"'“”‘’()[]-–—•*·#+>"


def memory_signals_enabled(metadata: Optional[Dict[str, Any]]) -> bool:
    """True when the caller opted into metamemory signal lines.

    Lenient about the truthy encoding (True, "true", "1", 1) because
    metadata survives several proxy hops (GP) and has arrived
    stringly-typed before. Never raises."""
    raw = (metadata or {}).get("memory_signals")
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw == 1
    if isinstance(raw, str):
        return raw.strip().lower() in ("true", "1", "yes")
    return False


def _is_sentence_start(text: str, pos: int) -> bool:
    """Whether pos sits at the start of the text or right after a
    sentence/phrase boundary (ignoring whitespace)."""
    i = pos - 1
    while i >= 0 and text[i] in " \t":
        i -= 1
    return i < 0 or text[i] in _SENTENCE_BOUNDARY


def _known_words(known_entities: Set[str]) -> Set[str]:
    words: Set[str] = set()
    for name in known_entities:
        for w in re.split(r"[^\w'’\-]+", name.lower()):
            if w:
                words.add(w)
    return words


def _focus_segment(text: str) -> str:
    """The portion of wire text the gap claim should be about: after
    the last question marker when GP's history framing is present,
    the whole text otherwise."""
    cut = max(text.rfind(m) + len(m) if text.rfind(m) >= 0 else -1 for m in _WIRE_MARKERS)
    return text[cut:] if cut > 0 else text


def extract_unmatched_mentions(
    text: str,
    known_entities: Set[str],
    cap: int = MAX_UNMATCHED_MENTIONS,
) -> List[str]:
    """Name-shaped mentions in `text` with zero overlap against the
    entity index (canonical names ∪ aliases).

    Conservative by design — see module docstring. Single capitalized
    words at a sentence start are ignored (ordinary sentence casing);
    multi-word runs count anywhere. When the text carries GP's
    conversation-history framing, only the current question is
    scanned. Returned in order of first appearance, case-insensitively
    deduplicated, capped."""
    text = _focus_segment(text)
    known_word_set = _known_words(known_entities)

    out: List[str] = []
    seen_lower: Set[str] = set()
    for m in _MENTION_RUN.finditer(text):
        words = [(w.group(), m.start() + w.start()) for w in re.finditer(r"\S+", m.group())]
        while words and words[0][0].lower() in _STOPWORDS:
            words = words[1:]
        if not words:
            continue
        if len(words) == 1:
            word, pos = words[0]
            if len(word) < 3 or _is_sentence_start(text, pos):
                continue
        candidate = " ".join(w for w, _ in words)
        cl = candidate.lower()
        if cl in seen_lower or cl in _STOPWORDS:
            continue
        # Suppress on any word-level overlap with the index: "Sarah"
        # must not be reported missing when "Sarah Abrams" is known.
        if any(w.lower() in known_word_set for w, _ in words):
            continue
        # Same precision rule for truncated fragments: "Hub" must not
        # be reported missing when "HubSpot" is known.
        if any(
            len(wl) >= 3 and any(k.startswith(wl) for k in known_word_set)
            for wl in (w.lower() for w, _ in words)
        ):
            continue
        seen_lower.add(cl)
        out.append(candidate)
        if len(out) >= cap:
            break
    return out


def build_coverage_line(rendered: int, total: int) -> Optional[str]:
    """Contract commitment E (context-flow summit, 2026-07): when a
    project-scoped recall renders fewer patches than the project holds,
    say so — a correctly scoped block that silently omits most of a
    project's memory reads as complete, which is worse than absence.
    Always-on for scoped recalls (not gated by memory_signals): the
    downstream teams treat coverage lines as part of the block."""
    if total <= 0 or rendered >= total:
        return None
    return f"(showing {rendered} of {total} stored patches for this project)"


def build_signal_lines(
    unmatched_mentions: List[str],
    project_scope_label: Optional[str] = None,
    project_scope_missing: bool = False,
    nothing_matched: bool = False,
) -> List[str]:
    """Render the signal lines appended below the context block.

    Ordering is fixed (mentions, project scope, nothing-matched) so the
    output is byte-stable for identical inputs."""
    lines: List[str] = []
    if unmatched_mentions:
        lines.append(f"(no stored memory about: {', '.join(unmatched_mentions)})")
    if project_scope_missing and project_scope_label:
        lines.append(f'(no stored project memory for "{project_scope_label}")')
    if nothing_matched:
        lines.append("(memory checked: nothing stored matched this request)")
    return lines
