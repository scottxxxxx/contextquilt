"""The stated_roles query, EXECUTED against a real Postgres.

This file exists because a source-reading test cannot see a query that
does not parse. On 2026-09-06 the word-boundary fix (#463) replaced the
`LIKE ANY($3)` leg with an EXISTS subquery and left `$3` bound but
unreferenced. Postgres cannot infer an unused parameter's type, so every
call raised "could not determine data type of parameter $3", the route's
`except Exception` swallowed it into `stated_roles_unavailable`, and
EVERY person on prod served `stated_roles: null` for nine minutes.

The unit suite was 3025 green throughout. Three source-reading tests
asserted the boundary predicate was present, and it was: the SQL was
correct and unrunnable. That is doc 19.12 with no ambiguity, so the
executing sibling lives here.

Needs a live Postgres via TEST_DATABASE_URL; CI provides one.
"""

import os
import re
import pathlib

import pytest

TEST_DB = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL not set")

MAIN = pathlib.Path("src/main.py").read_text()


def _stated_roles_sql() -> str:
    """The query as SHIPPED, lifted from the route rather than retyped.

    Retyping it here would make this file agree with a copy instead of
    with production, which is the fixture trap.
    """
    body = MAIN.split("async def get_person")[1].split("\n@app.")[0]
    after = body.split("role_rows = await db_pool.fetch(\n")[1]
    return after.split('"""')[1]


@pytest.mark.asyncio
async def test_the_stated_roles_query_actually_runs():
    """The whole point. It must PARSE and EXECUTE, not merely contain
    the right words."""
    import asyncpg
    conn = await asyncpg.connect(TEST_DB)
    try:
        keys = ["someone nobody has"]
        rows = await conn.fetch(_stated_roles_sql(),
                                "user:00000000-0000-0000-0000-000000000000",
                                "role", keys, keys)
        assert rows == []           # empty is correct; not raising is the test
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_every_bound_parameter_is_referenced():
    """The exact defect, caught structurally as well as by execution.

    Postgres refuses a parameter it cannot type, and it cannot type one
    the query never mentions. So the count of $N in the SQL must equal
    the count of arguments the route passes.
    """
    sql = _stated_roles_sql()
    refs = {int(n) for n in re.findall(r"\$(\d+)", sql)}
    assert refs == set(range(1, max(refs) + 1)), (
        f"parameters must be contiguous from $1; got {sorted(refs)}")

    body = MAIN.split("async def get_person")[1].split("\n@app.")[0]
    call = body.split("role_rows = await db_pool.fetch(\n")[1]
    args = call.split('"""')[2].split(")")[0]
    passed = len([a for a in args.split(",") if a.strip()])
    assert passed == max(refs), (
        f"query references ${max(refs)} but the route passes {passed} args")


@pytest.mark.asyncio
async def test_the_word_boundary_holds_when_executed():
    """"Anna" must not match a role about "Annapurna", proved by running
    the predicate rather than by reading it."""
    import asyncpg
    conn = await asyncpg.connect(TEST_DB)
    try:
        for text, name, expected in [
            ("Annapurna Patcharla leads HDBot development", "anna", False),
            ("Jayanth manages data", "jay", False),
            ("Anna is the VP of HR", "anna", True),
            ("Anna: VP of HR", "anna", True),
            ("Anna", "anna", True),                     # exact match survives
            ("Jayanth manages data", "jayanth", True),
        ]:
            got = await conn.fetchval(
                "SELECT lower($1::text) LIKE $2 || '%' "
                "AND substr(lower($1::text), length($2) + 1, 1) !~ '[[:alpha:]]'",
                text, name)
            assert got is expected, (text, name, got)
    finally:
        await conn.close()
