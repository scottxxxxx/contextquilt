"""Unit tests for strip_owner_on_self_typed_patches.

Belt-and-suspenders sanitizer that enforces "owner must be null on
trait/preference/goal/constraint" — the prompt instructs the model
to do this, but Haiku 4.5 occasionally reintroduces third-person
framing by setting owner='Scott' on these self-typed patches.
"""

from src.contextquilt.services.extraction_schema import (
    strip_owner_on_self_typed_patches,
)


def _patch(ptype: str, text: str, owner=None) -> dict:
    return {"type": ptype, "value": {"text": text, "owner": owner}, "connects_to": []}


class TestStripOwnerOnSelfTypedPatches:
    def test_clears_owner_on_trait(self):
        content = {"patches": [_patch("trait", "You prefer async", owner="Scott")]}
        strip_owner_on_self_typed_patches(content)
        assert content["patches"][0]["value"]["owner"] is None

    def test_clears_owner_on_preference(self):
        content = {"patches": [_patch("preference", "You prefer Slack to email", owner="Scott")]}
        strip_owner_on_self_typed_patches(content)
        assert content["patches"][0]["value"]["owner"] is None

    def test_clears_owner_on_goal(self):
        content = {"patches": [_patch("goal", "You aim to ship by Q2", owner="Scott")]}
        strip_owner_on_self_typed_patches(content)
        assert content["patches"][0]["value"]["owner"] is None

    def test_clears_owner_on_constraint(self):
        content = {"patches": [_patch("constraint", "You can't deploy on Fridays", owner="Scott")]}
        strip_owner_on_self_typed_patches(content)
        assert content["patches"][0]["value"]["owner"] is None

    def test_preserves_owner_on_commitment(self):
        """Action items legitimately need owner — this sanitizer must not touch them."""
        content = {"patches": [_patch("commitment", "Ship the SDK patch", owner="Thorne")]}
        strip_owner_on_self_typed_patches(content)
        assert content["patches"][0]["value"]["owner"] == "Thorne"

    def test_preserves_owner_on_blocker(self):
        content = {"patches": [_patch("blocker", "API not exposed", owner="Merrick")]}
        strip_owner_on_self_typed_patches(content)
        assert content["patches"][0]["value"]["owner"] == "Merrick"

    def test_preserves_owner_on_decision(self):
        content = {"patches": [_patch("decision", "Use agent ID for routing", owner="Zephyra")]}
        strip_owner_on_self_typed_patches(content)
        assert content["patches"][0]["value"]["owner"] == "Zephyra"

    def test_already_null_owner_is_no_op(self):
        content = {"patches": [_patch("trait", "You're a pragmatic problem-solver", owner=None)]}
        strip_owner_on_self_typed_patches(content)
        assert content["patches"][0]["value"]["owner"] is None

    def test_empty_string_owner_left_alone(self):
        """Empty string is falsy; the sanitizer skips it. Worth pinning so we
        don't silently turn '' into None and shift downstream behavior."""
        content = {"patches": [_patch("trait", "x", owner="")]}
        strip_owner_on_self_typed_patches(content)
        assert content["patches"][0]["value"]["owner"] == ""

    def test_mixed_patches_only_self_typed_cleared(self):
        content = {
            "patches": [
                _patch("trait", "You prefer async", owner="Scott"),
                _patch("commitment", "Ship the SDK", owner="Thorne"),
                _patch("preference", "You prefer Slack", owner="Scott"),
                _patch("blocker", "API down", owner="Merrick"),
            ]
        }
        strip_owner_on_self_typed_patches(content)
        owners = [p["value"]["owner"] for p in content["patches"]]
        assert owners == [None, "Thorne", None, "Merrick"]

    def test_empty_patches_array(self):
        content = {"patches": []}
        strip_owner_on_self_typed_patches(content)
        assert content == {"patches": []}

    def test_missing_patches_key(self):
        content = {}
        strip_owner_on_self_typed_patches(content)
        assert content == {}

    def test_idempotent(self):
        content = {"patches": [_patch("trait", "You prefer async", owner="Scott")]}
        strip_owner_on_self_typed_patches(content)
        first = content["patches"][0]["value"]["owner"]
        strip_owner_on_self_typed_patches(content)
        assert content["patches"][0]["value"]["owner"] == first  # still None
