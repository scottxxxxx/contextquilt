"""The believed-completion question must reach the surfaces that ask it.

Found 2026-08-19 by the local two-meeting simulation: the worker stamped
believed_complete_* on the exact target patch and /v1/quilt served null
for every one of them. The fields lived on the People detail only, while
SS's confirm card (project section AND meeting review) is fed by the
quilt sync. GP's 08-17 middle-hop proof was real but proved the fields
SURVIVE the hop, not that the origin emits them on this route (rule 5:
name which side each claim was proved on).

Second finding, same session: answering the card must clear the WHOLE
family. vouch stripped four of the five stamps and left
believed_evidence_strength dangling on a denied item, and uncomplete
left all five, so undoing a confirmed card resurrected a stale question.
The subtraction is UNCONDITIONAL on purpose: a partial family (seen live
after a pre-fix vouch) would survive any guard keyed on
believed_complete_at alone; subtracting an absent jsonb key is a no-op.

Source-reading tests by house convention for main.py routes; the
runtime halves were proved against the live sim stack (worker stamp ->
quilt wire -> vouch -> uncomplete), 2026-08-19.
"""

import pathlib
import re

MAIN = pathlib.Path("src/main.py").read_text()

FAMILY = (
    "believed_complete_at",
    "believed_complete_evidence",
    "believed_complete_reasons",
    "believed_complete_origin_id",
    "believed_evidence_strength",
)


def test_quilt_response_model_declares_the_family():
    for key in FAMILY:
        assert f"{key}: Optional[" in MAIN, f"QuiltPatchResponse lacks {key}"


def test_quilt_serving_loop_populates_the_family():
    """Every field is read from value in the QuiltPatchResponse
    construction, gated on completable like its decay_state siblings."""
    build = MAIN[MAIN.index("patch = QuiltPatchResponse("):]
    build = build[: build.index("if row[\"patch_type\"] in completable:")]
    for key in FAMILY:
        assert f'value.get("{key}")' in build, (
            f"the quilt serving loop never reads {key}; the confirm "
            "card starves exactly the way it did before 2026-08-19"
        )


def _update_blocks(verb_marker: str) -> str:
    """Source window from a route marker to the next route decorator."""
    start = MAIN.index(verb_marker)
    end = MAIN.index("@app.", start + 1)
    return MAIN[start:end]


def test_vouch_strips_the_whole_family_unconditionally():
    block = _update_blocks("async def vouch_patch")
    for key in FAMILY:
        assert re.search(rf"-\s*'{key}'", block), (
            f"vouch does not subtract {key}; a denied card keeps part "
            "of the belief it just answered"
        )


def test_uncomplete_strips_the_whole_family_unconditionally():
    block = _update_blocks("async def uncomplete_patch")
    for key in FAMILY:
        assert re.search(rf"-\s*'{key}'", block), (
            f"uncomplete does not subtract {key}; undoing a confirmed "
            "card resurrects a stale question"
        )


def test_uncomplete_strips_the_user_declared_date_stamp():
    block = _update_blocks("async def uncomplete_patch")
    assert re.search(r"-\s*'completed_at_source'", block)
    assert "'prior_completed_at_source'" in block


def test_both_verbs_preserve_the_belief_as_prior():
    """Clearing is not erasing: 'believed then answered' must stay
    distinguishable from 'never believed'."""
    for verb in ("async def vouch_patch", "async def uncomplete_patch"):
        block = _update_blocks(verb)
        assert "'prior_believed_complete_at'" in block, verb
        assert "'prior_believed_complete_evidence'" in block, verb
