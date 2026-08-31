"""A locale table missing one label erased the whole context block.

`format_category_grouped` indexes `labels['goals']` directly, and NOT ONE
of the five locale tables in main.py defines a `goals` key: en, es, fr,
pt and ja all carry the same nine. So a grouped-mode recall for a user
with any goal patch raised KeyError, and main.py's defensive
`except Exception: context = ""` turned that into an EMPTY CONTEXT BLOCK.

Empty is exactly what "nothing matched" looks like, so from every
caller's side the failure was indistinguishable from a clean result.
That is the same shape as the role-semantics gate that declined without
logging (#350), and the reason it survived is written on the handler
itself: "test coverage is flat-mode".

The fix is that caller labels MERGE OVER the formatter's defaults rather
than replacing them, so no key the formatter indexes can ever be absent,
and a locale that omits one degrades to its English label instead of
erasing everything. The `goals` label is also added to all five tables
so non-English callers get a translated word rather than the fallback.
"""

import pytest

from contextquilt.services.recall_formatter import (
    _DEFAULT_LABELS,
    format_category_grouped,
)

# The nine keys every locale table in main.py actually carried.
LOCALE_NINE = {
    "project": "Project", "people": "People", "connections": "Connections",
    "about_you": "About you", "decisions": "Decisions",
    "commitments": "Open commitments", "blockers": "Blockers",
    "roles": "Roles", "key_facts": "Key facts",
}


def scored(patch_type, text, score=10.0):
    return [(score, {"patch_id": f"{patch_type}-1", "patch_type": patch_type,
                     "value": {"text": text}})]


def test_a_goal_patch_does_not_erase_the_block_for_a_locale_caller():
    # The exact reproduction: KeyError before the fix, swallowed upstream
    # into "". A goal is an ordinary patch type and 169 of them are live.
    out = format_category_grouped(scored("goal", "Achieve 3M ARR"), [], [], LOCALE_NINE)
    assert "Achieve 3M ARR" in out, "the goal text was dropped"
    assert out.startswith("Goals:"), out


@pytest.mark.parametrize("locale_labels", [
    LOCALE_NINE,
    {},                     # a locale table that carries nothing
    None,                   # no labels at all, the formatter's own default
])
def test_no_label_set_can_erase_content(locale_labels):
    out = format_category_grouped(scored("goal", "Ship v2"), [], [], locale_labels)
    assert "Ship v2" in out


def test_a_locale_keeps_its_own_words_where_it_has_them():
    # Merging must not overwrite a translation with the English default.
    spanish = dict(LOCALE_NINE, commitments="Compromisos abiertos")
    out = format_category_grouped(
        scored("commitment", "Enviar el informe"), [], [], spanish)
    assert out.startswith("Compromisos abiertos:"), out


def test_every_key_the_formatter_indexes_has_a_default():
    # The failure was a key the formatter indexes with [] and no table
    # defined. This pins the invariant rather than the one instance:
    # anything indexed directly must exist in the defaults.
    from pathlib import Path
    import re
    src = (Path(__file__).resolve().parents[2]
           / "src/contextquilt/services/recall_formatter.py").read_text()
    indexed = set(re.findall(r"labels\['([a-z_]+)'\]", src))
    assert indexed, "expected to find direct label indexing"
    missing = indexed - set(_DEFAULT_LABELS)
    assert not missing, f"indexed with no default, will KeyError: {sorted(missing)}"


def test_main_defines_goals_for_every_locale():
    # The other half of the fix. Without it, non-English callers get the
    # English word rather than a crash, which is better but still wrong.
    from pathlib import Path
    import re
    src = (Path(__file__).resolve().parents[2] / "src/main.py").read_text()
    block = re.search(r"_RECALL_LABELS = \{(.*?)\n\}\n", src, re.S).group(1)
    tables = re.findall(r'"(\w\w)": \{(.*?)\n    \}', block, re.S)
    assert len(tables) >= 5, f"expected the five locales, found {len(tables)}"
    for code, body in tables:
        assert '"goals"' in body, f"locale {code} has no goals label"
