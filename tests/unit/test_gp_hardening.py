"""The two hardenings promised to GP on 2026-08-07.

1. Unknown fields on CONNECTION objects are a 422, never silently
   dropped (their `relationship` -> `label` rename shipped payloads
   whose real field was ignored and the edge landed with label NULL).
2. `cta:`-prefixed (and any non-UUID) patch ids on the patch verbs are
   a 422 with a message that says what happened, not an asyncpg cast
   error wearing a 500.
"""

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

SRC = Path(__file__).resolve().parents[2] / "src"
MAIN = (SRC / "main.py").read_text()


# --------------------------------------------------------------------
# 1. Connection objects forbid unknown fields (pure model behavior)
# --------------------------------------------------------------------

def _load_models():
    """The connection models without importing main.py (fastapi absent
    locally): exec just the model definitions against a stub namespace."""
    ns = {}
    exec(
        "from typing import Optional\n"
        "from pydantic import BaseModel, ConfigDict\n",
        ns,
    )
    for cls in ("PatchConnectionInput", "ConnectionCreate"):
        # Capture the class until the first line back at column 0, so
        # blank lines INSIDE the body (after model_config) don't truncate
        # the fields away.
        m = re.search(rf"class {cls}\(BaseModel\):\n(?:[ \t]+.*\n|\n)+", MAIN)
        assert m, f"{cls} not found"
        exec(m.group(0), ns)
    return ns["PatchConnectionInput"], ns["ConnectionCreate"]


def test_the_renamed_field_is_refused_not_dropped():
    """The exact payload that motivated the promise: `relationship`
    instead of `label`. Silent acceptance wrote an edge with label NULL
    while the caller believed they had labelled it."""
    PatchConnectionInput, ConnectionCreate = _load_models()
    with pytest.raises(ValidationError) as e:
        PatchConnectionInput(
            target_patch_id="x", role="informs", relationship="owns"
        )
    assert "relationship" in str(e.value)
    with pytest.raises(ValidationError):
        ConnectionCreate(
            from_patch_id="a", to_patch_id="b", role="informs", typo_label="owns"
        )


def test_known_fields_still_validate():
    PatchConnectionInput, ConnectionCreate = _load_models()
    ok = PatchConnectionInput(target_patch_id="x", role="informs", label="owns")
    assert ok.label == "owns"
    ok2 = ConnectionCreate(from_patch_id="a", to_patch_id="b", role="parent")
    assert ok2.label is None


def test_forbid_is_scoped_to_connection_shapes_only():
    """Top-level request models stay tolerant so additive evolution
    keeps working; the forbid contract is for connection objects, where
    an unknown key means a mis-named field, not a new feature."""
    assert MAIN.count('model_config = ConfigDict(extra="forbid")') == 2


# --------------------------------------------------------------------
# 2. cta: / non-UUID patch ids are a 422 on every patch verb
# --------------------------------------------------------------------

def test_validator_names_the_cta_case():
    m = re.search(r"def _require_patch_uuid.*?raise HTTPException.*?\n    \)", MAIN, re.DOTALL)
    assert m, "_require_patch_uuid not found"
    assert 'startswith("cta:")' in m.group(0)
    assert "INVALID_PATCH_ID" in m.group(0)
    assert "422" in m.group(0)


def test_every_patch_id_route_validates():
    """One rule, all verbs. A new {patch_id} route that skips the
    validator reintroduces the 500."""
    routes = [
        (m.group(1), m.group(0))
        for m in re.finditer(
            r'@app\.\w+\("(/v1/quilt/\{user_id\}/patches/\{patch_id\}[^"]*)"[^)]*\)\s*\n'
            r"async def \w+\(.*?(?=\n@app\.|\Z)",
            MAIN, re.DOTALL,
        )
    ]
    assert len(routes) >= 7, f"expected the 7 patch-id routes, found {len(routes)}"
    for path, body in routes:
        assert ("_require_patch_uuid(" in body) or ("_load_open_completable(" in body), (
            f"route {path} neither validates the patch id nor goes through "
            "_load_open_completable (which does)"
        )
