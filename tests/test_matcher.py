"""
Tests for the matching pipeline (app/matcher.py).

The pipeline is two stages: app.name_index decides which perfumes a message
could be naming (covered by tests/test_name_index.py), then Groq judges
whether the customer is actually asking and narrows the list. These tests
cover the second stage and the wiring — including every production bug the
previous keyword-based matcher was patched for, re-expressed against the
architecture that replaced it. Those bugs are the reason the rules exist;
they are not hypotheticals.

Groq is mocked throughout. Its real behaviour is exercised separately by
scripts/benchmark_llm.py, which needs an API key.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app import conversation
from app.catalog import PERFUMES
from app.groq_client import GroqClassification
from app.matcher import (
    sizes_per_perfume,
    MatchResult,
    _looks_like_explicit_request,
    extract_requested_size_ml,
    has_confident_keyword_match,
    match_perfume,
    normalize_message,
)
from app.name_index import message_focus, search

CLASSIFY = "app.groq_client.classify_and_phrase"


def run(coro):
    return asyncio.run(coro)


def groq(**kwargs):
    """Patch Groq with a fixed GroqClassification."""
    return patch(CLASSIFY, new_callable=AsyncMock, return_value=GroqClassification(**kwargs))


def groq_unreachable():
    """Groq itself could not be asked — its documented None return."""
    return patch(CLASSIFY, new_callable=AsyncMock, return_value=None)


def groq_picks_first():
    """Groq confirms whatever candidate the index put in front of it."""

    async def fake(message, candidates, history=None):
        return GroqClassification(
            perfume_ids=[next(iter(candidates))],
            explicit_ask=True,
            opening="Great choice!",
            closing="Let me know!",
        )

    return patch(CLASSIFY, new_callable=AsyncMock, side_effect=fake)


def ids(result: MatchResult) -> list[str]:
    return result.matched_perfume_ids or ([result.perfume_id] if result.perfume_id else [])


class TestNormalize:
    def test_lowercase(self):
        assert normalize_message("SAUVAGE") == "sauvage"

    def test_strip_punctuation(self):
        assert normalize_message("what's the price?") == "what s the price"

    def test_collapse_whitespace(self):
        assert normalize_message("  sauvage   price  ") == "sauvage price"

    def test_empty(self):
        assert normalize_message("") == ""
        assert normalize_message("   ") == ""


class TestExtractRequestedSizeMl:
    def test_bare_size_no_space(self):
        assert extract_requested_size_ml("sauvage 10ml") == 10

    def test_size_with_space(self):
        assert extract_requested_size_ml("sauvage 10 ml price") == 10

    def test_full_bottle_size(self):
        assert extract_requested_size_ml("sauvage 100ml") == 100

    def test_first_size_wins_when_multiple_mentioned(self):
        assert extract_requested_size_ml("sauvage 3ml or 5ml") == 3

    def test_digits_without_ml_suffix_are_not_a_size(self):
        assert extract_requested_size_ml("sauvage 10") is None

    def test_no_size_mentioned(self):
        assert extract_requested_size_ml("sauvage price") is None

    def test_empty(self):
        assert extract_requested_size_ml("") is None


class TestHasConfidentKeywordMatch:
    """
    app.main's escape hatch on the catalog-request veto: a "send me the
    catalogue" message goes straight to the catalog link UNLESS it also
    names a specific product confidently.
    """

    def test_bare_catalog_word_is_not_a_product(self):
        assert has_confident_keyword_match("catalogue") is False
        assert has_confident_keyword_match("catalog") is False

    def test_catalog_phrase_naming_a_real_product_is_confident(self):
        assert has_confident_keyword_match("show me 9pm rebel price") is True

    def test_polite_catalog_request_is_not_hijacked(self):
        """The exact production bug: the fuzzy matcher scored "please"
        against the catalog token "pleasure" at 85.7 and answered a catalog
        request with one perfume's price card."""
        assert has_confident_keyword_match("send me the catalogue please") is False

    def test_empty(self):
        assert has_confident_keyword_match("") is False


