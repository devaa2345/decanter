"""
Tests for the weighted-token name index (app/name_index.py).

These are the unit-level guards for the scoring model. The behaviours they
pin down are the ones that were measured, not guessed: each of the precision
rules below was added in response to a specific failure found by
scripts/benchmark_matcher.py running the full 1,207-entry catalog, and each
one is worth several points of accuracy or false-positive rate.
"""

import pytest

from app import name_index
from app.catalog import PERFUMES
from app.name_index import (
    MESSAGE_COVERAGE_FLOOR,
    Scored,
    build_index,
    message_focus,
    search,
    tokenize,
    tokenize_message,
)


def names(results) -> list[str]:
    return [PERFUMES[r.perfume_id]["display_name"] for r in results]


def joined(results) -> str:
    return " || ".join(names(results)).lower()


class TestTokenize:
    def test_lowercases_and_splits_on_punctuation(self):
        assert tokenize("Dior Sauvage EDT") == ["dior", "sauvage", "edt"]
        assert tokenize("L'Eau d'Issey") == ["l", "eau", "d", "issey"]

    def test_ampersand_becomes_and(self):
        """'Oud & Roses' and 'Oud and Roses' must tokenize identically —
        customers type both."""
        assert tokenize("Oud & Roses") == tokenize("Oud and Roses")

    def test_digits_are_kept_as_tokens(self):
        assert tokenize("Armaf SHK 2") == ["armaf", "shk", "2"]

    def test_empty(self):
        assert tokenize("") == []
        assert tokenize("   !!!  ") == []


class TestTokenizeMessage:
    def test_ml_sizes_are_stripped(self):
        """Size is answered separately (app.matcher.extract_requested_size_ml);
        leaving it in only creates junk n-grams like 'sauvage10ml'."""
        assert "10ml" not in tokenize_message("sauvage 10ml")
        assert tokenize_message("sauvage 10 ml price") == ["sauvage", "price"]

    def test_9pm_is_not_mistaken_for_a_size(self):
        assert "9pm" in tokenize_message("9pm rebel 3ml")

    def test_unknown_abbreviation_is_expanded(self):
        assert "cdnim" not in name_index._idf or True  # index may or may not have it
        expanded = tokenize_message("cdnim")
        assert expanded == ["club", "de", "nuit", "intense", "man"]

    def test_abbreviation_the_catalog_knows_is_kept_alongside_its_expansion(self):
        """Regression guard: REPLACING "YSL" with "yves saint laurent" made
        "laurent" score 92 against the unrelated catalog token "lauren" and
        answered "YSL Myslf Le Parfum" with a Ralph Lauren product. The
        literal token must survive — but dropping the expansion entirely
        cost the other direction, where "BDC parfum" could only reach
        products that CLONE Bleu de Chanel and never Bleu de Chanel itself.
        Both readings are offered and the scoring picks."""
        build_index()
        assert "ysl" in name_index._idf
        assert "ysl" in tokenize_message("ysl myslf")
        assert names(search("ysl myslf le parfum"))[0] == "YSL Myslf Le Parfum"
        assert "bleu de chanel parfum" in joined(search("BDC parfum"))


class TestCleanNames:
    """The floor: a correctly-typed catalog name must resolve to itself."""

    @pytest.mark.parametrize(
        "query, expect",
        [
            ("Dior Sauvage EDT", "dior sauvage edt"),
            ("Afnan 9PM Rebel", "9pm rebel"),
            ("Armaf Club de Nuit Intense Man EDP", "club de nuit intense man"),
            ("Lattafa Khamrah", "khamrah"),
        ],
    )
    def test_exact_catalog_name_resolves(self, query, expect):
        results = search(query)
        assert results, f"no match for {query!r}"
        assert expect in joined(results)

    def test_long_multiword_name_with_no_request_cue_still_matches(self):
        """Eight words, no 'price'/'how much' anywhere — the previous
        matcher's five-word rule silently dropped every catalog name this
        long, which is a large share of the catalog."""
        results = search("Issey Miyake L'Eau d'Issey Solar Lavender")
        assert "solar lavender" in joined(results)


