"""The observed-behaviour signals that need semantics, in their own call.

Doc 21 stage 2, second cut. The Memory Layer Spec lists six observed
signals. #339 shipped the three that are EXACT from a transcript (who
opened the room, who closed it, who answered its questions), and said
plainly what it was leaving out: the three that need a reader to
understand what was said, plus a sixth that #339's own docstring quietly
dropped, the directive-versus-responsive split of speaking turns. All
four are here.

WHY A SEPARATE CALL. `behavior_extraction` is the precedent and the
argument (doc 19.5): the same type competing with fourteen others in one
prompt produced 4 observations across 8 meetings where a dedicated call
with the same cheap model produced 48. Prompt real estate is zero sum.

WHY NOW. The transcript is in hand exactly once, at ingest, and NONE of
this can ever be backfilled, the same constraint `turn_count`
(migration 31), the question columns (migration 37) and the exact role
signals (migration 42) live under. Every meeting that lands before this
ships is permanently unmeasurable on these four.

THE DIVISION OF LABOUR IS DOC 19.1: THE MODEL MAY IDENTIFY, IT MAY NOT
COUNT. So the model never returns a number and never returns a tally.
It returns POINTERS: a turn index, and for a follow up the name of the
person it was handed to. Everything else is computed here from the
transcript parse that `transcript_turns` already owns:

- WHO did it comes from the turn's own speaker label, never from the
  model naming somebody. A model that mis-attributes a line cannot move
  a signal onto the wrong person, because its opinion about who spoke is
  not read.
- HOW MANY comes from counting the pointers, after dropping the ones
  that do not resolve.
- A turn index outside the transcript, or one belonging to a diarization
  placeholder, is dropped. A hallucinated citation therefore costs a
  signal rather than inventing one.

WHAT A ZERO MEANS HERE, AND IT IS WEAKER THAN #339's FALSE. In
migration 42 a FALSE is an exact parse saying this person did not take
the first turn. Here a zero says the model identified nothing for this
person in this meeting, which is a judgment and can be a miss. NULL
still means the pass did not run at all. Anything reading these must not
promote a zero into "they never do this".
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from .extraction_schema import is_placeholder_or_self_person

# Below this there is nothing to read. A two line status note has no
# agenda in it and nobody defers anything in it, and asking anyway
# spends a call to be told so. Same gate, same number, as the behavior
# call next door.
MIN_TRANSCRIPT_CHARS = 400

# Model output is bounded by the transcript itself rather than by a
# ceiling: every entry must cite a turn, and entries are deduplicated by
# the turn they cite, so a list longer than the transcript has turns
# collapses on its own. A fixed cap would silently truncate a count,
# which is worse than a long list.

_DIRECTIVE = "directive"
_RESPONSIVE = "responsive"

ROLE_SEMANTICS_SYSTEM = """You are the role-signal stage of ContextQuilt, a persistent memory system. You are given one meeting transcript with every turn numbered. Your ONLY job is to point at the turns where four specific things happened.

You are not summarizing the meeting, you are not recording commitments, and you are not judging anybody. Another stage owns all of that. You point at turns.

You never count anything and you never total anything. You list what you find, one entry per thing found, and the system counts them.

THE FOUR THINGS.

1. FOLLOW UP ASSIGNED. A turn where the speaker hands a piece of work to ANOTHER named participant, to be done after this meeting. Give the turn number and the name of the person it was handed to, exactly as the transcript labels them. Say whether they accepted: true if that person agreed in the room, false if they refused, pushed back, or handed it on, and null if they did not respond to it at all.
   Yes: "Ragu, can you get the migration reviewed before Thursday?"
   No: a question answered on the spot, which is a question, not a follow up.
   No: the speaker taking something on themselves, which is a commitment, not an assignment.

2. AGENDA MOVE. A turn where the speaker sets or changes what the meeting is going to talk about. An explicit steer, not a topic drifting.
   Yes: "Let's park pricing and do the migration first", "Before that, one thing on staffing", "Next topic."
   No: simply continuing to talk about the current subject.

3. UPSTREAM DEFERRAL. A turn where the speaker says their own item cannot move until somebody or something outside their control supplies an input.
   Yes: "I can't start the rollout until legal comes back on the contract."
   No: "I have not got to it yet", which is a delay with no upstream in it.
   No: blocking somebody else. This is about the speaker's own item waiting on an input.