class TestLooksLikeExplicitRequest:
    """
    The deterministic intent gate, used when Groq is unavailable. `focus` is
    how much of the message is the product name itself.
    """

    def test_bare_name_is_explicit(self):
        assert _looks_like_explicit_request("sauvage", 1.0) is True

    def test_long_name_with_no_cue_word_is_explicit(self):
        """The bug this replaced a word-count rule to fix: real catalog
        names run to eight or more words, and the old five-word cap silently
        dropped every one of them when no cue word was present."""
        query = "Issey Miyake L'Eau d'Issey Solar Lavender"
        assert _looks_like_explicit_request(query, message_focus(query, search(query))) is True

    def test_long_message_with_a_cue_is_explicit(self):
        assert (
            _looks_like_explicit_request(
                "hey quick question how much is sauvage going for these days", 0.2
            )
            is True
        )

    def test_name_in_passing_with_no_cue_is_not_explicit(self):
        """A perfume name surfacing mid-conversation is not a request."""
        assert (
            _looks_like_explicit_request("the owner told me sauvage is really nice", 0.3)
            is False
        )

    @pytest.mark.parametrize(
        "message",
        [
            "the 9 pm rebel is really good but i dont want ut",
            "the 9 pm rebel is really good but i don't want ut",
            "my friend uses sauvage but i am not interested in buying it",
            "someone gifted me sauvage and i will never need another bottle",
            "sauvage nahi chahiye mujhe abhi",
        ],
    )
    def test_negation_overrides_everything(self, message):
        """The exact reported bug: "...but I don't want ut" hit the "want"
        cue and fired a price card at a customer who had just declined.
        Negation is checked before the focus shortcut, so even a short
        message that is mostly the name stays silent."""
        assert _looks_like_explicit_request(normalize_message(message), 1.0) is False

    def test_negation_guard_is_not_a_blanket_suppression(self):
        assert (
            _looks_like_explicit_request(
                normalize_message("hey quick question how much do you want for sauvage"), 0.3
            )
            is True
        )

    def test_empty(self):
        assert _looks_like_explicit_request("", 1.0) is False


class TestGroqRefinement:
    def test_confident_pick_is_used_with_its_phrasing(self):
        with groq_picks_first():
            result = run(match_perfume("suvage 10ml"))
        assert result.perfume_id is not None
        assert result.opening == "Great choice!"
        assert result.closing == "Let me know!"

    def test_groq_is_only_offered_what_the_index_found(self):
        """Groq narrows; it does not search. It must never see the full
        catalog — the old design handed it a 25-entry fuzzy shortlist that
        was routinely all noise, and it picked from the noise."""
        with patch(
            CLASSIFY, new_callable=AsyncMock, return_value=GroqClassification()
        ) as mock:
            run(match_perfume("9pm rebel"))

        mock.assert_called_once()
        offered = set(mock.call_args.kwargs["candidates"])
        assert offered
        assert offered < set(PERFUMES)
        assert offered <= {s.perfume_id for s in search("9pm rebel")}

    def test_an_id_the_index_never_found_is_discarded(self):
        """Defense in depth: app.groq_client validates this too, but a
        hallucinated id must never be able to reach a price card. Every id
        that survives has to be one the index itself produced."""

        async def fake(message, candidates, history=None):
            return GroqClassification(
                perfume_ids=["totally_not_offered_xyz"], explicit_ask=True
            )

        with patch(CLASSIFY, new_callable=AsyncMock, side_effect=fake):
            result = run(match_perfume("9pm rebel"))

        assert "totally_not_offered_xyz" not in ids(result)
        assert set(ids(result)) <= {s.perfume_id for s in search("9pm rebel")}

    def test_a_mention_with_a_hallucinated_id_stays_silent(self):
        """Same discard, on a message the bare-name safety valve does not
        cover — nothing is left to answer with, so nothing is sent."""

        async def fake(message, candidates, history=None):
            return GroqClassification(perfume_ids=["nope_xyz"], explicit_ask=True)

        with patch(CLASSIFY, new_callable=AsyncMock, side_effect=fake):
            result = run(match_perfume("my friend was telling me about 9pm rebel yesterday"))
        assert ids(result) == []

    def test_one_valid_one_invalid_id_keeps_the_valid_one(self):
        async def fake(message, candidates, history=None):
            return GroqClassification(
                perfume_ids=[next(iter(candidates)), "nope_xyz"], explicit_ask=True
            )

        with patch(CLASSIFY, new_callable=AsyncMock, side_effect=fake):
            result = run(match_perfume("9pm rebel"))
        assert len(ids(result)) == 1

    def test_multiple_ids_surface_as_multiple_cards(self):
        async def fake(message, candidates, history=None):
            return GroqClassification(
                perfume_ids=list(candidates)[:2], explicit_ask=True, opening="Found a couple!"
            )

        with patch(CLASSIFY, new_callable=AsyncMock, side_effect=fake):
            result = run(match_perfume("sauvage and eros price"))
        assert result.ambiguous is True
        assert len(result.matched_perfume_ids) == 2

    def test_narrowing_several_candidates_to_one_is_the_llm_layer(self):
        """Worth distinguishing in the analytics dashboard from a match the
        index resolved unambiguously by itself."""
        assert len(search("sauvage")) > 1
        with groq_picks_first():
            result = run(match_perfume("sauvage"))
        assert result.layer == "llm"