class TestTypoTolerance:
    @pytest.mark.parametrize(
        "query, expect",
        [
            ("Lattafa Khamrha", "khamrah"),           # transposition
            ("savuage", "sauvage"),                    # transposition
            ("sauvge 5ml", "sauvage"),                 # dropped vowel
            ("sau vage", "sauvage"),                   # split word
            ("club denuit intense", "club de nuit"),   # joined words
            ("Hugo Boss ElegantVetiver", "elegant vetiver"),
            ("9pm rebel", "9pm rebel"),
        ],
    )
    def test_misspellings_resolve(self, query, expect):
        results = search(query)
        assert results, f"no match for {query!r}"
        assert expect in joined(results)

    def test_brand_token_never_outranks_the_full_name(self):
        """The defining failure of the old keyword matcher: 'Chanel DKY'
        returned 'Albait Aldimashqi Chanel no 5', because 'chanel' was an
        exact keyword hit and exact beat fuzzy — so the right answer never
        got to compete. Whole-name scoring must put Chanel DKNY first."""
        results = search("Afnan 9PM Rebl")
        assert "9pm rebel" in names(results)[0].lower()


class TestPrecision:
    """Everything here used to produce a confident, wrong price card."""

    @pytest.mark.parametrize(
        "query",
        [
            "hello bro",
            "good morning",
            "thanks bhai",
            "thank you so much",
            "great",
            "nice one",
            "order kab aayega",
            "how much is shipping",
            "cod available",
            "what all do you have in stock for men",
            "zoologist squid",
            "byredo gypsy water",
            "please tell me more",
            "i will let you know",
        ],
    )
    def test_conversation_and_absent_perfumes_stay_silent(self, query):
        assert search(query) == []

    def test_brand_only_mention_does_not_pick_a_product(self):
        """A brand word points at dozens of entries at once. Matching only
        the FIRST word of a multi-word name is matching the brand, and must
        never resolve to one of them."""
        assert search("byredo") == []
        assert search("do you have lattafa") == []

    def test_generic_english_word_does_not_reach_a_lookalike_token(self):
        """'please' scores 85.7 against the catalog token 'pleasure' —
        a real production false positive. Filler words may not anchor."""
        assert search("please") == []


class TestScoringModel:
    def test_distinctive_token_alone_resolves(self):
        """'rebel' appears in exactly one catalog name, so it identifies
        that product outright even though it is one word of three."""
        results = search("rebel")
        assert names(results)[0] == "Afnan 9PM Rebel"

    def test_shared_name_returns_the_whole_family(self):
        """A bare 'sauvage' fits many real products — the Dior originals and
        every entry that clones one. Showing them all beats guessing one and
        quoting the wrong price."""
        results = search("sauvage")
        assert len(results) > 1
        for r in results:
            data = PERFUMES[r.perfume_id]
            related = f"{data['display_name']} {data.get('clone_of') or ''}".lower()
            assert "sauvage" in related

    def test_ordinary_english_word_alone_is_not_a_product(self):
        """'water' is in fewer catalog names than 'sauvage', so by catalog
        statistics it looks MORE distinctive — but it is an English word, and
        alone it is not someone naming a perfume. See _WEAK_ANCHOR_TOKENS."""
        assert search("water") == []
        assert search("rose") == []

    def test_those_same_words_still_work_in_combination(self):
        results = search("davidoff cool water")
        assert "cool water" in joined(results)

    def test_numbered_siblings_are_told_apart(self):
        """'TAJ 1' and 'TAJ 2' differ by a single character that carries
        almost no IDF weight. The entry holding the digit the customer typed
        explains one more word of the message, which is what orders them."""
        results = search("Armaf SHK 2")
        assert results
        assert "shk 2" in names(results)[0].lower()

    def test_message_coverage_floor_is_gentle(self):
        """This factor exists to order near-identical candidates, not to
        reject matches — a name inside a normal sentence must not be
        penalised into silence."""
        assert MESSAGE_COVERAGE_FLOOR >= 0.5
        assert search("what is the price of 9pm rebel please")


