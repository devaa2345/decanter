"""
Unit tests for the greeting/catalog-request detector (app/greeting.py).

Called only after the perfume matcher already found nothing — its job is
to decide which of those leftover messages are worth a reply at all
(silence-by-default otherwise, see app/main.py).
"""

from app.greeting import is_catalog_request, is_greeting_or_catalog_request


class TestGreetings:
    def test_bare_hi(self):
        assert is_greeting_or_catalog_request("hi") is True

    def test_hello(self):
        assert is_greeting_or_catalog_request("hello") is True

    def test_hey(self):
        assert is_greeting_or_catalog_request("hey") is True

    def test_greeting_typo_variants(self):
        for msg in ("hii", "hiii", "helo", "hlo", "heya"):
            assert is_greeting_or_catalog_request(msg) is True

    def test_greeting_with_trailing_text(self):
        assert is_greeting_or_catalog_request("hi there, how are you") is True
        assert is_greeting_or_catalog_request("hello im looking for something") is True

    def test_good_morning_phrase(self):
        assert is_greeting_or_catalog_request("good morning") is True

    def test_case_insensitive(self):
        assert is_greeting_or_catalog_request("HELLO") is True
        assert is_greeting_or_catalog_request("Hi There") is True

    def test_greeting_not_as_first_word_does_not_count(self):
        """Only counts as a greeting-opener if it's how the message starts —
        avoids "thanks and hello to your team" type messages over-firing."""
        assert is_greeting_or_catalog_request("thanks and hello to your team") is False


class TestCatalogRequests:
    def test_catalog_word(self):
        assert is_greeting_or_catalog_request("can you send catalog") is True

    def test_price_list(self):
        assert is_greeting_or_catalog_request("please send price list") is True

    def test_what_do_you_have(self):
        assert is_greeting_or_catalog_request("what do you have") is True

    def test_menu(self):
        assert is_greeting_or_catalog_request("send menu") is True

    def test_all_products(self):
        assert is_greeting_or_catalog_request("show me all products") is True


class TestIsCatalogRequest:
    """
    is_catalog_request is the narrower, catalog-phrases-only check used as
    an early veto in app.main — must catch the exact real-world spelling
    variants that were confirmed getting hijacked into a wrong perfume's
    price card, and must NOT fire on a bare greeting (that stays through
    the normal Groq/fuzzy-tolerant pipeline instead).
    """

    def test_catalogue_british_spelling(self):
        """The exact reported production bug: 'catalogue' was getting a
        random perfume's price card instead of the catalog link."""
        assert is_catalog_request("catalogue") is True

    def test_catalog_american_spelling(self):
        assert is_catalog_request("catalog") is True

    def test_send_me_the_catalogue_please(self):
        assert is_catalog_request("send me the catalogue please") is True

    def test_case_insensitive(self):
        assert is_catalog_request("CATALOGUE") is True
        assert is_catalog_request("Catalogue") is True

    def test_price_list_phrase(self):
        assert is_catalog_request("please send price list") is True

    def test_bare_greeting_is_not_a_catalog_request(self):
        """Deliberately excludes greeting words — "hi" is a common opener
        before naming a (possibly misspelled) perfume in the same message,
        so it must stay on the full Groq/fuzzy-tolerant pipeline rather
        than being vetoed this early."""
        assert is_catalog_request("hi") is False
        assert is_catalog_request("hello") is False
        assert is_catalog_request("good morning") is False

    def test_unrelated_message_is_not_a_catalog_request(self):
        assert is_catalog_request("thanks bhai") is False
        assert is_catalog_request("order kab aayega") is False

    def test_empty_message(self):
        assert is_catalog_request("") is False
        assert is_catalog_request(None) is False


class TestNotGreetingOrCatalogRequest:
    """These must stay silent per the new default — none of them are a
    greeting or an explicit catalog ask."""

    def test_thank_you(self):
        assert is_greeting_or_catalog_request("thanks bhai") is False

    def test_delivery_status_question(self):
        assert is_greeting_or_catalog_request("order kab aayega") is False

    def test_shipping_question(self):
        assert is_greeting_or_catalog_request("how much is shipping") is False

    def test_random_gibberish(self):
        assert is_greeting_or_catalog_request("asdfghjkl random nonsense xyz") is False

    def test_are_you_open(self):
        assert is_greeting_or_catalog_request("are you open today") is False

    def test_empty_message(self):
        assert is_greeting_or_catalog_request("") is False
        assert is_greeting_or_catalog_request(None) is False

    def test_whitespace_only(self):
        assert is_greeting_or_catalog_request("   ") is False