class TestGroqSaysNotAnAsk:
    def test_a_mention_stays_silent(self):
        with groq(perfume_ids=[], explicit_ask=False):
            result = run(match_perfume("the owner told me sauvage is really nice apparently"))
        assert ids(result) == []
        assert result.ambiguous is False

    def test_a_bare_product_name_replies_anyway(self):
        """The safety valve. When the message is nothing but a clearly-typed
        product name, "the customer isn't asking" is not a judgment call —
        it is the model being wrong, and the cost is silence on the single
        most common message this bot gets."""
        with groq(perfume_ids=[], explicit_ask=False):
            result = run(match_perfume("9pm rebel"))
        assert result.perfume_id is not None

    def test_the_valve_does_not_open_on_a_negated_message(self):
        with groq(perfume_ids=[], explicit_ask=False):
            result = run(match_perfume("9pm rebel nahi chahiye"))
        assert ids(result) == []


class TestGroqUnavailable:
    """An outage must degrade the bot's manners, not its availability — the
    index resolves the perfume either way."""

    def test_exception_still_matches(self):
        with patch(CLASSIFY, new_callable=AsyncMock, side_effect=RuntimeError("groq down")):
            result = run(match_perfume("9pm rebel"))
        assert result.perfume_id is not None
        assert result.llm_unavailable is True

    def test_none_return_still_matches(self):
        with groq_unreachable():
            result = run(match_perfume("9pm rebel"))
        assert result.perfume_id is not None
        assert result.llm_unavailable is True

    def test_typo_still_matches(self):
        with groq_unreachable():
            result = run(match_perfume("9pm rebl"))
        assert result.perfume_id is not None

    def test_deterministic_intent_gate_still_applies(self):
        """Without Groq there is no contextual intent judgment, so the
        coarse gate stands in — a name in passing must still stay silent."""
        with groq_unreachable():
            result = run(match_perfume("the owner told me sauvage is really nice apparently"))
        assert ids(result) == []

    def test_kaaf_only_collision_does_not_over_match(self):
        """The exact reported wording. "only" was a standalone auto-generated
        keyword for "Afnan Supremacy Not Only Intense", so this message
        returned that product alongside the one the customer actually named.
        Nothing is keyed off single generic words any more."""
        with groq_unreachable():
            result = run(match_perfume("i want to confirm kaaf only"))
        found = [
            PERFUMES[p]["display_name"].lower() for p in ids(result)
        ]
        assert found, "the customer named a product and is confirming an order"
        assert all("kaaf" in n for n in found), found

    def test_filler_words_in_a_long_sentence_do_not_become_a_product(self):
        """Reported live: a customer asked about a perfume the catalog did
        not have, inside a longer sentence, and got a confident price card
        for "FW/FRENCH AVENUE BARAKKAT AQUA STELLAR EDP" — filler words in
        the sentence had scored against unrelated keywords ("tell"/"stellar"
        at 72.7, "have"/"heaven" at 80)."""
        with groq_unreachable():
            result = run(
                match_perfume(
                    "5ml for the penhalgions endymon and please tell me how much "
                    "of a difference will bluedart air have"
                )
            )
        assert ids(result) == []


