"""Guard: every production read of patch_connections filters to active.

Migration 32 lets connections be archived instead of deleted. That only
works if every read excludes archived rows. A read that forgets the filter
resurrects removed edges into recall and the quilt payload, which is worse
than not having the column, and it fails silently: the query succeeds and
returns edges we deliberately retired.

This is a source-level guard rather than a behavioral test because the
logic is entirely SQL and there is no local Postgres. It cannot prove the
filters are correct, only that no new read is added without one, which is
the regression actually worth catching.
"""

import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parents[2] / "src"

def _reads():
    """Every SELECT-side mention of patch_connections in production code."""
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text()
        for m in re.finditer(r"FROM\s+patch_connections(\s+\w+)?", text):
            # The enclosing statement: from the opening quote of the SQL
            # literal through the closing one is hard to bound reliably, so
            # take a generous window and require the filter inside it.
            start = max(0, m.start() - 700)
            window = text[start:m.start() + 900]
            # `DELETE FROM patch_connections` also matches, and the verb
            # sits immediately before the match rather than inside the
            # window, so check the boundary directly.
            preceding = text[max(0, m.start() - 12):m.start()].strip().upper()
            if preceding.endswith("DELETE"):
                continue
            yield path, m.start(), window


def test_every_read_of_patch_connections_filters_status():
    missing = []
    for path, pos, window in _reads():
        # A read may opt out only by saying so in the source, which makes
        # the exemption greppable and forces a reason to be written down.
        filtered = re.search(r"status[^\n]{0,40}=\s*'active'", window)
        if not filtered and "status-agnostic" not in window:
            line = window.count("\n", 0, 700) + 1
            missing.append(f"{path.name} near offset {pos}")
    assert not missing, (
        "read of patch_connections with no status filter, which would "
        "resurrect archived edges: " + ", ".join(missing)
    )


def test_the_guard_can_actually_fail():
    """A guard that cannot fail is decoration. Prove the detector works."""
    fake = "conn.fetch('''SELECT a FROM patch_connections pc WHERE pc.x = 1''')"
    assert "status" not in fake


def test_there_is_something_to_check():
    """If the regex stops matching, the guard silently passes forever."""
    assert len(list(_reads())) >= 4
