"""Instrumentation tests for strip_owner_on_self_typed_patches.

The sanitizer nulls `value.owner` on trait/preference/goal/constraint,
because the (you) speaker is the implicit owner and the model sometimes
names them explicitly. Right intent, too broad in effect: it also deletes
a genuine third-party attribution ("Brightwell prefers to avoid continuous
upgrades"), which the manifest wants as a `held_by` edge.

These tests pin the instrumentation added to measure how often that
happens. Behavior is unchanged on purpose: everything is still stripped.
The point is that the strip destroys the evidence, so stored rows cannot
tell us whether the model still attempts third-party attribution or
stopped, and the fix should be built against live frequency rather than
an inference from rows written before the rule existed.

Fixtures mirror the twelve legacy rows found on prod 2026-08-04: seven
self-attributions, two placeholders, one corrupt, two genuine.
"""

from src.contextquilt.services.extraction_schema import (
    strip_owner_on_self_typed_patches,
)


def _p(ptype: str, text: str, owner):
    return {"type": ptype, "value": {"text": text, "owner": owner}}


def _report(content):
    return content.get("_self_typed_owner_stripped")


class TestBehaviourIsUnchanged:
    """The whole point of shipping this separately: nothing moves yet."""

    def test_self_owner_still_stripped(self):
        c = {"patches": [_p("preference", "prefers async comms", "Scott")]}
        strip_owner_on_self_typed_patches(c, user_label="Scott")
        assert c["patches"][0]["value"]["owner"] is None

    def test_third_party_owner_still_stripped(self):
        """We are measuring the loss, not stopping it. Yet."""
        c = {"patches": [_p("preference", "Brightwell prefers fewer upgrades", "Brightwell")]}
        strip_owner_on_self_typed_patches(c, user_label="Scott")
        assert c["patches"][0]["value"]["owner"] is None

    def test_all_four_self_typed_types_covered(self):
        for t in ("trait", "preference", "goal", "constraint"):
            c = {"patches": [_p(t, "something", "Brightwell")]}
            strip_owner_on_self_typed_patches(c, user_label="Scott")
            assert c["patches"][0]["value"]["owner"] is None, t

    def test_action_types_untouched(self):
        """commitment/blocker keep their owner. That is the whole model."""
        c = {"patches": [_p("commitment", "ship it", "Brightwell")]}
        strip_owner_on_self_typed_patches(c, user_label="Scott")
        assert c["patches"][0]["value"]["owner"] == "Brightwell"
        assert _report(c) is None


class TestClassification:
    def test_the_user_counts_as_self(self):
        c = {"patches": [_p("preference", "prefers async comms", "Scott")]}
        strip_owner_on_self_typed_patches(c, user_label="Scott")
        r = _report(c)
        assert (r["self_or_placeholder"], r["third_party"]) == (1, 0)

    def test_placeholder_counts_as_self(self):
        c = {"patches": [_p("preference", "prefers volunteering", "Speaker 2")]}
        strip_owner_on_self_typed_patches(c, user_label="Scott")
        r = _report(c)
        assert (r["self_or_placeholder"], r["third_party"]) == (1, 0)

    def test_real_third_party_is_flagged(self):
        c = {"patches": [_p("preference", "Brightwell prefers fewer upgrades", "Brightwell")]}
        strip_owner_on_self_typed_patches(c, user_label="Scott")
        r = _report(c)
        assert (r["self_or_placeholder"], r["third_party"]) == (0, 1)
        assert r["third_party_detail"][0]["owner"] == "Brightwell"
        assert r["third_party_detail"][0]["type"] == "preference"

    def test_without_user_label_the_user_looks_like_a_third_party(self):
        """Known limitation, asserted so it cannot surprise anyone.

        The classifier needs the (you) label to tell the user apart from a
        third party. Callers that cannot supply it will over-count, so the
        worker passes it.
        """
        c = {"patches": [_p("preference", "prefers async comms", "Scott")]}
        strip_owner_on_self_typed_patches(c)
        assert _report(c)["third_party"] == 1

    def test_the_prod_mix_reproduces(self):
        """Seven self, two placeholder, one corrupt, two genuine."""
        patches = (
            [_p("preference", f"pref {i}", "Scott") for i in range(7)]
            + [_p("preference", "prefers volunteering", "Speaker 2"),
               _p("preference", "clear separation", "Speaker 5")]
            + [_p("preference", "You prefer to close integration work", "Fenwyck")]
            + [_p("preference", "Brightwell prefers fewer upgrades", "Brightwell"),
               _p("preference", "Brightwell prefers stability", "Brightwell")]
        )
        c = {"patches": patches}
        strip_owner_on_self_typed_patches(c, user_label="Scott")
        r = _report(c)
        # The corrupt one classifies as third party: 'Fenwyck' is a real
        # name even though the text says "You". No rule saves that row,
        # and it should show up in the sample rather than be silently
        # binned as self.
        assert (r["self_or_placeholder"], r["third_party"]) == (9, 3)
        assert all(p["value"]["owner"] is None for p in c["patches"])


class TestQuietWhenNothingHappens:
    def test_no_report_when_no_owners_present(self):
        c = {"patches": [_p("preference", "prefers async comms", None)]}
        strip_owner_on_self_typed_patches(c, user_label="Scott")
        assert _report(c) is None

    def test_no_report_on_empty_content(self):
        assert _report(strip_owner_on_self_typed_patches({})) is None

    def test_malformed_patches_do_not_raise(self):
        c = {"patches": [None, "junk", {"type": "preference"},
                         {"type": "preference", "value": "not a dict"}]}
        strip_owner_on_self_typed_patches(c, user_label="Scott")
        assert _report(c) is None

    def test_detail_text_is_truncated(self):
        c = {"patches": [_p("preference", "x" * 500, "Brightwell")]}
        strip_owner_on_self_typed_patches(c, user_label="Scott")
        assert len(_report(c)["third_party_detail"][0]["text"]) == 80
