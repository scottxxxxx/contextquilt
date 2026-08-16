"""Every lens's system prompt must carry the shared card shape rules.

The failure that produced this file (2026-08-16): `what_stands_out`
shipped with `""" + '"""' + """ + """ + '"""' + """ where the
follow-through prompt has `""" + '"""' + """ + CARD_SHAPE_RULES + """ + '"""' + """.
The concatenation was syntactically fine and silently dropped every
length limit, the do-line limit, and the never-open-with-the-name rule.

Consequence on production, two consolidation cycles in a row: the pass
selected exactly the right people and then rejected 4 of 4 cards with
`claim_too_long`, because the writer had never been told a limit
existed. `created=0` both times.

This is doc 19.2 at small scale. The rules had exactly one carrier per
prompt, so their disappearance was silent by construction. The fix for
that is never a more careful author, it is a test that the carrier is
still there, which is what this is.
"""

import pytest

from contextquilt.services.consolidation import PROFILE_SYSTEM
from contextquilt.services.follow_through import FOLLOW_THROUGH_SYSTEM
from contextquilt.services.insight_cards import (
    CARD_SHAPE_RULES,
    MAX_CLAIM_CHARS,
    MAX_DO_CHARS,
)
from contextquilt.services.relationship_lenses import STANDS_OUT_SYSTEM

# Every prompt that asks a model to write into a 16a card. A new lens
# belongs here on the day it is written.
CARD_WRITING_PROMPTS = {
    "how_they_decide / what_moves_them": PROFILE_SYSTEM,
    "how_they_follow_through": FOLLOW_THROUGH_SYSTEM,
    "what_stands_out": STANDS_OUT_SYSTEM,
}


@pytest.mark.parametrize("lens,prompt", sorted(CARD_WRITING_PROMPTS.items()))
def test_the_shared_card_rules_reach_the_model(lens, prompt):
    """Not 'the limits are documented somewhere' but 'this exact text is
    in the string the model receives'."""
    assert CARD_SHAPE_RULES in prompt, (
        f"{lens} does not carry CARD_SHAPE_RULES; its writer will never "
        "be told the card has a size"
    )


@pytest.mark.parametrize("lens,prompt", sorted(CARD_WRITING_PROMPTS.items()))
def test_the_real_ceilings_are_stated_numerically(lens, prompt):
    """A rule the model cannot act on is not a rule. The hard limits have
    to appear as numbers, not as 'keep it short'."""
    assert str(MAX_CLAIM_CHARS) in prompt
    assert str(MAX_DO_CHARS) in prompt


@pytest.mark.parametrize("lens,prompt", sorted(CARD_WRITING_PROMPTS.items()))
def test_the_name_rule_reaches_the_model(lens, prompt):
    """The card sits on the person's own page, so opening the claim with
    their name spends a fifth of the visible teaser on the one word the
    reader already has."""
    assert "NEVER begin the claim with the person's name" in prompt
