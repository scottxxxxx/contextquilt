"""Source-level guards for the triage surface (shelve / vouch / decay_state)
and for archive-cause stamping.

The logic under test is SQL plus request handling in main.py, which has no
local test double (fastapi/asyncpg are absent in the local venv), and its
failure modes are silent: a count that quietly includes shelved rows, or a
new archive site that forgets its cause stamp. These guards make both
greppable failures instead.
"""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
MAIN = (SRC / "main.py").read_text()
WORKER = (SRC / "worker.py").read_text()


# --------------------------------------------------------------------
# Shelved rows leave the ledger BEFORE anything counts anything
# --------------------------------------------------------------------

def test_both_ledger_sides_exclude_shelved_rows():
    """they_owe and you_owe each filter on shelved_at is None. The counting
    condition SS holds us to: every served count agrees with the rows it
    gates, so the exclusion must sit in the array construction, not in a
    separately-maintained count."""
    assert MAIN.count('r["shelved_at"] is None') >= 2, (
        "expected the shelved filter on both they_owe and you_owe"
    )


def test_open_by_decay_is_derived_from_they_owe():
    """The per-band counts are a sum over the SAME they_owe rows, not a
    second query that could disagree with the array it summarizes."""
    m = re.search(r'"open_by_decay":.*?\n\s*\},', MAIN, re.DOTALL)
    assert m, "open_by_decay missing from the person row"
    assert "for r in they_owe" in m.group(0)


def test_ledger_item_carries_the_triage_fields():
    """decay_state + shelved stamps on the item shape (doc section 5)."""
    m = re.search(r"def _item\(r\):.*?\n        \}", MAIN, re.DOTALL)
    assert m, "_item() not found"
    for field in ('"decay_state"', '"shelved_at"', '"shelved_source"'):
        assert field in m.group(0), f"{field} missing from ledger items"


def test_quilt_items_carry_the_shelved_stamps():
    """Shelved items STAY in /v1/quilt action_items (they are active; the
    ledger is what excludes them) and self-describe via the stamps, so a
    client can render or filter them without a tombstone."""
    assert "shelved_at: Optional[str] = None" in MAIN
    assert 'shelved_at=value.get("shelved_at")' in MAIN
    assert 'shelved_source=value.get("shelved_source")' in MAIN


# --------------------------------------------------------------------
# Write paths
# --------------------------------------------------------------------

def test_the_three_triage_routes_exist():
    assert '@app.post("/v1/quilt/{user_id}/patches/{patch_id}/vouch"' in MAIN
    assert '@app.post("/v1/quilt/{user_id}/patches/{patch_id}/shelve"' in MAIN
    assert '@app.delete("/v1/quilt/{user_id}/patches/{patch_id}/shelve"' in MAIN


def test_shelve_stays_active_never_archives():
    """The load-bearing half of "Let it go": a shelved patch stays active
    so recall still finds it. If the shelve handler ever writes status,
    it has become an archive and the delta cannot distinguish the user's
    deliberate act from decay."""
    m = re.search(
        r'@app\.post\("/v1/quilt/\{user_id\}/patches/\{patch_id\}/shelve".*?'
        r'@app\.delete', MAIN, re.DOTALL,
    )
    assert m, "shelve handler not found"
    assert "SET updated_at" in m.group(0)
    assert "status = 'archived'" not in m.group(0)


def test_vouch_stamps_the_deliberate_signal():
    """A vouch must be distinguishable from an incidental recall touch:
    the explicit stamp is the difference between "the user says this is
    live" and "this happened to surface"."""
    m = re.search(
        r'@app\.post\("/v1/quilt/\{user_id\}/patches/\{patch_id\}/vouch".*?'
        r'@app\.post\("/v1/quilt/\{user_id\}/patches/\{patch_id\}/shelve"',
        MAIN, re.DOTALL,
    )
    assert m, "vouch handler not found"
    assert "last_vouched_at" in m.group(0)
    assert "vouch_source" in m.group(0)
    assert "SET updated_at" in m.group(0), "the decay extension is the point"


def test_race_guards_repeat_the_state_predicate_in_the_update():
    """Same discipline as complete: the WHERE re-checks the state so a
    concurrent writer loses with a 409 instead of double-applying."""
    for probe in (
        "AND value->>'shelved_at' IS NULL",       # shelve
        "AND value->>'shelved_at' IS NOT NULL",   # un-shelve
    ):
        assert probe in MAIN, f"missing race guard: {probe}"


# --------------------------------------------------------------------
# archive_cause: no archive site without a stated reason
# --------------------------------------------------------------------

def _archive_update_windows(src: str):
    """Yield a window of source around every UPDATE that archives a patch."""
    for m in re.finditer(r"status = 'archived'", src):
        start = max(0, m.start() - 600)
        end = min(len(src), m.end() + 600)
        window = src[start:end]
        # Only UPDATE statements against context_patches count; SELECT
        # filters and patch_connections archival are different animals.
        if "UPDATE context_patches" in window or "UPDATE\n" in window:
            yield window


def test_every_patch_archive_site_states_its_cause():
    """A row archived without completed_at used to be unexplainable (864
    such rows on prod, cause unknown forever). Every archive site must
    stamp value.archive_cause, or be a completion (completed_at +
    completion_source already say why). A new site that does neither
    fails here instead of shipping another year of unexplained rows."""
    for name, src in (("main.py", MAIN), ("worker.py", WORKER)):
        for window in _archive_update_windows(src):
            assert ("archive_cause" in window) or ("completion_source" in window), (
                f"an archive site in {name} stamps no cause:\n...{window[:400]}..."
            )
