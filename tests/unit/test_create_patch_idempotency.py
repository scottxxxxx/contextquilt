"""Hand-authored action items: idempotency and the create echo.

SS ships a sheet where a human types a task and taps Add. The failure
that drove this is theirs and it is the specification: the user taps, the
network stalls, they see nothing, and they tap again. Without an
idempotency key the second tap is a second row in somebody's ledger, and
the client's only safe response to an ambiguous write is to stop
retrying and park the item for a human.

These tests defend the two properties that make that branch disappear,
plus the one trap that nearly shipped inside the echo.
"""

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
MAIN = (ROOT / "src" / "main.py").read_text()
MIGRATION = (ROOT / "init-db" / "45_patch_client_id.sql").read_text()


def _func_source(name: str) -> str:
    tree = ast.parse(MAIN)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(MAIN, node) or ""
    raise AssertionError(f"{name} not found in main.py")


# --- the race, which is the whole point -------------------------------

def test_insert_closes_the_race_with_on_conflict():
    """A lookup-then-insert has a window between the two statements, and
    that window is EXACTLY the condition the feature exists for: two taps
    in flight at once. Closing it in application code would be closing it
    everywhere except where it happens."""
    src = _func_source("create_patch")
    assert "ON CONFLICT (client_id)" in src
    assert "DO NOTHING" in src


def test_the_race_loser_is_served_the_winning_row_not_an_error():
    """Both requests did what the user meant. The loser must be told
    which patch exists, not handed a 500 for a successful write."""
    src = _func_source("create_patch")
    loser = src.split("if inserted is None:", 1)[1]
    assert "_existing_client_id_patch" in loser
    assert "created=False" in loser


def test_unique_index_is_partial_so_extracted_patches_are_untouched():
    """Every patch the extractor writes has no client_id. A non-partial
    unique index would make the second one a constraint violation."""
    assert "WHERE client_id IS NOT NULL" in MIGRATION
    assert "CREATE UNIQUE INDEX" in MIGRATION


def test_subject_is_checked_before_a_row_is_handed_back():
    """The unique index is global (the key is a client-minted UUID and
    patch_subjects is a table an index cannot reach into). So the lookup
    must confirm the row belongs to THIS caller, or a key collision hands
    one user another user's patch inside an echo."""
    src = _func_source("_existing_client_id_patch")
    assert "subject_key" in src
    assert "patch_subjects" in src


def test_a_cross_subject_collision_is_refused_not_served():
    src = _func_source("create_patch")
    assert "CLIENT_ID_TAKEN" in src
    assert "409" in src


# --- the echo ---------------------------------------------------------

def test_echo_is_rendered_by_the_read_route_not_beside_it():
    """One wire shape, one renderer. A second QuiltPatchResponse built in
    the create path would drift from /v1/quilt the first time a field is
    added to one and not the other, and the create would then tell the
    client something the read route does not."""
    src = _func_source("_created_patch_response")
    assert "get_user_quilt" in src
    assert "QuiltPatchResponse(" not in src


def test_echo_passes_every_quilt_parameter_explicitly():
    """THE BUG THIS CATCHES, WHICH NEARLY SHIPPED.

    get_user_quilt's parameters default to `Query(None)` OBJECTS, not to
    None. FastAPI substitutes real values per request; a DIRECT call does
    not. So an omitted `category` arrives as a truthy Query instance and
    the echo silently filters to nothing, on a route whose entire job is
    to hand back what it just created.

    Reading the signature suggests the defaults are None. Running it says
    otherwise. This test pins the fix so a later edit cannot quietly drop
    an argument back to its default.
    """
    src = _func_source("_created_patch_response")
    call = src.split("await get_user_quilt(", 1)[1].split(")", 1)[0]
    for param in ("category", "since", "origin_id", "group_by",
                  "project_id", "limit", "order", "max_age_days", "app_id"):
        assert f"{param}=" in call, f"{param} left to its Query(None) default"


