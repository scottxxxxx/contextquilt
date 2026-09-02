"""The fixed strings the people routes serve, by locale.

2026-09-02, found by ShoulderSurf on a Spanish device: the person page
showed the app's translated chrome wrapping an English clause from CQ.
Model-written prose (descriptions, lens bodies, do lines, claims) is
already in the meeting's language by construction (extraction writes in
`output_language`, the lens writer is told to match the listed items),
so the English was CQ's own constants: the subject line under each
computed fact, which the client renders verbatim inside a localized
sentence. The ledger `vocabulary` and the two on-wire definitions are
identifiers, not prose, and stay as they are.

Scott ruled: CQ localizes its fixed strings from `Accept-Language`, the
way recall labels already resolve from `metadata.locale`; SS sends the
header; translating prose ACROSS languages stays with GhostPour's
engine per the 08-23 ruling and is not attempted here. English is the
fallback for every locale not in the table, so an English caller and a
caller that sends no header see byte-identical output.

The translations below were written by the CQ session and REVIEWED by
ShoulderSurf on 2026-09-02, whose app is at 100% on these three
languages. Five of their corrections are in, and each was a real
defect rather than a preference: Spanish "acordarse" reads as "to
remember", so a handover read as "after remembering"; two Spanish
strings had no antecedent and no addressee in the two render sites
where `subject` stands alone with no colon frame; French "reformulés"
means rephrased when the measure is raised AGAIN, and their house form
is to name the person rather than pick a gender ("il ou elle" was the
one thing to avoid); Japanese 面談 is interview-flavoured and appears
in zero of their keys against 332 using 会議.

IMPORTANT for anyone adding a string here: `subject` renders in THREE
places, and only one wraps it in a colon frame ("Esta persona: 3 de
7: ..."). The count row under WHAT STANDS OUT and the working-with
view render it as a bare standalone line. A noun phrase survives that;
anything with an implied subject or addressee does not.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from contextquilt.services.relationship_lenses import FACT_SUBJECTS

DEFAULT_LOCALE = "en"

FACT_SUBJECT_LABELS: Dict[str, Dict[str, str]] = {
    "en": dict(FACT_SUBJECTS),
    "es": {
        "went_quiet": "temas abiertos que no han surgido en sus días de reunión recientes",
        "closed_late": "temas cerrados después de la fecha en que vencían",
        "re_dated": "temas cuya fecha de vencimiento cambió al menos una vez",
        "handed_back": "temas que cambiaron de responsable una vez acordados",
        "restated": "temas abiertos que esta persona ha vuelto a plantear en más de una reunión",
    },
    "fr": {
        "went_quiet": "sujets ouverts qui ne sont pas revenus lors de vos journées de réunion récentes",
        "closed_late": "sujets clos après leur date d'échéance",
        "re_dated": "sujets dont la date d'échéance a été déplacée au moins une fois",
        "handed_back": "sujets dont le responsable a changé après accord",
        "restated": "sujets ouverts que cette personne a soulevés dans plus d'une réunion",
    },
    "ja": {
        "went_quiet": "最近の会議日に話題に上がらなかった未完了の項目",
        "closed_late": "期限を過ぎてから完了した項目",
        "re_dated": "期限が少なくとも一度変更された項目",
        "handed_back": "合意後に担当者が変わった項目",
        "restated": "複数の会議で繰り返し持ち出された未完了の項目",
    },
}


def resolve_locale(accept_language: Optional[str]) -> str:
    """The first language tag in an Accept-Language header that the
    table knows, else English. Quality weights are respected in order
    of appearance only, which is what every client here sends."""
    if not accept_language:
        return DEFAULT_LOCALE
    for part in accept_language.split(","):
        tag = part.split(";", 1)[0].strip().lower()
        if not tag:
            continue
        primary = tag.split("-", 1)[0]
        if primary in FACT_SUBJECT_LABELS:
            return primary
    return DEFAULT_LOCALE


def localize_facts(facts: Any, locale: str) -> Any:
    """Replace each computed fact's `subject` with the locale's wording.

    Facts are stored on the insight row at write time with the English
    subject, so this runs at serve time, keyed by the fact's `key`. A
    fact whose key the table does not know keeps its stored subject; a
    locale the table does not know is English; English callers get the
    stored bytes back untouched, so nothing changes for them.
    """
    if locale == DEFAULT_LOCALE or not isinstance(facts, list):
        return facts
    table = FACT_SUBJECT_LABELS.get(locale)
    if not table:
        return facts
    out: List[Any] = []
    for f in facts:
        if isinstance(f, dict) and f.get("key") in table:
            out.append({**f, "subject": table[f["key"]]})
        else:
            out.append(f)
    return out