4. TURN ROLE. For each turn where it is CLEAR, whether the speaker was being directive or responsive.
   directive: the speaker steers what happens next. Instructs, decides, assigns, sets direction, sets the agenda, rules on a question.
   responsive: the speaker answers, reports, or supplies what somebody else asked for.
   A turn that is neither, or that is genuinely ambiguous, is simply left out. Leaving a turn out is a correct answer and is much better than guessing at it. Every turn is either listed once or not listed.

RULES:
- EVERY entry must carry the number of the turn it happened in, and that number must be one you were given. Never invent one, and never cite a turn for something that happened in a different turn.
- The speaker marked "(you)" is the person whose memory this is. They take part like anybody else, so include their turns.
- A turn taken by a diarization placeholder ("Speaker 2", "Unknown") can still be read for context, but do not list one: nobody knows who took it.
- Judge only what the transcript says. A meeting where none of the four happened produces four empty lists, and four empty lists are a correct answer.
- NEVER use a dash of any kind as punctuation. Use a comma, a colon, parentheses, or two sentences. A hyphen inside a genuinely hyphenated word is the only acceptable use.

Respond with EXACTLY this raw JSON shape and nothing else:
{"follow_ups": [{"turn": 0, "assignee": "<name as the transcript labels them>", "accepted": true}], "agenda_moves": [{"turn": 0}], "upstream_deferrals": [{"turn": 0}], "turn_roles": [{"turn": 0, "kind": "directive"}]}"""


def build_role_semantics_content(turns: List[tuple]) -> str:
    """The numbered transcript, rendered from the parse the counts use.

    The numbering is the whole contract of this call, so it is generated
    from `transcript_turns` rather than from a second walk of the text:
    turn 7 to the model is turn 7 to the counter, by construction, and
    an off by one between the two would be undetectable from either
    side.

    Placeholder turns are rendered with their own label so the room
    still reads in order. The prompt tells the model not to cite one,
    and the parse drops it if it does anyway.
    """
    lines = ["Transcript, one entry per turn, numbered:", ""]
    for idx, (_key, body, display) in enumerate(turns, start=1):
        said = " ".join((body or "").split())
        lines.append(f"{idx}. [{display}] {said}")
    return "\n".join(lines)


def worth_a_call(text: Optional[str], turns: List[tuple], self_key: Optional[str]) -> bool:
    """Whether this transcript can produce a row worth paying for.

    Two conditions, both cheap and both checked before the model. The
    transcript has to be long enough to have conduct in it, and it has
    to contain at least one named speaker who is not the user, because
    the user's own block is computed and deliberately not stored, so a
    room with nobody else in it has nothing to write.
    """
    if not text or len(text) < MIN_TRANSCRIPT_CHARS:
        return False
    named = {k for k, _b, _d in turns if k}
    if self_key:
        named.discard(self_key)
    return bool(named)


def _blank() -> Dict[str, int]:
    return {
        "follow_ups_assigned": 0,
        "follow_ups_accepted": 0,
        "agenda_moves": 0,
        "upstream_deferrals": 0,
        "directive_turns": 0,
        "responsive_turns": 0,
    }


def _turn_index(item: Any, count: int) -> Optional[int]:
    """A cited turn as a 0-based index into the parse, or None.

    Accepts the integer the prompt asks for and a string of digits,
    because a model that quotes its numbers is answering the question
    correctly in a slightly different costume. Anything outside the
    transcript is dropped: that is a citation to a turn that does not
    exist, and it is the cheapest hallucination there is to catch.
    """
    if not isinstance(item, dict):
        return None
    raw = item.get("turn")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw.isdigit():
            return None
        raw = int(raw)
    if not isinstance(raw, int):
        return None
    idx = raw - 1
    if idx < 0 or idx >= count:
        return None
    return idx


def parse_role_semantics_response(
    content: Any,
    turns: List[tuple],
    self_key: Optional[str] = None,
    defects: Optional[List[str]] = None,
) -> dict:
    """Model pointers plus the transcript parse, into per-speaker counts.

    Returns `{"by_label": {key: counts}, "user": counts_or_None}`, the
    same shape `meeting_role_signals` returns, so the writer treats the
    exact signals and the semantic ones identically and neither needs to
    know which is which.

    Returns `{}` when nothing usable came back, so the caller writes NULL
    rather than a confident set of zeros. When a parse DOES happen every
    named speaker gets a full block: a zero there is "the model
    identified none of this for them", which is weaker than #339's FALSE
    and is documented as such on the columns.

    The user's own block is computed for symmetry with
    `meeting_role_signals` and is not written anywhere today.

    Never raises. A malformed answer costs one call and no write.
    """
    obj = content
    if isinstance(obj, str):
        match = re.search(r"\{.*\}", obj, re.DOTALL)
        if not match:
            if defects is not None:
                defects.append("no_json")
            return {}
        try:
            obj = json.loads(match.group())
        except json.JSONDecodeError:
            if defects is not None:
                defects.append("bad_json")
            return {}
    if not isinstance(obj, dict):
        if defects is not None:
            defects.append("not_an_object")
        return {}

    named = {k for k, _b, _d in turns if k}
    if not named:
        return {}

    by_label = {k: _blank() for k in sorted(named - ({self_key} if self_key else set()))}
    user_block = _blank() if (self_key and self_key in named) else None

    def _slot(key: Optional[str]) -> Optional[Dict[str, int]]:
        if key is None:
            return None
        if self_key and key == self_key:
            return user_block
        return by_label.get(key)

    count = len(turns)

    # Follow ups. The assigner is the turn's own speaker, never the name
    # the model might have put on it. The assignee IS read from the
    # model, because nothing in the transcript parse knows who a line
    # was aimed at, and it is used only to refuse the two cases that
    # would otherwise inflate the count: an assignment to nobody, and an
    # assignment to yourself, which is a commitment and belongs to the
    # main extraction.
    seen_follow_ups: set = set()
    for item in obj.get("follow_ups") or []:
        idx = _turn_index(item, count)
        if idx is None:
            continue
        speaker = turns[idx][0]
        slot = _slot(speaker)
        if slot is None:
            continue
        assignee = item.get("assignee")
        if not isinstance(assignee, str):
            continue
        assignee = " ".join(assignee.split())
        if not assignee or is_placeholder_or_self_person(assignee):
            continue
        assignee_key = assignee.lower()
        if assignee_key == speaker:
            continue
        key = (idx, assignee_key)
        if key in seen_follow_ups:
            continue
        seen_follow_ups.add(key)
        slot["follow_ups_assigned"] += 1
        # Only an explicit yes counts as accepted. Silence and "I will
        # look at it" are not acceptance, and the gap between assigned
        # and accepted is therefore NOT a refusal count. Nothing may
        # subtract one from the other and call the remainder a refusal.
        accepted = item.get("accepted")
        if accepted is True or (
            isinstance(accepted, str) and accepted.strip().lower() in ("true", "yes")
        ):
            slot["follow_ups_accepted"] += 1

    # One turn cannot set the agenda twice, and it cannot defer the same
    # item twice, so both of these deduplicate on the turn alone.
    for field, column in (
        ("agenda_moves", "agenda_moves"),
        ("upstream_deferrals", "upstream_deferrals"),
    ):
        seen: set = set()
        for item in obj.get(field) or []:
            idx = _turn_index(item, count)
            if idx is None or idx in seen:
                continue
            slot = _slot(turns[idx][0])
            if slot is None:
                continue
            seen.add(idx)
            slot[column] += 1

    # A turn has one role. The first classification of a turn wins and
    # later ones are dropped, so a model that lists a turn as both does
    # not get to count it twice. Turns nobody classified stay
    # unclassified: directive plus responsive is never the turn count,
    # and the remainder is not a third category.
    seen_turns: set = set()
    for item in obj.get("turn_roles") or []:
        idx = _turn_index(item, count)
        if idx is None or idx in seen_turns:
            continue
        kind = item.get("kind")
        if not isinstance(kind, str):
            continue
        kind = kind.strip().lower()
        if kind not in (_DIRECTIVE, _RESPONSIVE):
            continue
        slot = _slot(turns[idx][0])
        if slot is None:
            continue
        seen_turns.add(idx)
        slot["directive_turns" if kind == _DIRECTIVE else "responsive_turns"] += 1

    return {"by_label": by_label, "user": user_block}