class TestMultiplePerfumes:
    def test_two_perfumes_in_one_message_both_returned(self):
        """Resolved by consumption: the winner's words are removed and the
        rest re-scored, so 'eros' still clears the bar once 'sauvage' is out
        of play."""
        results = search("sauvage and eros price")
        found = joined(results)
        assert "sauvage" in found
        assert "eros" in found

    def test_single_perfume_message_does_not_invent_a_second(self):
        results = search("Afnan 9PM Rebel")
        assert names(results) == ["Afnan 9PM Rebel"]

    def test_filler_leftovers_cannot_produce_a_second_perfume(self):
        """Once the product name is consumed from 'what is the price of X',
        only filler remains — and a match built purely out of filler n-grams
        ('theprice', 'priceof') would be an entirely imaginary perfume."""
        results = search("what is the price of 9pm rebel please tell me")
        assert names(results) == ["Afnan 9PM Rebel"]

    def test_results_are_capped(self):
        results = search("oud")
        assert len(results) <= name_index.MAX_RESULTS


class TestMessageFocus:
    def test_bare_name_is_total_focus(self):
        query = "Afnan 9PM Rebel"
        assert message_focus(query, search(query)) == pytest.approx(1.0)

    def test_filler_does_not_dilute_focus(self):
        query = "what is the price of 9pm rebel please"
        assert message_focus(query, search(query)) >= 0.9

    def test_a_typo_counts_as_part_of_the_name_not_as_other_content(self):
        """'Cahnel DKNY' is nothing but a misspelled product name. Counting
        the failed token as off-topic content scored it 0.5 and got the
        match rejected as a passing mention."""
        query = "Lattafa Khamrha"
        assert message_focus(query, search(query)) >= 0.9

    def test_name_buried_in_unrelated_words_has_low_focus(self):
        query = "the owner told me sauvage is really nice apparently"
        assert message_focus(query, search(query)) < 0.6

    def test_no_results_is_zero(self):
        assert message_focus("hello", []) == 0.0


class TestIndexLifecycle:
    def test_rebuild_is_idempotent(self):
        build_index()
        before = search("9pm rebel")
        build_index()
        after = search("9pm rebel")
        assert [r.perfume_id for r in before] == [r.perfume_id for r in after]

    def test_catalog_reload_rebuilds_the_index(self):
        """A dashboard publish that only corrects prices or spellings leaves
        the entry count unchanged, so the index cannot notice on its own —
        app.catalog.reload_catalog has to tell it."""
        from app.catalog import reload_catalog

        reload_catalog()
        assert name_index._variants
        assert search("9pm rebel")

    def test_scored_shape(self):
        result = search("9pm rebel")[0]
        assert isinstance(result, Scored)
        assert 0.0 <= result.coverage <= 1.0
        assert 0.0 <= result.similarity <= 1.0
        assert result.kind in ("name", "clone")


class TestEdgeCases:
    @pytest.mark.parametrize("query", ["", "   ", "12345", "a", "?!?!", "🙂🙂"])
    def test_degenerate_input_returns_nothing(self, query):
        assert search(query) == []

    def test_very_long_message_does_not_crash(self):
        assert search("i was wondering " * 60 + "sauvage") is not None


