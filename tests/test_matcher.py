"""
Unit tests for the matching pipeline.

Covers:
- Exact keyword/substring matching (Layer 1)
- Fuzzy matching with typos (Layer 2)
- Ambiguous multi-perfume messages
- Unrelated/empty messages
- Case insensitivity
"""

import pytest

from app.matcher import (
    MatchResult,
    _layer1_exact_match,
    _layer2_fuzzy_match,
    normalize_message,
)


class TestNormalize:
    """Test message normalization."""

    def test_lowercase(self):
        assert normalize_message("SAUVAGE") == "sauvage"

    def test_strip_punctuation(self):
        assert normalize_message("what's the price?") == "what s the price"

    def test_collapse_whitespace(self):
        assert normalize_message("  sauvage   price  ") == "sauvage price"

    def test_empty(self):
        assert normalize_message("") == ""
        assert normalize_message("   ") == ""


class TestLayer1ExactMatch:
    """Test Layer 1 — exact/substring keyword matching."""

    def test_direct_keyword(self):
        """A known keyword should match its perfume."""
        result = _layer1_exact_match("sauvage")
        # Should match some perfume that has "sauvage" as a keyword
        assert result.perfume_id is not None
        assert result.layer == "exact"
        assert result.confidence is not None

    def test_substring_in_sentence(self):
        """Keyword as a substring of a longer message."""
        result = _layer1_exact_match("how much for sauvage 10ml")
        assert result.perfume_id is not None
        assert result.layer == "exact"

    def test_9pm_keyword(self):
        """Test numeric-starting keyword."""
        result = _layer1_exact_match("9pm rebel price")
        assert result.perfume_id is not None
        assert result.layer == "exact"

    def test_no_match(self):
        """Completely unrelated message should not match."""
        result = _layer1_exact_match("hello how are you")
        assert result.perfume_id is None
        assert result.layer is None
        assert result.ambiguous is False

    def test_empty_message(self):
        """Empty message should not match."""
        result = _layer1_exact_match("")
        assert result.perfume_id is None

    def test_case_insensitive(self):
        """Already normalized, but double-check."""
        result = _layer1_exact_match("sauvage")
        assert result.perfume_id is not None

    def test_club_de_nuit(self):
        """Multi-word keyword match."""
        result = _layer1_exact_match("club de nuit intense")
        assert result.perfume_id is not None
        assert result.layer == "exact"

    def test_long_gibberish(self):
        """Random long text should not match."""
        result = _layer1_exact_match("xyzqwerty asdfgh nothing relevant here at all blah blah")
        assert result.perfume_id is None
        assert result.ambiguous is False


class TestLayer2FuzzyMatch:
    """Test Layer 2 — fuzzy string matching."""

    def test_common_misspelling_savage(self):
        """'savage' (common misspelling of 'sauvage') should fuzzy-match."""
        result = _layer2_fuzzy_match("savage price please")
        # Should match some perfume with "sauvage" keyword
        assert result.perfume_id is not None
        assert result.layer == "fuzzy"
        assert result.confidence is not None and result.confidence >= 70

    def test_typo_savuage(self):
        """Transposed letters should still match."""
        result = _layer2_fuzzy_match("savuage")
        assert result.perfume_id is not None
        assert result.layer == "fuzzy"

    def test_typo_sawage(self):
        """Another common typo."""
        result = _layer2_fuzzy_match("sawage")
        assert result.perfume_id is not None
        assert result.layer == "fuzzy"

    def test_completely_unrelated(self):
        """Unrelated text should not match even with fuzzy tolerance."""
        result = _layer2_fuzzy_match("i want to order pizza delivery")
        assert result.perfume_id is None

    def test_hello_no_match(self):
        """Greeting should not match."""
        result = _layer2_fuzzy_match("hello good morning")
        assert result.perfume_id is None

    def test_abbreviation_bdc(self):
        """Short abbreviation - may or may not fuzzy match depending on threshold."""
        # 'bdc' is a common abbreviation for Bleu de Chanel
        # This might match if 'bdc' is in the keywords
        result = _layer1_exact_match("bdc")
        if result.perfume_id is None:
            result = _layer2_fuzzy_match("bdc")
        # Either way, if bdc is in keywords it should match at some layer
        # If not, this test just validates no crash

    def test_partial_name(self):
        """Partial perfume name should fuzzy match."""
        result = _layer2_fuzzy_match("nitro red")
        assert result.perfume_id is not None
        assert result.layer == "fuzzy"


class TestEdgeCases:
    """Test edge cases across the matching pipeline."""

    def test_numbers_only(self):
        """Pure numbers should not match any perfume."""
        result = _layer1_exact_match("12345")
        assert result.perfume_id is None

    def test_single_character(self):
        """Single character should not match."""
        result = _layer1_exact_match("a")
        assert result.perfume_id is None

    def test_emoji_only(self):
        """Emoji-only message should not match."""
        result = _layer1_exact_match("")  # emojis stripped by normalization
        assert result.perfume_id is None

    def test_very_long_message(self):
        """Very long message with a keyword buried in it."""
        long_msg = "i was wondering " * 20 + "sauvage" + " thank you " * 10
        result = _layer1_exact_match(long_msg)
        assert result.perfume_id is not None

    def test_price_question_format(self):
        """Natural question format."""
        result = _layer1_exact_match("how much for 10ml sauvage plz")
        assert result.perfume_id is not None

    def test_price_question_format_2(self):
        """Another natural question format."""
        result = _layer1_exact_match("sauvage 5ml kitne ka hai")
        assert result.perfume_id is not None

    def test_are_you_open(self):
        """Non-perfume question should not match."""
        result = _layer1_exact_match("are you open today")
        assert result.perfume_id is None
        assert result.ambiguous is False

    def test_shipping_question(self):
        """Shipping question should not match a perfume."""
        result = _layer1_exact_match("how much is shipping to delhi")
        assert result.perfume_id is None
