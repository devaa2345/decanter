"""
Unit tests for the matching pipeline.

Covers:
- Exact keyword/substring matching (Layer 1)
- Fuzzy matching with typos (Layer 2)
- Ambiguous multi-perfume messages
- Unrelated/empty messages
- Case insensitivity
- Layer 3 candidate shortlisting (see TestTopCandidatesForLLM below —
  this is the regression guard for a real production bug: the old code
  handed Groq the entire 1200+ catalog on every call, running to ~23.5K
  tokens against a 6000 TPM limit and failing every single time)
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.catalog import PERFUMES
from app.matcher import (
    MatchResult,
    _build_ngram_candidates,
    _layer1_exact_match,
    _layer2_fuzzy_match,
    _layer3_llm_match,
    _top_candidates_for_llm,
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


class TestBuildNgramCandidates:
    """Shared phrase-extraction used by both Layer 2 and Layer 3 shortlisting."""

    def test_single_word(self):
        assert _build_ngram_candidates("sauvage") == ["sauvage"]

    def test_words_plus_bigrams_and_trigrams(self):
        result = _build_ngram_candidates("club de nuit")
        assert "club" in result
        assert "de" in result
        assert "nuit" in result
        assert "club de" in result
        assert "de nuit" in result
        assert "club de nuit" in result

    def test_empty_string(self):
        assert _build_ngram_candidates("") == []


class TestTopCandidatesForLLM:
    """
    Regression guard for the production bug: Groq's system prompt must
    stay a small shortlist, never the full catalog.
    """

    def test_never_exceeds_the_limit(self):
        shortlist = _top_candidates_for_llm("suvage 10ml", limit=25)
        assert len(shortlist) <= 25
        # The real bug: with 1200+ perfumes, an unbounded shortlist would
        # be an order of magnitude larger than this.
        assert len(shortlist) < len(PERFUMES)

    def test_default_limit_is_well_under_full_catalog_size(self):
        shortlist = _top_candidates_for_llm("suvage 10ml")
        assert len(shortlist) <= 25
        assert len(PERFUMES) > 1000  # sanity: this project's catalog is large

    def test_relevant_perfume_appears_in_shortlist_for_a_near_miss(self):
        """A message close enough to fail Layers 1/2 but clearly about a
        real perfume should still surface that perfume in the shortlist for
        the LLM to consider."""
        shortlist = _top_candidates_for_llm("suvage 10ml")
        matched_names = " ".join(
            data["display_name"].lower() for data in shortlist.values()
        )
        assert "sauvage" in matched_names

    def test_short_message_yields_empty_or_tiny_shortlist(self):
        """'hi' has no candidate substrings >= 4 chars — nothing to score,
        so Layer 3 has nothing plausible to offer and shouldn't invent one."""
        shortlist = _top_candidates_for_llm("hi")
        assert shortlist == {}

    def test_empty_message(self):
        assert _top_candidates_for_llm("") == {}

    def test_returned_entries_are_real_catalog_data(self):
        shortlist = _top_candidates_for_llm("suvage 10ml")
        for pid, data in shortlist.items():
            assert PERFUMES[pid] is data


class TestLayer3LLMMatch:
    def test_empty_shortlist_skips_the_llm_call_entirely(self):
        """No point calling Groq (or even building a prompt) if there's
        nothing plausible to offer it."""
        with patch("app.groq_client.classify_perfume", new_callable=AsyncMock) as mock_classify:
            result = asyncio.run(_layer3_llm_match("hi"))
        mock_classify.assert_not_called()
        assert result.perfume_id is None

    def test_calls_classify_perfume_with_a_bounded_candidates_dict(self):
        """End-to-end wiring check: Layer 3 must pass a shortlist, not the
        full PERFUMES dict, to the Groq client."""
        with patch(
            "app.groq_client.classify_perfume", new_callable=AsyncMock, return_value=None
        ) as mock_classify:
            asyncio.run(_layer3_llm_match("suvage 10ml"))

        mock_classify.assert_called_once()
        _, kwargs = mock_classify.call_args
        assert len(kwargs["candidates"]) <= 25
        assert len(kwargs["candidates"]) < len(PERFUMES)

    def test_matched_result_shape(self):
        # Returns whatever candidate it was actually offered, rather than a
        # hardcoded pid — classify_perfume can only ever return something
        # from the shortlist it's given, so a fixed unrelated pid would
        # (correctly) get rejected by the `result in candidates` check.
        async def fake_classify(message, candidates):
            return next(iter(candidates))

        with patch(
            "app.groq_client.classify_perfume",
            new_callable=AsyncMock,
            side_effect=fake_classify,
        ):
            result = asyncio.run(_layer3_llm_match("suvage 10ml"))
        assert result.perfume_id is not None
        assert result.layer == "llm"
        assert result.confidence == 80.0

    def test_exception_returns_empty_result_not_raised(self):
        with patch(
            "app.groq_client.classify_perfume",
            new_callable=AsyncMock,
            side_effect=RuntimeError("groq down"),
        ):
            result = asyncio.run(_layer3_llm_match("suvage 10ml"))
        assert result.perfume_id is None
