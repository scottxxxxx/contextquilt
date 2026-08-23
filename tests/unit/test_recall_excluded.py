"""The recall `excluded` block (GP #773, 2026-08-23): the two memory
moments an upgrade can name, counted in MEETINGS, from one indexed COUNT
per condition on project-scoped recalls only. Never a second recall.
Measured on the largest prod project: ~5 ms warm per count."""
import pathlib

MAIN = (pathlib.Path(__file__).resolve().parents[2] / "src" / "main.py").read_text()


def _block():
    i = MAIN.index("# The `excluded` block (GP #773")
    return MAIN[i:MAIN.index("# Cap for flat output", i)]


def test_it_is_on_the_response_model_and_survives_the_render_cache():
    assert "excluded: Optional[Dict[str, Any]] = None" in MAIN
    assert MAIN.count('"excluded": excluded,') == 2          # both lane cache writes
    assert 'excluded=cached.get("excluded")' in MAIN          # cache hit carries it
    assert MAIN.count("excluded=excluded,") == 2              # both lane returns


def test_only_on_project_scope_and_never_a_second_recall():
    b = _block()
    assert "if has_project_scope:" in b
    assert b.count("await db_pool.fetch") == 2                # two counts at most
    assert "count(DISTINCT cp.origin_id)" in b                # meetings, not patches
    assert "_fetch_" not in b and "traverse" not in b         # no recall leg re-run


def test_by_window_is_the_age_predicate_inverted_and_skips_universal_types():
    b = _block()
    i = b.index('excluded["by_window"]')
    q = b[b.index("if max_age_days is not None:"):i]
    assert "< ((NOW() AT TIME ZONE 'utc')::date - $4::int)" in q
    assert "NOT (cp.patch_type = ANY($3::text[]))" in q
    assert '"oldest"' in b[i:i + 400] and '"max_age_days": max_age_days' in b[i:i + 400]


def test_by_scope_uses_the_same_age_predicate_as_every_leg():
    b = _block()
    i = b.index('recall_scope") == "people"')
    q = b[i:b.index('excluded["by_scope"]', i)]
    assert '{AGE}' in q and 'AGE.format(d="$4", u="$3")' in q


def test_definitions_ride_on_the_wire_and_absent_means_not_computed():
    b = _block()
    assert b.count('"definition": (') == 2
    assert "if not excluded:\n                excluded = None" in b
    assert "A count of the scope, not of matches" in b