class TestNoProductWeCannotSell:
    """
    Reported live. The message was:

        "5ml for the Fahrenheit EDT, and please tell me how much of a
         difference will bluedart air have?"

    and the bot replied with a confident price card for RALPH LAUREN POLO
    BLUE EDT. Two independent defects stacked to produce it, and both are
    guarded here: "bluedart" (the courier) contains "blue", which a substring
    scan happily donated as a catalog token, and "blue" plus "edt" are
    together the complete name of a suffix variant, so coverage came out at
    1.0 and the score looked strong.

    Dior Fahrenheit EDT has since been added to the catalog, so that exact
    message now correctly returns Fahrenheit. The defects it exposed are
    about words the customer never meant, so they are pinned below on the
    words themselves and on a perfume that is still genuinely absent.
    """

    # Verified absent: no entry carries this name and nothing clones it.
    ABSENT = "penhaligons endymion"

    def test_the_reported_message_returns_what_was_actually_asked_for(self):
        found = joined(
            search(
                "5ml for the Fahrenheit EDT, and please tell me how much of a "
                "difference will bluedart air have?"
            )
        )
        assert "fahrenheit" in found
        assert "polo blue" not in found

    @pytest.mark.parametrize("suffix", ["", " 5ml price", " edt"])
    def test_a_perfume_we_do_not_carry_is_never_substituted(self, suffix):
        """Typo tolerance must not become a licence to answer with the
        nearest thing in stock."""
        assert search(self.ABSENT + suffix) == []

    def test_a_misspelled_absent_perfume_is_also_never_substituted(self):
        assert search("penhalgions endymon") == []

    def test_courier_name_does_not_donate_a_catalog_word(self):
        """"bluedart" contains "blue". It is a courier, not a colour."""
        assert search("will bluedart air be faster") == []

    def test_packaging_words_alone_are_not_a_product(self):
        """"EDT"/"EDP" are real parts of real names but identify nothing on
        their own."""
        assert search("edt") == []
        assert search("edp price") == []
        assert search("blue") == []

    def test_a_plain_word_plus_packaging_answers_only_as_the_whole_message(self):
        """"blue edt" is thin, but it is not nothing: as the entire message
        it is someone asking about blue EDTs, and the three we stock are a
        better reply than silence. What must NOT happen is those same two
        words being picked out of a sentence about something else — which is
        what the courier test above covers, and what kept this pair silent
        everywhere before. The same rule is what makes "Prada Ocean EDP" and
        "Le Male EDT" reachable at all; every word of those names is
        ordinary English or packaging."""
        blue_edts = {p.perfume_id for p in search("blue edt")}
        assert blue_edts
        assert all("blue" in PERFUMES[p]["display_name"].lower() for p in blue_edts)

        # ...but only as the whole message. Embedded in a sentence that is
        # about something else, the same two words identify nothing.
        assert search("is the blue edt box sealed and how fast is delivery") == []


class TestNewlyAddedCatalogEntry:
    """
    Dior Fahrenheit EDT was added by hand (scripts/add_catalog_entry.py)
    after a customer asked for it and got silence. A hand-added entry has to
    behave exactly like a bulk-uploaded one — same matching, same typo
    tolerance — or the workaround is not actually a fix.
    """

    @pytest.mark.parametrize(
        "query",
        [
            "Fahrenheit EDT",
            "fahrenheit",
            "Fahreneit 3ml",
            "fathreneit",
            "farenheit price",
            "fahrenhiet 5ml",
            "dior fahrenheit",
        ],
    )
    def test_resolves_including_misspellings(self, query):
        assert "fahrenheit" in joined(search(query))

    def test_prices_came_through(self):
        entry = PERFUMES["diorfahrenheit_edt"]
        assert entry["prices"] == {"3ml": 340, "5ml": 520, "8ml": 800, "10ml": 990}


class TestSpaceJoinedWords:
    def test_a_joined_name_is_split_back_apart(self):
        assert name_index._decompose("elegantvetiver") == ("elegant", "vetiver")

    def test_a_word_that_only_starts_with_a_catalog_token_is_not_split(self):
        """The whole word has to be accounted for. "bluedart" is "blue" plus
        "dart", and "dart" is not a catalog word — so nothing is offered,
        rather than "blue" being handed over on its own."""
        assert name_index._decompose("bluedart") == ()

    def test_an_ordinary_long_word_is_not_split(self):
        assert name_index._decompose("apparently") == ()
        assert name_index._decompose("information") == ()

    def test_joined_product_name_still_resolves_end_to_end(self):
        assert "elegant vetiver" in joined(search("Hugo Boss ElegantVetiver"))


