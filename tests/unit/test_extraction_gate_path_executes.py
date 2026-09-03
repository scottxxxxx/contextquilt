"""The extraction gate's early return, EXECUTED rather than read.

Doc 19.12: a check that never watches the outcome passes by construction.
`test_headline_lane.py` and `test_extraction_gate.py` both pin this lane by
READING worker.py as text, and both stayed green for three days while every
transcript under the gate floor died in production with

    UnboundLocalError: cannot access local variable 'origin_id'
    where it is not associated with a value

because `timestamp`, `project`, `project_id`, `origin_id` and `origin_type`
were bound AFTER the LLM call, below the early return that reads all five.
No amount of source reading finds that: the names are present in the text,
spelled correctly, on a branch nothing ever took.

So this file is the executing sibling. worker.py cannot be imported here
(asyncpg, httpx, redis and structlog are absent from the local venv, which is
the constraint every other worker test works under), so instead the REAL
source of `handle_meeting_summary` is compiled and called. What is stubbed is
only what the method reaches OUT to. The binding order under test is the
shipped text, compiled by the real compiler, and the branch is really taken.
"""

from __future__ import annotations

import ast
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKER_SRC = (ROOT / "src" / "worker.py").read_text()

GATE_FLOOR = 1200


# ----------------------------------------------------------------------
# Compiling the real method
# ----------------------------------------------------------------------

