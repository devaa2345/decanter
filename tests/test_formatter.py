"""
Unit tests for the reply text formatter.

Verifies card output matches expected format:
- Bold perfume name
- 2-column price layout
- Comma-formatted prices >= 1000
- Shipping card appended
"""

import pytest

from app.formatter import (
    AMBIGUOUS_MESSAGE,
    CATALOG_SHEET_URL,
    FALLBACK_MESSAGE,
    NON_TEXT_MESSAGE,
    WILL_CONTACT_LINE,
    build_multi_price_card,
    build_price_card,
)
from app.catalog import PERFUMES, SHIPPING_CARD


class TestBuildPriceCard:
    """Test price card formatting."""

    def test_valid_perfume_has_plain_name_no_asterisks(self):
        """Card should contain the display name (uppercased for a header
        effect — see build_price_card) as plain text — no asterisks.
        WhatsApp itself renders *word* as bold, but Chat Mitra's send API
        rejects the literal "*" character outright ("Text contains invalid
        characters"), confirmed via direct API tests: a lone "*" alone was
        enough to trigger it, nothing to do with pairing/emoji."""
        pid = next(iter(PERFUMES))
        card = build_price_card(pid)
        display_name = PERFUMES[pid]["display_name"]
        assert display_name.upper() in card
        assert "*" not in card

    def test_valid_perfume_has_shipping(self):
        """Shipping card should be appended to every price card."""
        pid = next(iter(PERFUMES))
        card = build_price_card(pid)
        assert "Prepaid only" in card
        assert "₹65 - Delhi NCR" in card
        assert "₹80 - Rest of India" in card

    def test_valid_perfume_has_prices(self):
        """Card should contain price values with ₹ symbol."""
        pid = next(iter(PERFUMES))
        card = build_price_card(pid)
        assert "₹" in card

    def test_comma_formatting_large_prices(self):
        """Prices >= 1000 should have comma separators."""
        # Find a perfume with a price >= 1000
        for pid, data in PERFUMES.items():
            for size, price in data["prices"].items():
                if price >= 1000:
                    card = build_price_card(pid)
                    assert f"₹{price:,}" in card
                    return
        pytest.skip("No perfume with price >= 1000 in catalog")

    def test_unknown_perfume_returns_fallback(self):
        """Unknown perfume ID should return fallback message."""
        card = build_price_card("nonexistent_perfume_xyz")
        assert card == FALLBACK_MESSAGE

    def test_card_is_single_string(self):
        """Card should be a single string (not a list or tuple)."""
        pid = next(iter(PERFUMES))
        card = build_price_card(pid)
        assert isinstance(card, str)

    def test_card_starts_with_uppercase_name_no_emoji_or_asterisks(self):
        """Uppercased name as a header effect (no markdown/emoji risk), no
        leading emoji (🌸 was suspected before "*" was isolated as the
        actual cause) and no asterisks anywhere (the confirmed cause)."""
        pid = next(iter(PERFUMES))
        card = build_price_card(pid)
        display_name = PERFUMES[pid]["display_name"]
        assert card.startswith(display_name.upper())
        for risky_char in ("🌸", "🚚", "|", "*"):
            assert risky_char not in card

    def test_size_labels_present(self):
        """Card should contain size tier labels."""
        pid = next(iter(PERFUMES))
        prices = PERFUMES[pid]["prices"]
        card = build_price_card(pid)
        for size in ["3ml", "5ml", "8ml", "10ml"]:
            if size in prices:
                assert size in card

    def test_ends_with_will_contact_line(self):
        """Every price reply should close with the 'we'll contact you' line,
        after the shipping card."""
        pid = next(iter(PERFUMES))
        card = build_price_card(pid)
        assert card.endswith(WILL_CONTACT_LINE)
        assert card.index(SHIPPING_CARD) < card.index(WILL_CONTACT_LINE)


class TestBuildMultiPriceCard:
    """
    When 2+ perfumes are found in one message — whether the customer
    clearly asked about multiple distinct products (e.g. "sauvage and eros
    price") or a single mention was ambiguous among close variants (e.g.
    bare "9pm") — the reply must show a FULL price card for each one, not
    just a name + price range, and not silently pick only one.
    """

    def test_shows_a_full_price_card_for_each_perfume(self):
        pids = list(PERFUMES)[:2]
        card = build_multi_price_card(pids)
        for pid in pids:
            display_name = PERFUMES[pid]["display_name"]
            assert display_name.upper() in card
            for size, price in PERFUMES[pid]["prices"].items():
                assert f"₹{price:,}" in card

    def test_perfumes_appear_in_the_given_order(self):
        pids = list(PERFUMES)[:3]
        card = build_multi_price_card(pids)
        idx = [card.index(PERFUMES[pid]["display_name"].upper()) for pid in pids]
        assert idx == sorted(idx)

    def test_shipping_card_and_closing_appear_only_once(self):
        """One shared shipping card + closing line at the end, not repeated
        per perfume — keeps a multi-perfume reply from ballooning."""
        pids = list(PERFUMES)[:3]
        card = build_multi_price_card(pids)
        assert card.count("Prepaid only") == 1
        assert card.count(WILL_CONTACT_LINE) == 1

    def test_none_falls_back_to_plain_ambiguous_message(self):
        assert build_multi_price_card(None) == AMBIGUOUS_MESSAGE

    def test_empty_list_falls_back_to_plain_ambiguous_message(self):
        assert build_multi_price_card([]) == AMBIGUOUS_MESSAGE

    def test_truncates_beyond_the_cap_with_a_count(self):
        many_pids = list(PERFUMES)[:15]
        card = build_multi_price_card(many_pids)
        assert "more" in card.lower()
        # Only the capped number of names should actually appear, not all 15
        shown_names = sum(
            1 for pid in many_pids if PERFUMES[pid]["display_name"].upper() in card
        )
        assert shown_names < len(many_pids)

    def test_opening_prepended_and_closing_used_when_given(self):
        pids = list(PERFUMES)[:2]
        card = build_multi_price_card(pids, opening="Found a couple!", closing="Let us know!")
        assert card.startswith("Found a couple!")
        assert card.endswith("Let us know!")

    def test_no_asterisks_or_untested_characters(self):
        pids = list(PERFUMES)[:3]
        card = build_multi_price_card(pids)
        assert "*" not in card
        for risky_char in ("🌸", "🚚", "|", "#"):
            assert risky_char not in card


class TestFixedMessages:
    """Test fixed reply messages."""

    def test_fallback_message(self):
        assert "perfume" in FALLBACK_MESSAGE.lower() or "🙂" in FALLBACK_MESSAGE

    def test_fallback_message_includes_catalog_link_and_prompt(self):
        assert CATALOG_SHEET_URL in FALLBACK_MESSAGE
        assert "quantity" in FALLBACK_MESSAGE.lower()

    def test_ambiguous_message(self):
        assert "more than one" in AMBIGUOUS_MESSAGE.lower()

    def test_non_text_message(self):
        assert "type" in NON_TEXT_MESSAGE.lower()
