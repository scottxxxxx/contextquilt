"""Unit tests for multi-deliverable text-matching auto-parent.

Pre-fix behavior: when an extraction emitted >1 deliverable patch, the
auto-parent logic gave up on deliverable-level granularity and parented
all orphan episodes to the project. v1.2 adds per-orphan text matching
against each deliverable so a clear winner gets picked; ambiguous cases
still fall back to project (the safe default).
"""

from src.contextquilt.services.extraction_schema import (
    enforce_connection_requirements,
    _best_matching_deliverable,
    _content_words,
)


def _patch(ptype: str, text: str, connects_to=None) -> dict:
    return {
        "type": ptype,
        "value": {"text": text, "owner": None, "deadline": None},
        "connects_to": connects_to or [],
    }


def _parent_of(patch: dict) -> tuple[str, str] | None:
    """Return (target_type, target_text) of the parent connection, or None."""
    for c in patch.get("connects_to", []):
        if c.get("role") == "parent":
            return (c.get("target_type"), c.get("target_text"))
    return None


# ============================================================
# _content_words tokenizer
# ============================================================

class TestContentWords:
    def test_lowercases_and_filters_short_tokens(self):
        words = _content_words("Ship the SDK Patch")
        assert words == {"ship", "sdk", "patch"}  # "the" stopwordred, no <3 char tokens

    def test_filters_stopwords(self):
        # Common glue words drop; content stays.
        words = _content_words("Deploy the patch to QA after testing")
        assert "deploy" in words
        assert "patch" in words
        assert "testing" in words
        assert "the" not in words
        assert "after" not in words

    def test_punctuation_split(self):
        words = _content_words("agent-routing payload, simplified")
        assert "agent" in words
        assert "routing" in words
        assert "payload" in words
        assert "simplified" in words

    def test_empty_input(self):
        assert _content_words("") == set()
        assert _content_words(None) == set()


# ============================================================
# _best_matching_deliverable
# ============================================================

class TestBestMatchingDeliverable:
    def test_clear_winner_returned(self):
        deliv = ["API rewrite for agent routing", "Mobile launch for iOS"]
        match = _best_matching_deliverable(
            "Apply patch to agent routing API endpoint",
            deliv,
        )
        assert match == "API rewrite for agent routing"

    def test_returns_none_on_tie(self):
        # Both deliverables share exactly 2 content words with the orphan;
        # no clear winner → fall back to project.
        deliv = ["API rewrite project", "API rewrite roadmap"]
        match = _best_matching_deliverable("Schedule API rewrite review", deliv)
        # Both share {"api", "rewrite"} = 2 words each → tie → None.
        assert match is None

    def test_returns_none_when_best_below_threshold(self):
        # Single shared word is too thin a signal — fall back.
        deliv = ["API rewrite", "Mobile launch"]
        match = _best_matching_deliverable("Schedule the API meeting", deliv)
        # Only "api" overlaps → score 1 → below 2-word threshold.
        assert match is None

    def test_returns_none_with_single_deliverable(self):
        # Helper is only used in the multi-deliverable case.
        assert _best_matching_deliverable("anything", ["solo deliverable"]) is None
        assert _best_matching_deliverable("anything", []) is None

    def test_returns_none_for_empty_orphan(self):
        deliv = ["First deliverable", "Second deliverable"]
        assert _best_matching_deliverable("", deliv) is None

    def test_runner_up_close_but_not_tied(self):
        # Best=3, runner-up=2 → strict greater, valid winner.
        deliv = [
            "API rewrite for agent routing payload",
            "API roadmap planning",
        ]
        match = _best_matching_deliverable(
            "Update agent routing payload format",
            deliv,
        )
        assert match == "API rewrite for agent routing payload"


# ============================================================
# enforce_connection_requirements multi-deliverable path
# ============================================================

