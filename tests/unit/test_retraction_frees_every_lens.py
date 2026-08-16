"""A retracted card must not block re-derivation on ANY lens.

#255 drew the distinction (a card CQ withdrew is not a user's no) and I
applied it to the contrastive lens only. An hour later I retracted six
prose cards to clear repeated openings and the next cycle created
NOTHING, because the prose checks still counted them. Six people lost a
card permanently and silently.

There are FOUR places that read a lens stamp, and a fix that lands in
one of them is not a fix. This pins all four.
"""

import pathlib

WORKER = pathlib.Path("src/worker.py").read_text()

RETRACTED_SKIP = "archive_cause', '') <> 'retracted'"


def test_every_lens_stamp_reader_skips_retracted_cards():
    """One per reader: the prose helper, the prose SQL candidate gate,
    the follow-through candidate gate, and the contrastive check."""
    assert WORKER.count(RETRACTED_SKIP) == 4


def test_the_prose_helper_skips_retracted():
    helper = WORKER.split("async def _taken_lenses(")[1].split("async def ")[0]
    assert RETRACTED_SKIP in helper


def test_the_prose_candidate_gate_skips_retracted():
    """The SQL gate removes a person from the candidate set before any
    call is spent, so a retraction invisible here costs the person their
    slot as surely as the helper does."""
    gate = WORKER.split("durable-no idempotency, PER LENS")[1].split("GROUP BY")[0]
    assert RETRACTED_SKIP in gate


def test_a_user_delete_still_blocks_everywhere():
    """The whole point of the distinction. Only 'retracted' is exempt;
    every other archive cause, user_delete above all, still counts."""
    assert "'user_delete'" not in WORKER.split(RETRACTED_SKIP)[0][-400:]
    for chunk in WORKER.split(RETRACTED_SKIP)[1:]:
        assert "<> 'user_delete'" not in chunk[:200]
