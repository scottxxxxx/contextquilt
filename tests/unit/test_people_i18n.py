"""CQ's fixed strings on the people routes follow Accept-Language.

Found by SS on a Spanish device (2026-09-02): the app's translated
chrome wrapping an English clause from CQ. Model prose was already in
the meeting's language; the English was the subject line under each
computed fact.
"""

import ast
import pathlib

from contextquilt.services.people_i18n import (
    DEFAULT_LOCALE, FACT_SUBJECT_LABELS, localize_facts, resolve_locale)
from contextquilt.services.relationship_lenses import FACT_SUBJECTS

MAIN = pathlib.Path("src/main.py").read_text()


def test_every_locale_covers_every_fact_key_and_english_is_the_source():
    for locale, table in FACT_SUBJECT_LABELS.items():
        assert set(table) == set(FACT_SUBJECTS), locale
    assert FACT_SUBJECT_LABELS["en"] == FACT_SUBJECTS


def test_no_dash_punctuation_in_any_locale():
    import re
    for locale, table in FACT_SUBJECT_LABELS.items():
        for v in table.values():
            assert not re.search("[–—]", v), (locale, v)


def test_locale_resolution_takes_the_first_known_tag_and_falls_back_to_english():
    assert resolve_locale(None) == "en"
    assert resolve_locale("") == "en"
    assert resolve_locale("es-MX,es;q=0.9,en;q=0.8") == "es"
    assert resolve_locale("de-DE,de;q=0.9,fr;q=0.5") == "fr"
    assert resolve_locale("de-DE") == "en"
    assert resolve_locale("ja-JP") == "ja"


# THE REAL STORED SHAPE, copied from a production insight row on
# 2026-09-02 rather than written from the model class. One OBJECT, and
# the key field is `fact_key`. The first version of these tests invented
# a list of {"key": ...} from `Fact.as_dict()` and passed while the
# function localized nothing on real data.
PROD_FACTS = {
    "subject": "items whose due date moved at least once",
    "fact_key": "re_dated",
    "direction": "better",
    "numerator": 0,
    "denominator": 5,
    "about_person": "Satyajit Nanda",
    "roster_people": 8,
    "roster_numerator": 25,
    "roster_denominator": 151,
}


def test_localize_the_shape_production_actually_stores():
    es = localize_facts(PROD_FACTS, "es")
    assert es["subject"] == FACT_SUBJECT_LABELS["es"]["re_dated"]
    # Everything else survives, including the numbers the receipts rail
    # proves the claim with.
    assert es["numerator"] == 0 and es["denominator"] == 5
    assert es["roster_denominator"] == 151 and es["about_person"] == "Satyajit Nanda"
    assert es["fact_key"] == "re_dated"


def test_an_unknown_fact_key_keeps_its_stored_subject():
    other = {**PROD_FACTS, "fact_key": "something_new", "subject": "kept as stored"}
    assert localize_facts(other, "fr")["subject"] == "kept as stored"


def test_a_list_and_the_key_spelling_from_the_model_class_also_work():
    """`Fact.as_dict()` emits `key`, and a future lens could serve a
    list. Both exist in this codebase, so both are handled."""
    facts = [{"key": "went_quiet", "numerator": 3, "denominator": 7,
              "subject": FACT_SUBJECTS["went_quiet"], "higher_is_worse": True},
             {"key": "unknown_key", "subject": "kept as stored"}]
    es = localize_facts(facts, "es")
    assert es[0]["subject"] == FACT_SUBJECT_LABELS["es"]["went_quiet"]
    assert es[0]["numerator"] == 3 and es[0]["key"] == "went_quiet"
    assert es[1] == facts[1]


def test_english_and_unknown_locales_return_the_stored_object_untouched():
    assert localize_facts(PROD_FACTS, "en") is PROD_FACTS
    assert localize_facts(PROD_FACTS, "de") is PROD_FACTS
    assert localize_facts(None, "es") is None
    assert localize_facts("not a fact", "es") == "not a fact"


def test_the_detail_route_reads_accept_language_and_localizes_facts():
    tree = ast.parse(MAIN)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "get_person")
    src = ast.get_source_segment(MAIN, fn)
    assert 'alias="Accept-Language"' in src
    assert "people_i18n.localize_facts(" in src
    assert "people_i18n.resolve_locale(accept_language)" in src