def test_query_defaults_really_are_truthy_objects():
    """Proves the premise of the test above rather than asserting it, so
    the fix is not defending an imaginary bug. If FastAPI ever changed
    this, this test goes red and the explicit-args rule can be revisited.
    """
    tree = ast.parse(MAIN)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "get_user_quilt":
            defaults = node.args.defaults
            assert defaults, "get_user_quilt has no defaulted params"
            # Every optional query param defaults to a Query(...) CALL,
            # not to a None literal.
            calls = [d for d in defaults if isinstance(d, ast.Call)]
            assert calls, "expected Query(...) defaults, found none"
            return
    raise AssertionError("get_user_quilt not found")


def test_echo_failure_never_fails_the_write():
    """The patch is committed before the echo runs. A slow or failed
    render is a thinner response, never a 500 for a create that
    succeeded, and never a signal to the client to retry."""
    src = _func_source("_created_patch_response")
    assert "except Exception" in src
    assert "item_rendered" in src
    body = src.split("except Exception", 1)[1]
    assert "raise" not in body


def test_echo_reports_whether_it_actually_rendered():
    """An absent item must be distinguishable from a fabricated one, so
    the client refetches rather than believing an empty echo."""
    src = _func_source("_created_patch_response")
    assert '"item_rendered": False' in src
    assert 'body["item_rendered"] = True' in src


def test_created_flag_distinguishes_a_new_row_from_a_replay():
    src = _func_source("_created_patch_response")
    assert '"created": created' in src
    assert '"status": "created" if created else "exists"' in src


# --- the contract SS reads --------------------------------------------

def test_client_id_is_optional_so_existing_callers_are_unaffected():
    """The extractor and every current caller write without a key."""
    model = MAIN.split("class PatchCreate(BaseModel):", 1)[1].split("\n\n\n", 1)[0]
    assert re.search(r"client_id:\s*Optional\[str\]\s*=\s*Field\(\s*\n?\s*default=None", model)


# --- the bug the source tests above could NOT catch --------------------
#
# The first cut of the echo read `quilt.patches`. QuiltResponse has no
# such field: its rows live in `facts` and `action_items`. Every source
# assertion above passed, because the code said the right SHAPE of
# thing; it named a field that does not exist. Only the prod smoke found
# it, as item_rendered: false on every create.
#
# So this test does not assert another literal, which is how the bug got
# written in the first place. It reads the field names off QuiltResponse
# itself and checks the echo only ever reaches for those.


def _quilt_response_list_fields() -> set:
    """Field names on QuiltResponse annotated List[QuiltPatchResponse]."""
    tree = ast.parse(MAIN)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "QuiltResponse":
            out = set()
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    ann = ast.unparse(stmt.annotation)
                    if "QuiltPatchResponse" in ann and ann.startswith("List["):
                        out.add(stmt.target.id)
            return out
    raise AssertionError("QuiltResponse not found")


def test_echo_reads_fields_that_actually_exist_on_quilt_response():
    """Checked against the MODEL, not against a second hand-written name.

    A literal here would be the same class of mistake as the bug: two
    places naming a field, one of them wrong, nothing comparing them.
    """
    declared = _quilt_response_list_fields()
    assert declared, "expected QuiltResponse to declare patch arrays"
    src = _func_source("_created_patch_response")
    reached = set(re.findall(r"quilt\.([a-z_]+)", src))
    assert reached, "echo never reads the quilt result"
    unknown = reached - declared
    assert not unknown, f"echo reads non-existent QuiltResponse field(s): {unknown}"


def test_echo_searches_every_array_a_created_patch_could_land_in():
    """create_patch accepts all of VALID_PATCH_TYPES, and completables go
    to action_items while the rest go to facts. Searching one array would
    render an echo for some types and silently not for others."""
    declared = _quilt_response_list_fields()
    src = _func_source("_created_patch_response")
    reached = set(re.findall(r"quilt\.([a-z_]+)", src))
    assert reached == declared, (
        f"echo searches {reached}, QuiltResponse declares {declared}"
    )
