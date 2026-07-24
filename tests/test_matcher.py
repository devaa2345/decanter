"""
Unit tests for the matching pipeline.

Covers:
- Exact keyword/substring matching (fallback, was "Layer 1")
- Fuzzy matching with typos (fallback, was "Layer 2")
- Ambiguous multi-perfume messages
- Unrelated/empty messages
- Case insensitivity
- Primary-LLM candidate shortlisting (see TestTopCandidatesForLLM below —
  this is the regression guard for a real production bug: the old code
  handed Groq the entire 1200+ catalog on every call, running to ~23.5K
  tokens against a 6000 TPM limit and failing every single time)
- Groq-first pipeline ordering with fallback to the free matchers (see
  TestMatchPerfumeGroqFirstWithFallback) — Groq is now tried FIRST for
  every message, not as a last resort, but the bot must never go fully
  silent on a Groq outage/rate-limit/low-confidence response.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.catalog import PERFUMES
from app.groq_client import GroqClassification
from app.matcher import (
    MatchResult,
    _build_ngram_candidates,
    _keyword_boundary_regex,
    _layer1_exact_match,
    _layer2_fuzzy_match,
    _looks_like_explicit_request,
    _primary_llm_match,
    _top_candidates_for_llm,
    has_confident_keyword_match,
    match_perfume,
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


class TestWordBoundaryMatching:
    """
    Regression guard for a real production bug: Layer 1 used raw substring
    containment (`if keyword in normalized`), so a keyword could match
    *inside* an unrelated word. Confirmed concretely: the typo "fregrance"
    contains "egra", a real keyword for an unrelated perfume ("Rasasi
    Egra"), and matched it. Word-boundary matching (`\\b...\\b`) fixes this
    without weakening genuine standalone-word matches.
    """

    def test_keyword_embedded_in_unrelated_word_does_not_match(self):
        result = _layer1_exact_match("9pm fregrance")
        assert result.perfume_id != "rasasiegra"

    def test_keyword_as_a_genuine_standalone_word_still_matches(self):
        """The fix must not regress real matches — "sauvage" as an actual
        word in the message still has to match exactly as before."""
        result = _layer1_exact_match("how much for sauvage 10ml")
        assert result.perfume_id is not None
        assert result.layer == "exact"

    def test_multiword_keyword_still_matches(self):
        result = _layer1_exact_match("club de nuit intense man")
        assert result.perfume_id is not None


class TestNinePMFamilyAmbiguity:
    """
    "9pm" alone is genuinely ambiguous among 4 different Afnan products
    (Rebel, Night Out, Elixir, and the base "Afnan 9PM") — confirmed in
    production ("9pm fragrance" got no match at all, since none of the four
    has a bare "9pm" keyword by design). A bare mention should be flagged
    ambiguous, not silently guess one (risking the wrong price quoted), but
    mentioning the actual distinguishing word must still resolve precisely.
    """

    def test_bare_9pm_is_ambiguous(self):
        result = _layer1_exact_match("9pm")
        assert result.ambiguous is True
        assert result.perfume_id is None

    def test_9pm_with_space_variant_is_ambiguous(self):
        result = _layer1_exact_match("9 pm")
        assert result.ambiguous is True

    def test_9pm_with_filler_word_is_still_ambiguous(self):
        result = _layer1_exact_match("9pm fragrance")
        assert result.ambiguous is True

    def test_9pm_rebel_resolves_precisely(self):
        result = _layer1_exact_match("9pm rebel")
        assert result.ambiguous is False
        assert result.perfume_id == "afnan9pm_rebel"

    def test_9pm_night_out_resolves_precisely(self):
        result = _layer1_exact_match("9pm night out")
        assert result.ambiguous is False
        assert result.perfume_id == "afnan9pm_night_out"

    def test_9pm_elixir_resolves_precisely(self):
        result = _layer1_exact_match("9pm elixir")
        assert result.ambiguous is False
        assert result.perfume_id == "afnan9pm_elixir_parfum"

    def test_afnan_9pm_resolves_to_the_base_product(self):
        result = _layer1_exact_match("afnan 9pm")
        assert result.ambiguous is False
        assert result.perfume_id == "afnanafnan_9pm"

    def test_unrelated_9_number_does_not_trigger_ambiguity(self):
        """Sanity check: this isn't just "any message containing digit 9" —
        it's specifically the "9pm" term."""
        result = _layer1_exact_match("i need 9 bottles")
        assert result.ambiguous is False

    def test_bare_9pm_lists_all_four_real_candidates(self):
        """The reply needs actual candidates to list, not just a bare
        ambiguous flag — this is what lets the bot show a real comparison
        card instead of a content-free "which one?" message."""
        result = _layer1_exact_match("9pm")
        assert result.matched_perfume_ids is not None
        assert set(result.matched_perfume_ids) == {
            "afnan9pm_rebel",
            "afnanafnan_9pm",
            "afnan9pm_night_out",
            "afnan9pm_elixir_parfum",
        }


class TestAmbiguousKeywordCollisionCandidates:
    """
    When 2+ different keyword strings each match a different perfume, that
    means the customer named multiple distinct products (e.g. "sauvage and
    eros price") — _layer1_exact_match must return every one of them via
    matched_perfume_ids so the reply can show a full card for each, rather
    than silently keeping only the longest-keyword match and dropping the
    rest (the real "shows one random perfume's detail" production bug) or
    treating it as a vague "which one?" ambiguity. Uses a controlled
    synthetic catalog (patched in) rather than hunting for a fragile
    real-world example, since the exact catalog contents can change on
    re-upload.
    """

    def test_tied_length_different_keywords_lists_both_candidates(self):
        # "crimson" and "emerald" are deliberately the same length (7 chars)
        # — confirms the tied-length case also returns both, not just the
        # unequal-length case covered below.
        synthetic = {
            "brandone_crimson": {
                "display_name": "BrandOne Crimson",
                "keywords": ["crimson"],
                "prices": {"3ml": 100},
                "clone_of": None,
            },
            "brandtwo_emerald": {
                "display_name": "BrandTwo Emerald",
                "keywords": ["emerald"],
                "prices": {"3ml": 100},
                "clone_of": None,
            },
        }
        with patch("app.matcher.PERFUMES", synthetic):
            _keyword_boundary_regex.cache_clear()
            result = _layer1_exact_match("crimson emerald combo")
            _keyword_boundary_regex.cache_clear()  # don't leak into other tests

        assert result.ambiguous is True
        assert set(result.matched_perfume_ids) == {"brandone_crimson", "brandtwo_emerald"}

    def test_unequal_length_different_keywords_still_lists_both(self):
        """Regression guard for the actual reported production bug: the old
        code picked ONLY the longer/more-specific keyword match and
        silently dropped the other whenever the two matched keywords had
        different lengths — so "sauvage and bleu de chanel" would keep
        only "bleu de chanel" (14 chars) and drop "sauvage" (7 chars)
        entirely. Different keyword strings for different products must
        list both regardless of length difference."""
        synthetic = {
            "brandone_sauvage": {
                "display_name": "BrandOne Sauvage",
                "keywords": ["sauvage"],
                "prices": {"3ml": 100},
                "clone_of": None,
            },
            "brandtwo_bleudechanel": {
                "display_name": "BrandTwo Bleu De Chanel",
                "keywords": ["bleu de chanel"],
                "prices": {"3ml": 100},
                "clone_of": None,
            },
        }
        with patch("app.matcher.PERFUMES", synthetic):
            _keyword_boundary_regex.cache_clear()
            result = _layer1_exact_match("sauvage 10ml and bleu de chanel 5ml please")
            _keyword_boundary_regex.cache_clear()

        assert result.perfume_id is None
        assert set(result.matched_perfume_ids) == {"brandone_sauvage", "brandtwo_bleudechanel"}

    def test_three_distinct_perfumes_all_listed(self):
        synthetic = {
            "brandone_crimson": {
                "display_name": "BrandOne Crimson",
                "keywords": ["crimson"],
                "prices": {"3ml": 100},
                "clone_of": None,
            },
            "brandtwo_emerald": {
                "display_name": "BrandTwo Emerald",
                "keywords": ["emerald"],
                "prices": {"3ml": 100},
                "clone_of": None,
            },
            "brandthree_topaz": {
                "display_name": "BrandThree Topaz",
                "keywords": ["topaz"],
                "prices": {"3ml": 100},
                "clone_of": None,
            },
        }
        with patch("app.matcher.PERFUMES", synthetic):
            _keyword_boundary_regex.cache_clear()
            result = _layer1_exact_match("crimson emerald and topaz please")
            _keyword_boundary_regex.cache_clear()

        assert set(result.matched_perfume_ids) == {
            "brandone_crimson",
            "brandtwo_emerald",
            "brandthree_topaz",
        }

    def test_same_shared_keyword_variant_collision_is_still_a_single_pick(self):
        """A bare shared generic term matching several concentration
        variants of ONE product line is still a single ambiguous item, not
        a multi-perfume request — the customer named one thing, we're just
        unsure which variant. Unaffected by the multi-perfume fix above."""
        synthetic = {
            "brand_sauvage_edt": {
                "display_name": "Brand Sauvage EDT",
                "keywords": ["sauvage"],
                "prices": {"3ml": 100},
                "clone_of": None,
            },
            "brand_sauvage_edp": {
                "display_name": "Brand Sauvage EDP",
                "keywords": ["sauvage"],
                "prices": {"3ml": 120},
                "clone_of": None,
            },
        }
        with patch("app.matcher.PERFUMES", synthetic):
            _keyword_boundary_regex.cache_clear()
            result = _layer1_exact_match("sauvage price")
            _keyword_boundary_regex.cache_clear()

        assert result.ambiguous is False
        assert result.perfume_id in synthetic
        assert result.matched_perfume_ids is None


class TestHasConfidentKeywordMatch:
    """
    has_confident_keyword_match is the deterministic pre-check app.main
    uses to veto a catalog-phrase message straight to the catalog reply,
    without ever handing it to Groq or the fuzzy matcher — it must say
    False for a bare catalog word (the actual production bug) and True
    whenever a real perfume is also precisely named in the same message.
    """

    def test_bare_catalog_word_has_no_confident_match(self):
        assert has_confident_keyword_match("catalogue") is False
        assert has_confident_keyword_match("catalog") is False

    def test_real_keyword_present_is_confident(self):
        assert has_confident_keyword_match("how much for sauvage") is True

    def test_family_ambiguity_still_counts_as_confident(self):
        """A bare "9pm" is a genuine multi-candidate case (matched_perfume_ids,
        no single perfume_id) — must still count as "found something", since
        it's a real, precise, deterministic result, not a mismatch risk."""
        assert has_confident_keyword_match("9pm") is True

    def test_typo_alone_is_not_confident(self):
        """A misspelling that only fuzzy/LLM could resolve must NOT count —
        that's exactly the case the veto needs to let through to the full
        pipeline instead of blocking."""
        assert has_confident_keyword_match("suvage") is False

    def test_empty_message(self):
        assert has_confident_keyword_match("") is False


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


class TestPrimaryLLMMatch:
    """
    _primary_llm_match is the Groq-first primary path (promoted from a
    last-resort "Layer 3" to running first on every message — see
    TestMatchPerfumeGroqFirstWithFallback for the full pipeline ordering).
    """

    def test_empty_shortlist_skips_the_llm_call_entirely(self):
        """No point calling Groq (or even building a prompt) if there's
        nothing plausible to offer it."""
        with patch("app.groq_client.classify_and_phrase", new_callable=AsyncMock) as mock_classify:
            result = asyncio.run(_primary_llm_match("hi"))
        mock_classify.assert_not_called()
        assert result.perfume_id is None

    def test_calls_classify_and_phrase_with_a_bounded_candidates_dict(self):
        """End-to-end wiring check: the primary path must pass a shortlist,
        not the full PERFUMES dict, to the Groq client."""
        with patch(
            "app.groq_client.classify_and_phrase",
            new_callable=AsyncMock,
            return_value=GroqClassification(),
        ) as mock_classify:
            asyncio.run(_primary_llm_match("suvage 10ml"))

        mock_classify.assert_called_once()
        _, kwargs = mock_classify.call_args
        assert len(kwargs["candidates"]) <= 25
        assert len(kwargs["candidates"]) < len(PERFUMES)

    def test_matched_result_carries_opening_and_closing(self):
        # Returns whatever candidate it was actually offered, rather than a
        # hardcoded pid — classify_and_phrase can only ever return something
        # from the shortlist it's given, so a fixed unrelated pid would
        # (correctly) get rejected by the "pid in candidates" check.
        async def fake_classify(message, candidates):
            return GroqClassification(
                perfume_ids=[next(iter(candidates))], opening="Hi!", closing="Cool?"
            )

        with patch(
            "app.groq_client.classify_and_phrase",
            new_callable=AsyncMock,
            side_effect=fake_classify,
        ):
            result = asyncio.run(_primary_llm_match("suvage 10ml"))
        assert result.perfume_id is not None
        assert result.layer == "llm"
        assert result.confidence == 80.0
        assert result.opening == "Hi!"
        assert result.closing == "Cool?"

    def test_multiple_ids_return_a_multi_candidate_result(self):
        """The core new behavior: Groq can identify 2+ distinct perfumes in
        one message (e.g. "sauvage and eros price") — that must surface as
        matched_perfume_ids, the same shape the fallback exact matcher uses
        for its own multi-candidate case, so app.main can build a full card
        for each regardless of which layer found them."""
        async def fake_classify(message, candidates):
            ids = list(candidates)[:2]
            return GroqClassification(perfume_ids=ids, opening="Found a couple!", closing="Cool?")

        with patch(
            "app.groq_client.classify_and_phrase",
            new_callable=AsyncMock,
            side_effect=fake_classify,
        ):
            result = asyncio.run(_primary_llm_match("suvage 10ml"))
        assert result.perfume_id is None
        assert result.ambiguous is True
        assert result.matched_perfume_ids is not None
        assert len(result.matched_perfume_ids) == 2
        assert result.layer == "llm"
        assert result.opening == "Found a couple!"

    def test_empty_classification_returns_empty_result(self):
        with patch(
            "app.groq_client.classify_and_phrase",
            new_callable=AsyncMock,
            return_value=GroqClassification(),
        ):
            result = asyncio.run(_primary_llm_match("suvage 10ml"))
        assert result.perfume_id is None
        assert result.matched_perfume_ids is None

    def test_id_outside_offered_candidates_is_rejected(self):
        """Defense in depth even though app.groq_client already validates
        this — a pid outside what was offered must never be trusted."""
        async def fake_classify(message, candidates):
            return GroqClassification(
                perfume_ids=["totally_not_offered_xyz"], opening="Hi", closing="Ok"
            )

        with patch(
            "app.groq_client.classify_and_phrase",
            new_callable=AsyncMock,
            side_effect=fake_classify,
        ):
            result = asyncio.run(_primary_llm_match("suvage 10ml"))
        assert result.perfume_id is None
        assert result.matched_perfume_ids is None

    def test_one_valid_one_invalid_id_keeps_only_the_valid_one(self):
        async def fake_classify(message, candidates):
            return GroqClassification(
                perfume_ids=[next(iter(candidates)), "totally_not_offered_xyz"],
                opening="Hi",
                closing="Ok",
            )

        with patch(
            "app.groq_client.classify_and_phrase",
            new_callable=AsyncMock,
            side_effect=fake_classify,
        ):
            result = asyncio.run(_primary_llm_match("suvage 10ml"))
        assert result.perfume_id is not None
        assert result.matched_perfume_ids is None

    def test_exception_returns_empty_result_not_raised(self):
        with patch(
            "app.groq_client.classify_and_phrase",
            new_callable=AsyncMock,
            side_effect=RuntimeError("groq down"),
        ):
            result = asyncio.run(_primary_llm_match("suvage 10ml"))
        assert result.perfume_id is None


class TestMatchPerfumeGroqFirstWithFallback:
    """
    The core behavior change: Groq runs FIRST for every message now, not
    as a last resort. The free exact/fuzzy matchers only run when Groq
    isn't confident (or errors/times out) — so the bot never goes fully
    silent on a Groq outage or rate limit (this has actually happened once
    already this session, via a token-limit error).
    """

    def test_confident_groq_match_is_used_directly(self):
        async def fake_classify(message, candidates):
            return GroqClassification(
                perfume_ids=[next(iter(candidates))], opening="Hi!", closing="Cool?"
            )

        with patch(
            "app.groq_client.classify_and_phrase",
            new_callable=AsyncMock,
            side_effect=fake_classify,
        ):
            result = asyncio.run(match_perfume("suvage 10ml"))
        assert result.layer == "llm"
        assert result.opening == "Hi!"

    def test_groq_multi_match_is_used_directly_without_falling_through(self):
        """A confident multi-perfume Groq result must short-circuit the
        pipeline exactly like a single confident match does — it must not
        fall through to the free matchers just because perfume_id (the
        single-match field) is empty."""
        async def fake_classify(message, candidates):
            return GroqClassification(perfume_ids=list(candidates)[:2], opening="Found 2!")

        with patch(
            "app.groq_client.classify_and_phrase",
            new_callable=AsyncMock,
            side_effect=fake_classify,
        ):
            result = asyncio.run(match_perfume("suvage 10ml"))
        assert result.layer == "llm"
        assert result.matched_perfume_ids is not None
        assert len(result.matched_perfume_ids) == 2

    def test_groq_failure_falls_through_to_exact_match(self):
        """A message that resolves fine via exact match must still work
        even when Groq is completely down."""
        with patch(
            "app.groq_client.classify_and_phrase",
            new_callable=AsyncMock,
            side_effect=RuntimeError("groq is down"),
        ):
            result = asyncio.run(match_perfume("sauvage"))
        assert result.perfume_id is not None
        assert result.layer == "exact"

    def test_groq_no_confident_match_falls_through_to_exact_match(self):
        with patch(
            "app.groq_client.classify_and_phrase",
            new_callable=AsyncMock,
            return_value=GroqClassification(),
        ):
            result = asyncio.run(match_perfume("sauvage"))
        assert result.perfume_id is not None
        assert result.layer == "exact"

    def test_groq_and_exact_both_miss_falls_through_to_fuzzy(self):
        with patch(
            "app.groq_client.classify_and_phrase",
            new_callable=AsyncMock,
            return_value=GroqClassification(),
        ):
            result = asyncio.run(match_perfume("savage price please"))  # typo, misses exact
        assert result.perfume_id is not None
        assert result.layer == "fuzzy"

    def test_everything_missing_returns_empty_result(self):
        with patch(
            "app.groq_client.classify_and_phrase",
            new_callable=AsyncMock,
            return_value=GroqClassification(),
        ):
            result = asyncio.run(match_perfume("completely unrelated gibberish qzxjklw"))
        assert result.perfume_id is None
        assert result.ambiguous is False


class TestLooksLikeExplicitRequest:
    """
    Deterministic stand-in for Groq's explicit_ask judgment, used only by
    the exact/fuzzy fallback layers (see match_perfume) — Groq itself is
    unavailable whenever these run, so there's no LLM to ask. A short
    message is treated as explicit outright; a longer one needs an actual
    request cue word.
    """

    def test_bare_name_is_explicit(self):
        assert _looks_like_explicit_request("sauvage") is True

    def test_name_plus_size_is_explicit(self):
        assert _looks_like_explicit_request("sauvage 10ml") is True

    def test_long_message_with_a_price_cue_is_explicit(self):
        assert (
            _looks_like_explicit_request(
                "hey quick question how much is sauvage going for these days"
            )
            is True
        )

    def test_long_message_naming_a_perfume_without_a_cue_is_not_explicit(self):
        """The exact reported bug: a perfume name surfacing mid-conversation
        (e.g. talking to the shop owner about something else) is not a
        request just because the name is technically present."""
        assert (
            _looks_like_explicit_request("the owner told me sauvage is really nice apparently")
            is False
        )

    def test_empty_message_is_not_explicit(self):
        assert _looks_like_explicit_request("") is False


class TestExplicitRequestGateOnFallbackLayers:
    """
    match_perfume end-to-end with Groq mocked away (simulating either a real
    outage or Groq's own explicit_ask=false collapsing to an empty
    classification) — the deterministic exact/fuzzy fallback must apply the
    same "mention vs. ask" distinction, not just fire on any keyword hit.
    """

    def test_bare_name_still_matches_via_fallback(self):
        with patch(
            "app.groq_client.classify_and_phrase",
            new_callable=AsyncMock,
            return_value=GroqClassification(),
        ):
            result = asyncio.run(match_perfume("sauvage"))
        assert result.perfume_id is not None
        assert result.layer == "exact"

    def test_name_with_request_cue_still_matches_via_fallback(self):
        with patch(
            "app.groq_client.classify_and_phrase",
            new_callable=AsyncMock,
            return_value=GroqClassification(),
        ):
            result = asyncio.run(match_perfume("what is the price of sauvage please"))
        assert result.perfume_id is not None

    def test_name_mentioned_in_passing_mid_conversation_stays_silent(self):
        """The reported production bug, reproduced directly against
        match_perfume: a perfume name inside a longer, unrelated sentence
        (no price/buy/availability cue) must not auto-fire a price card."""
        with patch(
            "app.groq_client.classify_and_phrase",
            new_callable=AsyncMock,
            return_value=GroqClassification(),
        ):
            result = asyncio.run(
                match_perfume("the owner told me sauvage is really nice apparently")
            )
        assert result.perfume_id is None
        assert result.matched_perfume_ids is None
        assert result.ambiguous is False

    def test_typo_mentioned_in_passing_stays_silent_via_fuzzy_fallback_too(self):
        with patch(
            "app.groq_client.classify_and_phrase",
            new_callable=AsyncMock,
            return_value=GroqClassification(),
        ):
            result = asyncio.run(
                match_perfume("my friend recently bought a bottle of suvage last week apparently")
            )
        assert result.perfume_id is None
