"""One definition of which meetings belong to a project.

The defect these guard: `meeting_count` counted rows in
`origin_project_assignments`, a table holding FIVE rows in all of
production, so it read 0 for 85 of 160 projects including one with 94
real meetings. The same join was the roster's `observed` leg.

These tests are deliberately about the SHAPE of the union rather than
about a rendered string appearing somewhere in the file. This module
already learned that lesson once: a sabotage that deleted a value left
every source-reading test green because the word also appeared in a
docstring.
"""

import re

import pytest

from contextquilt.services import project_meetings as pm


def _tables(sql: str):
    return set(re.findall(r"FROM\s+(\w+)", sql))


class TestBothSourcesPresent:
    """Either record can put a meeting in a project, so both are read."""

    def test_reads_the_assignment_table(self):
        assert "origin_project_assignments" in _tables(
            pm.meetings_for_project_sql("p.project_id"))

    def test_reads_the_patches(self):
        # The leg whose absence WAS the bug.
        assert "context_patches" in _tables(
            pm.meetings_for_project_sql("p.project_id"))

    def test_the_two_are_unioned_not_chosen_between(self):
        sql = pm.meetings_for_project_sql("p.project_id")
        assert re.search(r"\bUNION\b", sql), "one source would silently win"

    def test_union_is_not_union_all(self):
        # A meeting recorded BOTH ways is one meeting.
        sql = pm.meetings_for_project_sql("p.project_id")
        assert not re.search(r"\bUNION\s+ALL\b", sql)


class TestProjectRefReachesBothLegs:
    """A ref interpolated into only one leg scopes half the query."""

    def test_ref_appears_in_each_leg(self):
        sql = pm.meetings_for_project_sql("$2")
        legs = re.split(r"\bUNION\b", sql)
        assert len(legs) == 2
        for leg in legs:
            assert "$2" in leg

    def test_a_correlated_column_works_too(self):
        legs = re.split(
            r"\bUNION\b", pm.meetings_for_project_sql("p.project_id"))
        assert all("p.project_id" in leg for leg in legs)


class TestNullOriginsExcluded:
    """A patch with no origin is not a meeting."""

    def test_both_legs_exclude_null_origin(self):
        legs = re.split(
            r"\bUNION\b", pm.meetings_for_project_sql("p.project_id"))
        for leg in legs:
            assert "IS NOT NULL" in leg


class TestSingleColumnContract:
    """Callers JOIN on `origin_id`; the shape is the contract."""

    def test_every_leg_names_the_column_origin_id(self):
        sql = pm.meetings_for_project_sql("$2")
        assert len(re.findall(r"AS origin_id", sql)) == 2

    def test_count_wraps_the_same_definition(self):
        inner = pm.meetings_for_project_sql("p.project_id")
        count = pm.meeting_count_sql("p.project_id")
        assert inner.strip() in count
        assert count.strip().startswith("(SELECT count(*)")


class TestRefGuard:
    """The ref is interpolated, so it must never be caller input."""

    @pytest.mark.parametrize("bad", ["", None, 0, [], {}])
    def test_rejects_a_non_expression(self, bad):
        with pytest.raises(ValueError):
            pm.meetings_for_project_sql(bad)


class TestStatusIsNotFiltered:
    """A meeting whose patches all decayed still happened.

    Documented choice, not an oversight: `patch_count` is active-only
    and means what it says, while a project reading '5 meetings, 0
    patches' is informative where a silent 0 misleads.
    """

    def test_no_status_predicate(self):
        assert "status" not in pm.meetings_for_project_sql("p.project_id")