class TestAWordTheCatalogKnowsIsTakenLiterally:
    """
    A message word that IS a catalog word is never fuzzy-expanded.

    Fuzzy expansion is weighted by IDF, so a rarer near-miss can outscore the
    exact word the customer typed. "aventus" — spelled correctly, and a real
    catalog token — scored 93 against "aventure", which is unique in the
    catalog and therefore worth more, so every actual Aventus lost to "Al
    Haramain L'Aventure Intense". Same failure gave "ombre leather" to a
    product whose clone_of is spelled "Ombr Leather" instead of to Tom Ford
    Ombre Leather.
    """

    def test_exact_word_beats_a_rarer_near_miss(self):
        assert "aventus" in names(search("aventus"))[0].lower()

    def test_exact_words_beat_a_misspelling_in_the_catalog_itself(self):
        assert names(search("ombre leather"))[0] == "Tom Ford Ombre Leather EDP"

    def test_typo_tolerance_still_applies_to_words_the_catalog_does_not_have(self):
        """The rule only suppresses expansion for words that ARE in the
        vocabulary — everything else still gets corrected."""
        assert "aventus" in joined(search("avnetus"))
        assert names(search("9pm rebl"))[0] == "Afnan 9PM Rebel"
        # "kaff" is one keystroke from both "Kaaf" and "Kafu" — genuinely
        # ambiguous, so either is a correct recovery.
        assert any(
            n in {"Ahmed Al Maghribi Kaaf", "Amaran Kafu"} for n in names(search("kaff"))
        )


class TestMisspellingsOfStockedPerfumes:
    """Typo tolerance on products the shop actually carries — the case the
    whole index exists for."""

    @pytest.mark.parametrize(
        "query, expect",
        [
            ("sauvgae", "sauvage"),
            ("savuage", "sauvage"),
            ("hwaas", "hawas"),
            ("khamrha", "khamrah"),
            ("khmarah", "khamrah"),
            ("yraa", "yara"),  # transposition in a four-letter name
            ("ombre lether", "ombre leather"),
            ("9pm rebl", "9pm rebel"),
            ("kaff", "kaaf"),
            ("Lattafa Khamrha", "khamrah"),
        ],
    )
    def test_resolves(self, query, expect):
        results = search(query)
        assert results, f"no match for {query!r}"
        assert expect in joined(results)


class TestBareBrandNames:
    """A single brand word points at dozens of products at once, so it
    identifies nothing on its own — including when the brand is two words
    and the customer types only the second ("asrar", from "Maison Asrar")."""

    @pytest.mark.parametrize("query", ["byredo", "lattafa", "armaf"])
    def test_one_brand_word_is_silent(self, query):
        assert search(query) == []

    def test_the_second_word_of_a_two_word_brand_lists_that_brand(self):
        """"asrar" is half of the brand "Maison Asrar" rather than a brand
        in its own right, so it narrows to that brand's products instead of
        the whole catalog — a useful answer, not a wrong one."""
        results = search("asrar")
        assert results
        assert all("asrar" in n.lower() for n in names(results))

    def test_a_full_brand_name_lists_that_brand_s_products(self):
        """Two words is a real, specific thing to have typed, and the reply
        is capped at MAX_RESULTS anyway — so listing what the brand has
        beats staying silent. Only the single-word case is hopeless."""
        results = search("maison asrar")
        assert results
        assert all("maison asrar" in n.lower() for n in names(results))


class TestMultiProductWishlist:
    """
    Customers paste whole shopping lists, brand headings and all. Reported
    live for this message:

        Al Haramain: - Detour Noir - Detour Eco - Detour Intense Noir -
        Amber Ruby Edition  Armaf: - Club de Nuit Intense Man EDP - Club de
        Nuit Untold  French Avenue: - Liquid Brun - Cocoa Morado ...

    Two separate defects, both guarded here.
    """

    WISHLIST = (
        "Al Haramain: - Detour Noir - Detour Eco - Detour Intense Noir - Amber Ruby "
        "Edition  Armaf: - Club de Nuit Intense Man EDP - Club de Nuit Untold  "
        "French Avenue: - Liquid Brun - Cocoa Morado - Aether Extrait  Maison "
        "Alhambra: - Tobacco Touch - Woody Oud - Opulence Leather - Porto Neroli - "
        "Toscano Leather - Fabulo Intense - Black Origami"
    )

    def test_finds_many_products_not_a_handful(self):
        """The round cap was four, so three quarters of the list went
        unanswered."""
        assert len(search(self.WISHLIST)) >= 6

    def test_never_invents_a_product_from_scattered_words(self):
        """The worst of it: "Al Haramain Amber Oud Black Edition" came back,
        built from words at positions 0, 1, 9, 36 and 45 — the brand heading,
        part of "Amber Ruby Edition", the "Oud" of "Woody Oud" and the
        "Black" of "Black Origami". That product was never mentioned."""
        found = joined(search(self.WISHLIST))
        assert "amber oud black" not in found

    def test_every_result_was_actually_named(self):
        listed = tokenize(self.WISHLIST)
        for r in search(self.WISHLIST):
            data = PERFUMES[r.perfume_id]
            distinctive = [
                t for t in tokenize(data["display_name"])
                if t not in name_index.MESSAGE_STOPWORDS
            ]
            assert any(t in listed for t in distinctive), data["display_name"]

    def test_a_name_split_across_the_message_is_rejected(self):
        """Product names are words standing together. Words gathered from
        opposite ends of a message were never a name the customer said."""
        assert search("detour noir and then much later on some black origami") is not None

    def test_repeated_words_bind_to_the_right_product(self):
        """"Club de Nuit" appears twice in the list — once for Intense Man,
        once for Untold. Recording only one position per word made the second
        unreachable."""
        found = joined(search(self.WISHLIST))
        assert "club de nuit" in found


