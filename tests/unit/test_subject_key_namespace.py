"""Guard: subject keys survive per-app namespacing.

GP mints the CQ subject from its own user id, and SS and TR share a single
user row because Apple's `apple_sub` is per developer team rather than per
app. Two users are active in both apps today. So the app isolation decided
in doc 18 depends on GP namespacing the subject per app, and the namespaced
form will contain a colon: `user:tr:<uuid>` rather than `user:<uuid>`.

CQ never parses a user id, it only prefixes it, so the serving path is
already format-agnostic. The admin dashboard was not: one site took the
second colon-delimited segment and would have returned the NAMESPACE as the
user id, and two more used an unanchored `replace()` that strips every
occurrence of `user:` rather than the prefix.

These tests pin the invariant that makes any namespace safe: **`user:` is
the prefix, and everything after it is opaque and may contain colons.**
"""

import pathlib
import re

DASH = pathlib.Path(__file__).resolve().parents[2] / "src" / "dashboard" / "router.py"
SRC = pathlib.Path(__file__).resolve().parents[2] / "src"


def _strip_prefix(subject: str) -> str:
    """The rule the dashboard now uses, restated so it can be exercised."""
    return subject[5:] if subject.startswith("user:") else subject


def test_the_plain_form_is_unchanged():
    assert _strip_prefix("user:abc-123") == "abc-123"


def test_a_namespaced_subject_keeps_its_whole_id():
    """THE BUG. `split(':')[1]` returned "tr" here, silently attributing
    every row to a user id that is really an app namespace."""
    assert _strip_prefix("user:tr:abc-123") == "tr:abc-123"


def test_a_subject_with_no_prefix_passes_through():
    assert _strip_prefix("abc-123") == "abc-123"


def test_an_id_that_itself_contains_the_prefix_is_not_mangled():
    """Why anchoring matters: an unanchored replace of "user:" would strip
    the inner occurrence too and produce a different id."""
    assert _strip_prefix("user:tr:user-facing-test") == "tr:user-facing-test"


def test_dashboard_no_longer_splits_on_colon_for_a_user_id():
    text = DASH.read_text()
    assert "subject.split(':')[1]" not in text, (
        "taking the second colon-delimited segment returns the app namespace "
        "rather than the user id once subjects are namespaced"
    )


def test_dashboard_prefix_strips_are_anchored():
    """`replace(x, 'user:', '')` removes EVERY occurrence. Only an anchored
    strip is safe once the id after the prefix is opaque."""
    text = DASH.read_text()
    assert "replace(ps.subject_key, 'user:', '')" not in text
    assert "regexp_replace(ps.subject_key, '^user:', '')" in text


def test_nothing_in_the_serving_path_validates_a_user_id_as_a_uuid():
    """A namespaced id is not a UUID. If anything ever coerces one, the
    namespace breaks at the door rather than in the dashboard."""
    for path in (SRC / "main.py", SRC / "worker.py"):
        text = path.read_text()
        assert not re.search(r"UUID\(\s*user_id", text), f"{path.name} coerces user_id to UUID"


def test_subject_construction_only_ever_prefixes():
    """The invariant the whole scheme rests on: CQ builds `user:` + the id
    it was given and never takes it apart. If a site ever starts parsing
    the id, per-app namespacing stops being transparent."""
    text = (SRC / "worker.py").read_text()
    assert 'subject_key = f"user:{user_id}"' in text
    assert "user_id.split" not in text
