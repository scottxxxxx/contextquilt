"""Unit tests for the length-scaled extraction patch backstop.

Design source: 2026-07-30 density probe (12 real meetings, uncapped:
natural emission 1→47, correlation with length only 0.558) plus the
dense synthetic fixtures (32 memories on a 1.7K-char standup). The
bound is a backstop against degenerate output, never a target, sized
never to bind observed legitimate content — with margin.
"""

from src.contextquilt.services.extraction_schema import extraction_patch_backstop


def test_floor_holds_for_tiny_transcripts():
    assert extraction_patch_backstop(0) == 36
    assert extraction_patch_backstop(900) == 36


def test_scales_with_length():
    assert extraction_patch_backstop(8000) == 44
    assert extraction_patch_backstop(25000) == 61


def test_ceiling_holds_for_huge_transcripts():
    assert extraction_patch_backstop(50000) == 64
    assert extraction_patch_backstop(10**9) == 64


def test_never_binds_observed_legitimate_content():
    # Probe maxima and the dense fixtures, each with real margin.
    assert extraction_patch_backstop(25248) > 46   # densest probed meeting
    assert extraction_patch_backstop(50699) > 47   # densest long meeting
    assert extraction_patch_backstop(1700) > 32    # stillwater fixture
    assert extraction_patch_backstop(8200) > 28    # harborview fixture


def test_custom_floor_and_ceiling():
    # floor is a minimum guarantee — it raises the bound, never lowers it
    assert extraction_patch_backstop(0, floor=50) == 50
    assert extraction_patch_backstop(0, floor=10) == 36  # scaled base wins over a lower floor
    assert extraction_patch_backstop(10**7, ceiling=40) == 40


def test_defensive_on_negative_input():
    assert extraction_patch_backstop(-5) == 36
