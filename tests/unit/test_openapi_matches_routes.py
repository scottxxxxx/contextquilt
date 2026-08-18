"""The spec and the routes are two things that must agree, with nothing
forcing them to.

GP's articulation, from finding a column header that had stopped
matching its own description:

    The description and the label came apart in a single commit, mine,
    and nothing connected them. Both halves were mine and I was right
    both times. Neither was a mistake at the moment it was written,
    which is why review would not have caught it either. Nobody was ever
    wrong, the pair just stopped being true together.

CQ carries the same class in several places. This guards the one that is
mechanically checkable: `docs/openapi.yaml` against the routes actually
registered in `src/main.py`.

MEASURED 2026-08-18: 48 routes registered, 19 declared, and the drift is
entirely in the SAFE direction. openapi never promises a route that does
not exist; it just describes a true subset. That is meaningfully less
dangerous than a name claiming more than its content admits, and it is
why the two directions get different treatment below.

No YAML dependency: pyyaml is not in requirements and adding one for a
test is heavier than the test. The parser here was verified to produce
an identical route set to yaml.safe_load before being trusted.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

VERBS = ("get", "post", "put", "patch", "delete")

# The gap as it stood when this guard was written. A RATCHET, not a
# target: it does not demand the backlog be documented, it stops the
# backlog growing. Lower it whenever routes get declared.
KNOWN_UNDECLARED = 29


def registered_routes():
    main = (ROOT / "src" / "main.py").read_text()
    return {
        (m.group(1).upper(), m.group(2))
        for m in re.finditer(r'@app\.(' + "|".join(VERBS) + r')\("([^"]+)"', main)
    }


def declared_routes():
    out, path = set(), None
    for line in (ROOT / "docs" / "openapi.yaml").read_text().splitlines():
        m = re.match(r"^  (/\S*):\s*$", line)
        if m:
            path = m.group(1)
            continue
        if path:
            v = re.match(r"^    (" + "|".join(VERBS) + r"):\s*$", line)
            if v:
                out.add((v.group(1).upper(), path))
            elif re.match(r"^  \S", line):
                path = None
    return out


def test_the_parsers_find_something():
    """A guard whose input silently became empty would pass forever. This
    is the check-your-instrument step, not a formality."""
    assert len(registered_routes()) > 30
    assert len(declared_routes()) > 10


def test_openapi_never_promises_a_route_that_does_not_exist():
    """HARD failure, no ratchet. A consumer reading the spec and building
    against a route we deleted gets a 404 they cannot debug from their
    side, which is the dangerous direction and is currently at zero."""
    ghosts = sorted(declared_routes() - registered_routes())
    assert not ghosts, (
        "openapi.yaml declares routes that main.py does not register: "
        + ", ".join(f"{v} {p}" for v, p in ghosts)
    )


def test_the_undocumented_surface_does_not_grow():
    """RATCHET. 29 real routes are undeclared and documenting them is a
    separate job; this only stops number 30 arriving unnoticed.

    Deliberately a count rather than a pinned set: pinning the set churns
    on every rename, and the point is the size of the gap, not its
    membership."""
    undeclared = registered_routes() - declared_routes()
    assert len(undeclared) <= KNOWN_UNDECLARED, (
        f"{len(undeclared)} routes are undeclared in openapi.yaml, up from "
        f"{KNOWN_UNDECLARED}. Declare the new one, or lower the ratchet with "
        "a reason. New: "
        + ", ".join(f"{v} {p}" for v, p in sorted(undeclared))
    )