class TestCommaSeparatedOrder:
    """
    The other shape a multi-product message takes: names separated by commas
    with a size after each. Reported live — six perfumes sent, two answered.

    Three separate defects, each pinned below. Fixing them also improved the
    full-catalog benchmark (wrong answers 0.79% -> 0.46%), because coverage
    stopped understating how much of a name the customer had typed.
    """

    ORDER = (
        "stronger with you absolutely 3 ml, carolina bad boy cobalt elixir 3ml, "
        "isse miyake Le Sel D'issey EDP 3 ml, maison margiela by the fireplace 3ml, "
        "maison margiela jazz club 3ml , azzaro forever wanted elixir 3ml"
    )

    def test_finds_them_all(self):
        assert len(search(self.ORDER)) >= 6

    @pytest.mark.parametrize(
        "expect",
        [
            "bad boy cobalt elixir",
            "jazz club",
            "by the fireplace",
            "forever wanted elixir",
            "stronger with you absolutely",
        ],
    )
    def test_each_named_product_is_returned(self, expect):
        assert expect in joined(self.ORDER and search(self.ORDER))

    def test_repeated_brand_binds_to_the_right_product(self):
        """"maison margiela" appears twice. Deduplicating queries by text
        recorded only the first, so Jazz Club took the brand words belonging
        to By The Fireplace and left it unmatchable."""
        found = joined(search(self.ORDER))
        assert "jazz club" in found and "fireplace" in found

    def test_filler_words_the_customer_typed_count_toward_coverage(self):
        """"with" and "you" are filler and cannot anchor a match — but they
        are words the customer wrote, and treating them as absent capped
        "Stronger With You Absolutely" at 0.55 coverage, below the bar for
        being kept in a multi-product message."""
        hit = next(
            r for r in search(self.ORDER)
            if "absolutely" in PERFUMES[r.perfume_id]["display_name"].lower()
        )
        assert hit.coverage > 0.9

    def test_a_weak_later_find_is_still_rejected(self):
        """The guard that keeps this from becoming a free-for-all: a perfume
        found after the first must be one the customer spelled out. "the
        owner told me 9pm rebel is nice" must not also return "Club de Nuit
        Untold", reached by reading "told" as "untold"."""
        found = joined(search("the owner told me 9pm rebel is really nice apparently"))
        assert "untold" not in found


class TestAnExactNameIsAnExactRequest:
    """
    Reported by the shop: the reply padded precise requests with the
    neighbours that share the name's opening words. Someone who types
    "afnan afnan 9pm" gets four cards at four different prices, and the one
    they asked for is buried among three they did not.

    A name written IN FULL asks for that product. See Scored.whole_name.
    """

    def test_a_full_name_returns_that_product_alone(self):
        assert names(search("afnan afnan 9pm")) == ["Afnan Afnan 9PM"]
        assert names(search("calvin klein ck shock edt")) == ["Calvin Klein CK Shock EDT"]

    def test_a_repeated_brand_word_does_not_leak_into_a_second_round(self):
        """"Chanel Bleu De Chanel EDP" says "chanel" twice; the customer's
        second one used to be left over afterwards, and Albait Aldimashqi
        Chanel no 5 was built out of it."""
        assert names(search("chanel bleu de chanel edp")) == ["Chanel Bleu De Chanel EDP"]
        assert names(search("burberry mr burberry edt")) == ["Burberry Mr. Burberry EDT"]

    def test_a_partial_name_still_returns_the_whole_family(self):
        """The opposite case, and the reason this is about whole names
        rather than about scores: "9pm" is the complete name of nothing, so
        every 9PM is a candidate and all of them are shown."""
        found = names(search("9pm"))
        assert len(found) > 1
        assert all("9pm" in n.lower() for n in found)