class TestConversationContext:
    """
    A follow-up that names no perfume resolves against the card the customer
    is replying to. Without this the bot answered "and 5ml?" with silence.
    """

    def setup_method(self):
        conversation.clear()

    def _history(self, pid: str) -> list[dict]:
        return [
            {"role": "customer", "text": "9pm rebel price"},
            {"role": "bot", "text": "(card)", "perfume_ids": [pid], "perfume_names": ["x"]},
        ]

    @pytest.mark.parametrize(
        "followup",
        ["and 5ml?", "10ml ka kya rate hai", "how much for that one", "iska price kya hai"],
    )
    def test_followup_resolves_to_the_last_perfume(self, followup):
        pid = next(iter(PERFUMES))
        with groq_unreachable():
            result = run(match_perfume(followup, history=self._history(pid)))
        assert result.perfume_id == pid
        assert result.layer == "context"

    def test_unrelated_message_does_not_resolve_from_context(self):
        """Context must not turn every message into a price query — "thanks"
        and "order kab aayega" stay silent exactly as they did before."""
        pid = next(iter(PERFUMES))
        for message in ("thanks bhai", "order kab aayega", "ok"):
            with groq_unreachable():
                result = run(match_perfume(message, history=self._history(pid)))
            assert ids(result) == [], message

    def test_declining_does_not_resolve_from_context(self):
        pid = next(iter(PERFUMES))
        with groq_unreachable():
            result = run(match_perfume("no i dont want it", history=self._history(pid)))
        assert ids(result) == []

    def test_no_history_means_no_context_match(self):
        with groq_unreachable():
            result = run(match_perfume("and 5ml?", history=[]))
        assert ids(result) == []

    def test_a_named_perfume_still_wins_over_context(self):
        """Context fills gaps; it never overrides what the customer just
        said."""
        other = next(iter(PERFUMES))
        with groq_unreachable():
            result = run(match_perfume("9pm rebel 5ml", history=self._history(other)))
        assert "rebel" in PERFUMES[result.perfume_id]["display_name"].lower()

    def test_ambiguous_history_returns_every_candidate(self):
        pids = list(PERFUMES)[:3]
        history = [
            {"role": "customer", "text": "sauvage"},
            {"role": "bot", "text": "(cards)", "perfume_ids": pids, "perfume_names": []},
        ]
        with groq_unreachable():
            result = run(match_perfume("5ml", history=history))
        assert result.matched_perfume_ids == pids

    def test_history_is_passed_to_groq(self):
        history = self._history(next(iter(PERFUMES)))
        with patch(
            CLASSIFY, new_callable=AsyncMock, return_value=GroqClassification()
        ) as mock:
            run(match_perfume("9pm rebel", history=history))
        assert mock.call_args.kwargs["history"] == history


class TestEdgeCases:
    @pytest.mark.parametrize("message", ["", "   ", "12345", "a", "🙂"])
    def test_degenerate_input_is_silent(self, message):
        with groq_unreachable():
            result = run(match_perfume(message))
        assert ids(result) == []

    def test_no_groq_call_is_made_when_the_index_finds_nothing(self):
        """No candidates means nothing to judge — the call would be a
        wasted round trip and an invitation to invent one."""
        with patch(CLASSIFY, new_callable=AsyncMock) as mock:
            run(match_perfume("order kab aayega"))
        mock.assert_not_called()

    def test_very_long_message_with_a_name_and_a_cue(self):
        with groq_unreachable():
            result = run(match_perfume("i was wondering " * 20 + "what is the price of 9pm rebel"))
        assert result.perfume_id is not None

    def test_scores_are_exposed_for_diagnostics(self):
        with groq_picks_first():
            result = run(match_perfume("9pm rebel"))
        assert result.scores
        assert all(isinstance(score, float) for _, score in result.scores)


class TestBareNameValveIsNotAboutCandidateCount:
    """
    Groq marks a bare product name "not an ask" in two reproducible ways,
    both observed live and both costing a reply to a real question:

      * "9pm rebel" is an ask, "9pm rebl" is not — the misspelling flips it.
      * A one-word name matching several products is not an ask, even as the
        model writes "We have Aventus variants from Armaf, Blanche and
        Anilla!" in the same response.

    The valve overriding that verdict therefore keys on the message, not on
    how many products the name happens to fit.
    """

    def setup_method(self):
        conversation.clear()

    def test_a_bare_name_matching_several_products_still_replies(self):
        assert len(search("sauvage")) > 1
        with groq(perfume_ids=[], explicit_ask=False):
            result = run(match_perfume("sauvage"))
        assert ids(result), "a bare product name must never be answered with silence"

    def test_every_candidate_is_returned_not_just_the_top_one(self):
        """Groq declined to narrow, so there is no basis for picking one —
        showing all of them is the honest answer, and they have different
        prices."""
        expected = {s.perfume_id for s in search("sauvage")}
        with groq(perfume_ids=[], explicit_ask=False):
            result = run(match_perfume("sauvage"))
        assert set(ids(result)) == expected

    def test_a_misspelled_bare_name_still_replies(self):
        with groq(perfume_ids=[], explicit_ask=False):
            result = run(match_perfume("9pm rebl"))
        assert ids(result)

    def test_a_passing_mention_is_still_silenced(self):
        """The valve must not become a blanket override — it opens only for
        messages that are essentially nothing but the name."""
        with groq(perfume_ids=[], explicit_ask=False):
            result = run(match_perfume("the owner told me sauvage is really nice apparently"))
        assert ids(result) == []

    def test_a_decline_is_still_silenced(self):
        with groq(perfume_ids=[], explicit_ask=False):
            result = run(match_perfume("sauvage nahi chahiye"))
        assert ids(result) == []


