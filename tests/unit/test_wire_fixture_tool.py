"""The fixture tool serializes the way the ROUTE does, not the way a script would.

2026-08-31: a fixture generated "from the running code" carried
`"occurred_at": "2026-08-31 14:14:10.707165+00:00"`, a space where
ISO-8601 wants a T, because the throwaway script used
`json.dumps(..., default=str)` while the route returns a raw datetime
that FastAPI renders with `.isoformat()`. Same process, same image, same
data, different serializer.

ShoulderSurf decoded those bytes, found a present-but-unparseable date
throws rather than yielding nil, watched one timestamp fail the patch,
the array and the whole digest, and shipped a fix for a bug that did not
exist. They had a note in their own repo documenting CQ timestamps
correctly and did not look at it, because the fixture was newer and
generated.

A generated artifact carries more authority than a description while
being just as capable of being wrong, and the authority is the defect.
"""

from pathlib import Path

TOOL = (Path(__file__).resolve().parents[2]
        / "scripts" / "dump_wire_fixture.py").read_text()


def test_it_serializes_through_the_frameworks_own_encoder():
    assert "from fastapi.encoders import jsonable_encoder" in TOOL
    assert "jsonable_encoder(obj)" in TOOL


def test_it_never_reaches_for_the_serializer_that_caused_this():
    """`default=str` is the exact call that produced the bad byte.

    Asserted over the AST rather than the text, because the module
    docstring names the bad call deliberately and a substring check
    cannot tell prose from code. The first version of this test could
    not, and the honest fix was a narrower test rather than a quieter
    docstring: the receipt is the reason anyone will believe the rule.
    """
    import ast
    for node in ast.walk(ast.parse(TOOL)):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg == "default":
                assert not (isinstance(kw.value, ast.Name)
                            and kw.value.id == "str"), (
                    "a call passes default=str; that is the serializer "
                    "that produced a fixture differing from the wire"
                )


def test_the_docstring_carries_the_receipt_rather_than_a_rule():
    # A rule without its receipt gets argued with; this one cost another
    # team a debugging session on a bug that was never there.
    assert "occurred_at" in TOOL
    assert "DIFFERENT" in TOOL and "SERIALIZER" in TOOL


def test_the_lean_mode_picks_the_hardest_patch_not_a_convenient_one():
    # A fixture is only worth pinning a decoder against if it is the
    # sparsest thing the route can emit.
    assert "min(patches, key=lambda p: sum(1 for v in p.values() if v))" in TOOL


def test_the_seam_dump_does_not_invent_tiling_fields():
    # The seam has no weight, span or height: capture order, nothing to
    # tile. A fixture that added them would teach a decoder to require
    # fields the route never sends.
    seam = TOOL[TOOL.index('else:\n        # The seam builds its own dict'):]
    seam = seam[:seam.index("if not patches:")]
    for absent in ('"weight"', '"span"', '"height"'):
        assert absent not in seam
