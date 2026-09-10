"""Markers a client puts on an ingest, and what CQ does with them.

Today that is one marker, `X-CZ-Recovery`, which says this delivery is a
REPLAY rather than a first attempt.

WHY IT IS A MODULE AND NOT THREE LINES IN THE ROUTE. The route cannot be
imported without fastapi, which is absent from the local environment, so
logic living there is testable only in CI. The property that matters
here is a negative one (absent means absent), and a negative property
that can only be checked in CI is a negative property nobody checks.

WHAT IT COST TO NOT HAVE THIS. ShoulderSurf writes `pending_ingests.json`
at session start and clears the entry only on a 2xx, so a live meeting's
ingest is a durable debt replayed later with this header set. CQ ignored
the header entirely until 2026-09-10, and GhostPour never forwarded it
(received on `/v1/capture-transcript`, logged, then an outbound request
built with auth headers only) until GP #954. Meanwhile the stream holds
61 origins whose entries are byte-identical repeats, in two bursts of 86
entries in May and 33 in September, and nobody can say whether those are
replays or deliberate re-ingests. The sender was labelling them the
whole time and neither hop carried the label.

ABSENT MEANS ABSENT, and this is the rule the tests lean on hardest. A
key that is always present and sometimes null would be worse than
nothing here: every stream entry would then carry a recovery field, and
the thing being measured is exactly which entries carry one. The
opposite error is worse still. If a hop ever SYNTHESISED this header, CQ
would stamp replays that never happened and stop questioning duplicates
it should question, which is the same failure as GhostPour's
`_freshness.cached` defaulting to True and so never able to fail the
check people used it for. GP tests that direction at their hop; this
module cannot, and does not pretend to.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

RECOVERY_HEADER = "X-CZ-Recovery"

# An unvalidated client string that lands in a payload the worker parses
# and that persists on the stream for months. No legitimate marker needs
# more than this, and a cap is cheaper than trusting the sender.
MAX_MARKER_CHARS = 64


def recovery_marker(header: object) -> Optional[str]:
    """The marker to record, or None when this is an ordinary delivery.

    None for absent, empty, whitespace-only, and for anything that is not
    a string. A header that arrived but says nothing is not evidence of a
    replay, and treating it as one would put a label on a first delivery.
    """
    if not isinstance(header, str):
        return None
    marker = header.strip()
    if not marker:
        return None
    return marker[:MAX_MARKER_CHARS]


def stamp_recovery(payload: Dict[str, Any], header: object) -> Optional[str]:
    """Stamp the payload when this delivery is a replay. Returns the
    marker recorded, or None if nothing was stamped.

    STAMPED ON THE ENTRY RATHER THAN ONLY LOGGED, because a log line
    answers the question for as long as docker keeps the log and the next
    replay may be weeks away: the most recent byte-identical repeat on
    prod is 2026-09-03. The stream is the durable record and it is where
    the duplicates being explained already live, so the label belongs
    beside them rather than in a file that rolls.

    Mutates `payload` in place, and does NOT touch it at all when there
    is no marker.
    """
    marker = recovery_marker(header)
    if marker is None:
        return None
    payload["recovery"] = marker
    return marker