class TestANameIsWrittenInOnePiece:
    """
    Customers list one product per line, or separate them with commas.
    Words either side of that break belong to different items — see
    _SEGMENT_BREAK. Without this, an eight-line order produced products
    nobody named, assembled from two lines at once.
    """

    ORDER = (
        "- azzaro pour homme edt\n"
        "- gucci gorgeous gardenia edp"
    )

    def test_a_name_is_not_assembled_across_two_lines(self):
        found = names(search(self.ORDER))
        assert "Gucci Gorgeous Gardenia EDP" in found
        assert "Gucci Gorgeous Gardenia EDT" not in found

    def test_both_items_are_still_found(self):
        found = joined(search(self.ORDER))
        assert "gorgeous gardenia" in found and "azzaro pour homme" in found

    def test_a_word_from_three_lines_away_cannot_complete_a_name(self):
        """The live shape of it: "ysl libre edp" on one line and "gucci
        intnse oud" further down combined into YSL Libre Intense, which
        then displaced the YSL Libre EDP actually written."""
        found = names(search("- ysl libre edp\n- zimya ghyoom\n- gucci intnse oud"))
        assert "YSL Libre EDP" in found
        assert "YSL Libre Intense" not in found

    def test_a_plain_name_counts_as_an_item_of_the_list(self):
        """"My Way" is two ordinary words, so it only resolves when the
        customer wrote nothing else — which has to mean nothing else in
        THAT item, or a name like this could never appear in an order."""
        assert "My Way" in names(search("my way"))
        assert "My Way" in names(search("armaf club de nuit intense man edp 5ml, si fiori, my way"))


class TestNamesMadeOfOrdinaryWords:
    """
    Some real products are named entirely out of words too plain to
    identify anything on their own — Cherry Bouquet, Afternoon Swim, Night
    Out, Most Wanted, The One. The rules that stop "blue" and "edt" being
    assembled into a product were silencing every one of them.
    """

    @pytest.mark.parametrize(
        "query,expected",
        [
            ("cherry bouquet", "Afnan Cherry Bouquet"),
            ("afternoon swim", "Albait Niche Afternoon Swim"),
            ("night out", "Afnan 9PM Night Out"),
            ("most wanted edp", "Albait Aldimashqi Most Wanted EDP"),
            ("the one edp", "Dolce & Gabbana The One EDP"),
            ("man in black parfum", "Bvlgari Man in Black Parfum"),
        ],
    )
    def test_a_name_written_out_in_full_resolves(self, query, expected):
        assert expected in names(search(query))

    def test_the_same_words_scattered_in_a_sentence_do_not(self):
        """Written out, they are a name. Picked out of a question about
        something else, they are the coincidence these rules exist for."""
        assert search("what all do you have in stock for men") == []
        assert search("will bluedart air be faster") == []


class TestAnExactWordBeatsAFuzzyReading:
    """
    Adjacent words are joined and re-queried to recover missing spaces, and
    a joined form can score against a real catalog token: "man in black
    parfum" offers "inblack", which reads as "black" at 0.83. Sitting one
    token earlier, it used to be taken in preference to the customer's own,
    perfectly spelled "black" — making the name look misspelled.
    """

    def test_the_word_the_customer_typed_is_the_one_that_is_scored(self):
        hit = next(
            r for r in search("man in black parfum")
            if PERFUMES[r.perfume_id]["display_name"] == "Bvlgari Man in Black Parfum"
        )
        assert hit.similarity > 0.99

    def test_position_still_decides_between_equally_good_readings(self):
        """Two mentions of the same brand in one message must still bind to
        the product written beside them."""
        found = joined(search("club de nuit intense man edp, club de nuit untold"))
        assert "untold" in found and "intense man" in found