class TestMultiDeliverableAutoParent:
    def test_two_deliverables_clear_match_picks_specific(self):
        content = {
            "patches": [
                _patch("project", "Q2 Engagement"),
                _patch("deliverable", "API rewrite for agent routing"),
                _patch("deliverable", "Mobile launch for iOS"),
                _patch("commitment", "Apply patch to agent routing API"),
            ]
        }
        enforce_connection_requirements(content, meeting_project="Q2 Engagement")

        commit = next(p for p in content["patches"] if p["type"] == "commitment")
        assert _parent_of(commit) == ("deliverable", "API rewrite for agent routing")

    def test_two_deliverables_ambiguous_falls_back_to_project(self):
        # Generic episode with no clear text affinity to either deliverable.
        content = {
            "patches": [
                _patch("project", "Q2 Engagement"),
                _patch("deliverable", "API rewrite"),
                _patch("deliverable", "Mobile launch"),
                _patch("commitment", "Schedule weekly sync"),
            ]
        }
        enforce_connection_requirements(content, meeting_project="Q2 Engagement")

        commit = next(p for p in content["patches"] if p["type"] == "commitment")
        assert _parent_of(commit) == ("project", "Q2 Engagement")

    def test_single_deliverable_unchanged_behavior(self):
        # Pre-existing single-deliverable case keeps working: orphan parents
        # to the deliverable directly, no text matching needed.
        content = {
            "patches": [
                _patch("project", "Q2 Engagement"),
                _patch("deliverable", "API rewrite"),
                _patch("commitment", "Schedule the design review"),
            ]
        }
        enforce_connection_requirements(content, meeting_project="Q2 Engagement")

        commit = next(p for p in content["patches"] if p["type"] == "commitment")
        assert _parent_of(commit) == ("deliverable", "API rewrite")

    def test_zero_deliverables_falls_back_to_project(self):
        content = {
            "patches": [
                _patch("project", "Q2 Engagement"),
                _patch("commitment", "Apply the patch"),
            ]
        }
        enforce_connection_requirements(content, meeting_project="Q2 Engagement")

        commit = next(p for p in content["patches"] if p["type"] == "commitment")
        assert _parent_of(commit) == ("project", "Q2 Engagement")

    def test_non_deliverable_child_type_skips_matching(self):
        # role / goal / constraint / deliverable itself stay project-parented
        # regardless of how many deliverables are in scope.
        content = {
            "patches": [
                _patch("project", "Q2 Engagement"),
                _patch("deliverable", "API rewrite for agent routing"),
                _patch("deliverable", "Mobile launch for iOS"),
                _patch("role", "API rewrite owner"),  # mentions "API rewrite" but role isn't a deliv child
            ]
        }
        enforce_connection_requirements(content, meeting_project="Q2 Engagement")

        role = next(p for p in content["patches"] if p["type"] == "role")
        assert _parent_of(role) == ("project", "Q2 Engagement")

    def test_orphan_with_existing_parent_unchanged(self):
        # If the LLM already wired a valid parent, don't override.
        content = {
            "patches": [
                _patch("project", "Q2 Engagement"),
                _patch("deliverable", "API rewrite for agent routing"),
                _patch("deliverable", "Mobile launch for iOS"),
                _patch(
                    "commitment",
                    "Apply patch to agent routing API",
                    connects_to=[
                        {
                            "role": "parent",
                            "label": "belongs_to",
                            "target_type": "deliverable",
                            "target_text": "Mobile launch for iOS",  # LLM's choice, even if "wrong"
                        }
                    ],
                ),
            ]
        }
        enforce_connection_requirements(content, meeting_project="Q2 Engagement")

        commit = next(p for p in content["patches"] if p["type"] == "commitment")
        assert _parent_of(commit) == ("deliverable", "Mobile launch for iOS")

    def test_three_deliverables_picks_best(self):
        content = {
            "patches": [
                _patch("project", "Q2 Engagement"),
                _patch("deliverable", "Authentication system rewrite"),
                _patch("deliverable", "Payment gateway integration"),
                _patch("deliverable", "Notification service launch"),
                _patch("commitment", "Add OAuth flow to authentication system"),
            ]
        }
        enforce_connection_requirements(content, meeting_project="Q2 Engagement")

        commit = next(p for p in content["patches"] if p["type"] == "commitment")
        assert _parent_of(commit) == ("deliverable", "Authentication system rewrite")
