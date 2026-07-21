"""Unit tests for strip_prose_from_person_names.

Belt-and-suspenders sanitizer for the prompt rule "person value.text is
the NAME ONLY." The LLM occasionally stuffs a sentence into the name
field; this sanitizer truncates at the first natural prose boundary.

The corpus of bad shapes seen in prod (all from 2026-04-22 or earlier):
  - "Scott — collaborator on AI interview tool development and ..."
  - "Speaker 5, AI tool operator and technical interview practice partner"
  - "Speaker 2, colleague and interviewer with 15+ years of hiring ..."
  - "Ashby - customer success point of contact for ..."
  - "Redfern - technical lead and primary presenter"
  - "Yardley is a developer working for Morgan Stanley on ..."
  - "Speaker 5 is involved with Praise Angels, an initiative ..."
"""

from src.contextquilt.services.extraction_schema import (
    strip_prose_from_person_names,
)


def _patch(ptype: str, text: str) -> dict:
    return {"type": ptype, "value": {"text": text}, "connects_to": []}


class TestStripProseFromPersonNames:
    # ----- The seven observed bad shapes -----

    def test_em_dash_prose(self):
        content = {
            "patches": [_patch("person", "Scott — collaborator on AI interview tool development")]
        }
        strip_prose_from_person_names(content)
        assert content["patches"][0]["value"]["text"] == "Scott"

    def test_ascii_dash_prose(self):
        content = {
            "patches": [_patch("person", "Ashby - customer success point of contact")]
        }
        strip_prose_from_person_names(content)
        assert content["patches"][0]["value"]["text"] == "Ashby"

    def test_comma_prose(self):
        content = {
            "patches": [_patch("person", "Speaker 5, AI tool operator and partner")]
        }
        strip_prose_from_person_names(content)
        assert content["patches"][0]["value"]["text"] == "Speaker 5"

    def test_is_sentence(self):
        content = {
            "patches": [_patch("person", "Yardley is a developer working for Morgan Stanley.")]
        }
        strip_prose_from_person_names(content)
        assert content["patches"][0]["value"]["text"] == "Yardley"

    def test_was_sentence(self):
        content = {"patches": [_patch("person", "Thorne was the project lead")]}
        strip_prose_from_person_names(content)
        assert content["patches"][0]["value"]["text"] == "Thorne"

    def test_has_sentence(self):
        content = {"patches": [_patch("person", "Fenwick has 15 years of experience")]}
        strip_prose_from_person_names(content)
        assert content["patches"][0]["value"]["text"] == "Fenwick"

    def test_will_sentence(self):
        content = {"patches": [_patch("person", "Jaffer will coordinate with the team")]}
        strip_prose_from_person_names(content)
        assert content["patches"][0]["value"]["text"] == "Jaffer"

    def test_who_clause(self):
        content = {"patches": [_patch("person", "Arvind who joined last quarter")]}
        strip_prose_from_person_names(content)
        assert content["patches"][0]["value"]["text"] == "Arvind"

    # ----- Names that must survive untouched -----

    def test_single_name_unchanged(self):
        content = {"patches": [_patch("person", "Thorne")]}
        strip_prose_from_person_names(content)
        assert content["patches"][0]["value"]["text"] == "Thorne"

    def test_two_word_name_unchanged(self):
        content = {"patches": [_patch("person", "Bramble Martinez")]}
        strip_prose_from_person_names(content)
        assert content["patches"][0]["value"]["text"] == "Bramble Martinez"

    def test_three_word_name_unchanged(self):
        content = {"patches": [_patch("person", "Mary Jane Mayfield")]}
        strip_prose_from_person_names(content)
        assert content["patches"][0]["value"]["text"] == "Mary Jane Mayfield"

    def test_hyphenated_name_unchanged(self):
        """Jean-Luc has no spaces around the hyphen, so the separator
        " - " (space-dash-space) doesn't match."""
        content = {"patches": [_patch("person", "Jean-Luc Picard")]}
        strip_prose_from_person_names(content)
        assert content["patches"][0]["value"]["text"] == "Jean-Luc Picard"

    def test_apostrophe_name_unchanged(self):
        content = {"patches": [_patch("person", "O'Brien")]}
        strip_prose_from_person_names(content)
        assert content["patches"][0]["value"]["text"] == "O'Brien"

    def test_title_with_period_unchanged(self):
        content = {"patches": [_patch("person", "Dr. Mayfield")]}
        strip_prose_from_person_names(content)
        assert content["patches"][0]["value"]["text"] == "Dr. Mayfield"

    def test_compound_slash_unchanged(self):
        """Compound names (handled by split_compound_person_patches)
        shouldn't be touched here — no separator matches."""
        content = {"patches": [_patch("person", "Zephyra/Yardley")]}
        strip_prose_from_person_names(content)
        assert content["patches"][0]["value"]["text"] == "Zephyra/Yardley"

    def test_arvind_and_family_unchanged(self):
        """' and ' isn't in the separator list — preserves legitimate
        compound-form names that don't use slash."""
        content = {"patches": [_patch("person", "Arvind and Family")]}
        strip_prose_from_person_names(content)
        assert content["patches"][0]["value"]["text"] == "Arvind and Family"

    # ----- Other types must not be touched -----

    def test_does_not_touch_commitment(self):
        content = {
            "patches": [
                _patch(
                    "commitment",
                    "Thorne - draft requirements document by Friday",
                )
            ]
        }
        strip_prose_from_person_names(content)
        # Commitments routinely use dashes in their text; leave them alone.
        assert (
            content["patches"][0]["value"]["text"]
            == "Thorne - draft requirements document by Friday"
        )

    def test_does_not_touch_trait(self):
        content = {"patches": [_patch("trait", "Scott - prefers async communication")]}
        strip_prose_from_person_names(content)
        assert (
            content["patches"][0]["value"]["text"]
            == "Scott - prefers async communication"
        )

    # ----- Defensive edge cases -----

    def test_missing_value_dict_is_safe(self):
        content = {"patches": [{"type": "person", "value": None}]}
        strip_prose_from_person_names(content)  # must not raise
        assert content["patches"][0]["value"] is None

    def test_missing_text_is_safe(self):
        content = {"patches": [{"type": "person", "value": {}}]}
        strip_prose_from_person_names(content)  # must not raise

    def test_non_string_text_is_safe(self):
        content = {"patches": [{"type": "person", "value": {"text": 42}}]}
        strip_prose_from_person_names(content)
        assert content["patches"][0]["value"]["text"] == 42

    def test_empty_patches_list_is_safe(self):
        strip_prose_from_person_names({"patches": []})
        strip_prose_from_person_names({})

    def test_separator_at_start_skipped(self):
        """If the 'name' is just '- description', cleanup would yield an
        empty string. Sanitizer must leave the patch unchanged to avoid
        corrupting data we can't confidently fix."""
        content = {"patches": [_patch("person", "- some description")]}
        strip_prose_from_person_names(content)
        assert content["patches"][0]["value"]["text"] == "- some description"

    def test_earliest_separator_wins(self):
        """Bias toward the earliest match keeps multi-separator strings
        from being misread. 'X is a Y - the Z' should truncate at 'is'."""
        content = {
            "patches": [_patch("person", "Ashby is a customer success rep - point of contact")]
        }
        strip_prose_from_person_names(content)
        assert content["patches"][0]["value"]["text"] == "Ashby"