def _method_source(name: str) -> str:
    """The shipped source of one method, dedented, nothing rewritten."""
    tree = ast.parse(WORKER_SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            segment = ast.get_source_segment(WORKER_SRC, node)
            assert segment, f"could not recover source for {name}"
            return textwrap.dedent(segment)
    raise AssertionError(f"{name} not found in worker.py")


class _Recorder:
    """Captures what the lanes on the gate path were handed."""

    def __init__(self) -> None:
        self.behavior_args: tuple | None = None
        self.headline_args: tuple | None = None
        self.logged: list[tuple[str, dict]] = []
        self.llm_called = False
        self.alerted: list[BaseException] = []

    def log(self, event: str, **kw) -> None:
        self.logged.append((event, kw))

    def events(self) -> list[str]:
        return [e for e, _ in self.logged]

    def failure(self) -> str | None:
        """The production symptom: what `meeting_summary_failed` recorded.

        `handle_meeting_summary` wraps its whole body in `except Exception`,
        so the UnboundLocalError never escapes the call. It is logged and
        swallowed, which is precisely why this cost three days: from the
        caller's side a crashed ingest and a healthy one are the same
        awaited None.
        """
        for event, kw in self.logged:
            if event == "meeting_summary_failed":
                return str(kw.get("error"))
        return None


def _build(gate_reason: str | None, listening: bool, listening_types: set[str]):
    """Compile `handle_meeting_summary` against stubbed surroundings.

    Everything here is an INPUT to the method or a collaborator it calls.
    None of it re-implements the binding order the test is about.
    """
    rec = _Recorder()

    async def _behavior(*args):
        rec.behavior_args = args

    async def _headlines(*args):
        rec.headline_args = args

    async def _open_commits(user_id, project_id):
        return ""

    async def _extract(**kwargs):
        # Resolved BEFORE the gate, so the object must exist; calling it
        # means the gate did not return and the meeting was paid for.
        rec.llm_called = True
        raise AssertionError("the gate path must not call the LLM")

    async def _llm_for_app(app_id):
        return SimpleNamespace(extract=_extract)

    async def _maybe_alert_llm_failure(exc):
        rec.alerted.append(exc)

    async def _resolve_prompt(app_id):
        return ("SYSTEM", {"schema": True}, {"manifest": True})

    async def _app_manifest(app_id):
        return {"manifest": True}

    async def _apply_speaker_identities(user_id, summary, metadata, person_type):
        return summary, False

    selfish = SimpleNamespace(
        _extract_behavior_observations=_behavior,
        _generate_headlines=_headlines,
        _build_open_commitments_block=_open_commits,
        _get_llm_for_app=_llm_for_app,
        _maybe_alert_llm_failure=_maybe_alert_llm_failure,
        _resolve_extraction_prompt=_resolve_prompt,
        _app_manifest=_app_manifest,
        _apply_speaker_identities=_apply_speaker_identities,
    )

    namespace: dict = {
        "datetime": datetime,
        "timedelta": timedelta,
        "timezone": timezone,
        "logger": SimpleNamespace(
            info=lambda event, **kw: rec.log(event, **kw),
            warning=lambda event, **kw: rec.log(event, **kw),
            error=lambda event, **kw: rec.log(event, **kw),
            debug=lambda event, **kw: rec.log(event, **kw),
        ),
        "validate_user_attribution_hint": lambda hint: None,
        "normalize_owner_in_transcript": lambda summary, label: summary,
        "people_vocabulary": lambda manifest: SimpleNamespace(
            person_entity_type="person"
        ),
        "material_kind": SimpleNamespace(
            is_listening=lambda metadata: listening,
            allowed_types=lambda manifest: listening_types,
            build_listening_system=lambda types: "LISTENING SYSTEM",
        ),
        "extraction_gate": SimpleNamespace(
            why_not_worth_extracting=lambda text: gate_reason,
            min_transcript_chars=lambda: GATE_FLOOR,
        ),
    }
    exec(compile(_method_source("handle_meeting_summary"), "worker.py", "exec"), namespace)
    return namespace["handle_meeting_summary"], selfish, rec


ORIGIN = "C709097E-9FC5-4212-B9CF-03FC829F7C32"

# A real one: 229 characters is the transcript that failed at 04:28Z on
# 2026-09-03, one of seventeen since the gate shipped on 2026-08-31.
SHORT_TRANSCRIPT = (
    "[Scott (you)] Quick one before I forget. I told Ana I would send the "
    "revised pricing sheet by Thursday, and she is still blocked on the "
    "vendor list from procurement. Nothing else moved today. That is it."
)

PAYLOAD = {
    "user_id": "fa4d903c-24c0-45d5-9fdb-b5496e32501b",
    "content": SHORT_TRANSCRIPT,
    "app_id": "shouldersurf",
    "timestamp": "2026-09-03T04:28:57+00:00",
    "metadata": {
        "origin_id": ORIGIN,
        "origin_type": "meeting",
        "project": "ABM Industries",
        "project_id": "10437AFE",
    },
}


# ----------------------------------------------------------------------
# The gate path
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_gated_transcript_does_not_fail_the_task():
    """The bug, stated as the line production actually emitted.

    Note what this does NOT assert: that the call raises. It cannot. The
    handler wraps everything in `except Exception`, so the UnboundLocalError
    is caught, written to one log line, and the coroutine returns normally.
    A test that only checked "did not raise" would be green on the bug.
    """
    assert len(SHORT_TRANSCRIPT) < GATE_FLOOR, "fixture must actually trip the gate"
    handler, selfish, rec = _build("too_short", listening=False, listening_types=set())

    await handler(selfish, PAYLOAD)

    assert rec.failure() is None, f"the ingest died: {rec.failure()}"
    assert "extraction_skipped" in rec.events()
    assert rec.llm_called is False, "a gated meeting must not be paid for"


@pytest.mark.asyncio
async def test_the_gate_still_pays_for_the_behavior_lane_and_the_headlines():
    """Not raising is not enough; the lane the gate was PRICED on must run.

    #356 kept this lane deliberately: meetings between 400 and 1200 chars
    produced 18 behavior patches across 8 meetings over 30 days, which is
    4.6x what the gate was ruled on. A fix that merely silences the
    exception without running these two would pay that cost silently.
    """
    handler, selfish, rec = _build("too_short", listening=False, listening_types=set())

    await handler(selfish, PAYLOAD)

    assert rec.behavior_args is not None, "the behavior lane never ran"
    assert rec.headline_args is not None, "the headline lane never ran"


@pytest.mark.asyncio
async def test_the_five_names_carry_their_real_values_not_none():
    """The names were never merely absent, they were UNBOUND.

    Binding them to None would stop the crash and quietly file every gated
    meeting's behavior rows under no origin and no project, which is a
    worse failure than the one being fixed because it writes rather than
    raises. So assert the VALUES, positionally, exactly as the lane is
    called: (user_id, summary, app_id, origin_id, origin_type, timestamp,
    project, project_id, user_label, resolved_manifest).
    """
    handler, selfish, rec = _build("too_short", listening=False, listening_types=set())

    await handler(selfish, PAYLOAD)

    args = rec.behavior_args
    assert args[3] == ORIGIN, f"origin_id was {args[3]!r}"
    assert args[4] == "meeting", f"origin_type was {args[4]!r}"
    assert args[5] == PAYLOAD["timestamp"], f"timestamp was {args[5]!r}"
    assert args[6] == "ABM Industries", f"project was {args[6]!r}"
    assert args[7] == "10437AFE", f"project_id was {args[7]!r}"

    # And the headline lane, which is where a gated meeting's tiles come
    # from: (user_id, origin_id, app_id).
    assert rec.headline_args[1] == ORIGIN, "headlines got the wrong origin"


@pytest.mark.asyncio
async def test_the_skip_log_carries_the_origin_it_skipped():
    """A gated meeting is only findable later by its origin.

    `extraction_skipped` is the one record that a transcript arrived and
    was declined on purpose. Without the origin on it, a declined meeting
    and a lost one are the same log line.
    """
    handler, selfish, rec = _build("too_short", listening=False, listening_types=set())

    await handler(selfish, PAYLOAD)

    skipped = dict(next(kw for event, kw in rec.logged if event == "extraction_skipped"))
    assert skipped["origin"] == ORIGIN
    assert skipped["reason"] == "too_short"
    assert skipped["chars"] == len(SHORT_TRANSCRIPT)


# ----------------------------------------------------------------------
# The doc 22 listening path, which reads the same unbound name
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_listening_material_with_no_declared_types_logs_and_returns():
    """#414 added a second reader of `origin_id` above the binding.

    The listening path is inert in production until GhostPour forwards
    `material_kind`, so this branch has never been taken. It would have
    raised the identical UnboundLocalError on the first app that declared
    none of takeaway, event or artifact.
    """
    handler, selfish, rec = _build("too_short", listening=True, listening_types=set())

    await handler(selfish, PAYLOAD)

    assert "listening_no_declared_types" in rec.events()
    logged = dict(next(kw for e, kw in rec.logged if e == "listening_no_declared_types"))
    assert logged["origin"] == ORIGIN

    # It returns BEFORE the gate, so neither lane runs: nobody in a
    # recording owes the listener a behavior observation.
    assert rec.behavior_args is None
    assert rec.headline_args is None
