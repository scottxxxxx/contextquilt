"""separated_from on every People row (lost-phone recovery, 2026-08-24).

SS's merge-proposal veto (peopleDismissedMergePairs) was a device-only
UserDefaults cache of a ruling CQ already stores in entity_separations.
Serving the pairs makes the client cache derived: a fresh phone never
re-proposes a pair the user refused.
"""
import pathlib

MAIN = (pathlib.Path(__file__).resolve().parents[2] / "src" / "main.py").read_text()


def test_list_rows_carry_sorted_separations_from_the_server_table():
    i = MAIN.index("async def _people_core(")
    body = MAIN[i:MAIN.index("\nasync def ", i + 10)]
    assert "await _read_separations(conn, user_id, [str(i) for i in ids])" in body
    assert 'separated_by_id.setdefault(lo, set()).add(hi)' in body
    assert 'separated_by_id.setdefault(hi, set()).add(lo)' in body      # both directions
    assert '"separated_from": sorted(separated_by_id.get(eid, ()))' in body


def test_it_degrades_to_empty_on_a_db_without_the_table():
    i = MAIN.index("separated_by_id: dict = {}")
    block = MAIN[i:i + 500]
    assert "except Exception" in block and "separations_unavailable" in block