class TestContextDoesNotAnswerForAProductWeDoNotHave:
    """
    Observed live. After a Kaaf price card, the customer typed "fathreneit".
    The index found nothing (Fahrenheit is not in this catalog), so the
    message fell through to context resolution — which handed Groq the
    PREVIOUS card's candidates and asked which one was meant. Groq replied
    "Fahrenheit EDT is a great choice!", confirming a perfume that does not
    exist, in the shop's own voice.

    A follow-up refers to what was shown. A message that introduces a name
    is not a follow-up, whether or not we recognise the name.
    """

    def setup_method(self):
        conversation.clear()

    def _history(self) -> list[dict]:
        pid = next(iter(PERFUMES))
        return [
            {"role": "customer", "text": "kaaf price"},
            {"role": "bot", "text": "(card)", "perfume_ids": [pid], "perfume_names": ["Kaaf"]},
        ]

    @pytest.mark.parametrize(
        # All genuinely absent from this catalog — no entry carries the name
        # and nothing lists it as a clone_of. A name the catalog DOES have
        # never reaches this code path at all: the index resolves it.
        "message",
        ["penhalgions endymon", "penhaligons endymion", "zoologist squid", "jo malone english pear"],
    )
    def test_an_unrecognised_name_is_not_resolved_from_context(self, message):
        with groq_picks_first():
            result = run(match_perfume(message, history=self._history()))
        assert ids(result) == [], f"{message!r} must not inherit the previous card"

    @pytest.mark.parametrize(
        "message",
        ["and 5ml?", "the edp one please", "second one 10ml", "how much for that one"],
    )
    def test_genuine_follow_ups_still_resolve(self, message):
        """The guard must not swallow the feature. Concentration words are
        allowed through — "the EDP one" refers to what was shown, it does not
        name something new."""
        with groq_picks_first():
            result = run(match_perfume(message, history=self._history()))
        assert ids(result), f"{message!r} should have resolved from context"


class TestWishlistIsAnAsk:
    """
    A pasted shopping list must be answered. Reported live: a customer sent
    sixteen perfumes across four brands and got silence, while Groq's own
    reply named three of the products it had just recognised and still set
    explicit_ask=false.
    """

    WISHLIST = (
        "Al Haramain: - Detour Noir - Detour Eco - Detour Intense Noir - Amber Ruby "
        "Edition  Armaf: - Club de Nuit Intense Man EDP - Club de Nuit Untold  "
        "French Avenue: - Liquid Brun - Cocoa Morado - Aether Extrait  Maison "
        "Alhambra: - Tobacco Touch - Woody Oud - Opulence Leather - Porto Neroli - "
        "Toscano Leather - Fabulo Intense - Black Origami"
    )

    def setup_method(self):
        conversation.clear()

    def test_answered_even_when_groq_calls_it_not_an_ask(self):
        with groq(perfume_ids=[], explicit_ask=False):
            result = run(match_perfume(self.WISHLIST))
        assert len(ids(result)) >= 4

    def test_answered_when_groq_is_unavailable(self):
        with groq_unreachable():
            result = run(match_perfume(self.WISHLIST))
        assert len(ids(result)) >= 4

    def test_groq_sees_every_candidate_the_index_found(self):
        """A tighter shortlist truncated the list: eight products were found
        and six were offered, so two could not be answered whatever Groq
        replied."""
        found = {s.perfume_id for s in search(self.WISHLIST)}
        with patch(
            CLASSIFY, new_callable=AsyncMock, return_value=GroqClassification()
        ) as mock:
            run(match_perfume(self.WISHLIST))
        assert set(mock.call_args.kwargs["candidates"]) == found

    def test_two_names_in_passing_are_still_not_an_ask(self):
        """The list rule keys on the message being mostly product names, so
        a remark that happens to name two perfumes stays silent."""
        with groq(perfume_ids=[], explicit_ask=False):
            result = run(match_perfume("my friend uses sauvage and eros and loves them"))
        assert ids(result) == []

    def test_a_declined_list_stays_silent(self):
        with groq(perfume_ids=[], explicit_ask=False):
            result = run(match_perfume("i dont want detour noir or cocoa morado"))
        assert ids(result) == []


class TestPerPerfumeSizes:
    """
    Reported live: a customer ordering several decants sized them
    individually — "3ml for the first one, 5ml for the second" — and got the
    FIRST size quoted for every product. On a three-item order that is two
    wrong prices, on an order the customer is about to pay for.

    A size belongs to the name it was written next to.
    """

    def setup_method(self):
        conversation.clear()

    def _sizes(self, message):
        return sizes_per_perfume(message, search(message))

    def _named(self, message):
        return {
            PERFUMES[pid]["display_name"]: ml
            for pid, ml in self._sizes(message).items()
        }

    def test_each_product_gets_its_own_size(self):
        got = self._named("9pm rebel 3ml, kaaf 10ml")
        assert got["Afnan 9PM Rebel"] == 3
        assert got["Ahmed Al Maghribi Kaaf"] == 10

    def test_three_different_sizes(self):
        got = self._named("9pm rebel 3ml, khamrah 5ml, kaaf 10ml")
        assert got["Afnan 9PM Rebel"] == 3
        assert got["Lattafa Khamrah"] == 5
        assert got["Ahmed Al Maghribi Kaaf"] == 10

    def test_one_size_for_the_whole_order_still_applies_to_all(self):
        got = self._named("9pm rebel and kaaf 5ml")
        assert set(got.values()) == {5}

    def test_a_size_written_before_the_names_still_applies(self):
        """"3ml of X and Y" — the size leads, which the next-size-after rule
        alone would miss entirely."""
        got = self._named("3ml of 9pm rebel and kaaf")
        assert set(got.values()) == {3}

    def test_no_size_mentioned_yields_nothing(self):
        assert self._sizes("9pm rebel and kaaf") == {}

    def test_sizes_ride_along_on_the_match_result(self):
        async def fake(message, candidates, history=None):
            return GroqClassification(perfume_ids=list(candidates), explicit_ask=True)

        with patch(CLASSIFY, new_callable=AsyncMock, side_effect=fake):
            result = run(match_perfume("9pm rebel 3ml, kaaf 10ml"))

        named = {PERFUMES[p]["display_name"]: ml for p, ml in result.sizes.items()}
        assert named["Afnan 9PM Rebel"] == 3
        assert named["Ahmed Al Maghribi Kaaf"] == 10


class TestMultiSizeCard:
    """The reply itself has to show those different sizes, not one of them."""

    def test_card_prices_each_product_at_its_own_size(self):
        from app.formatter import build_multi_price_card

        rebel = next(p for p, d in PERFUMES.items() if d["display_name"] == "Afnan 9PM Rebel")
        kaaf = next(p for p, d in PERFUMES.items() if "Kaaf" == d["display_name"].split()[-1])
        card = build_multi_price_card(
            [rebel, kaaf], None, None, requested_ml=3, sizes={rebel: 3, kaaf: 10}
        )
        assert "3ml" in card and "10ml" in card
        expected = PERFUMES[rebel]["prices"]["3ml"] + PERFUMES[kaaf]["prices"]["10ml"]
        assert f"{expected:,}" in card

    def test_a_product_without_its_own_size_falls_back(self):
        from app.formatter import build_multi_price_card

        rebel = next(p for p, d in PERFUMES.items() if d["display_name"] == "Afnan 9PM Rebel")
        kaaf = next(p for p, d in PERFUMES.items() if "Kaaf" == d["display_name"].split()[-1])
        card = build_multi_price_card([rebel, kaaf], None, None, requested_ml=5, sizes={rebel: 3})
        assert "3ml" in card and "5ml" in card
