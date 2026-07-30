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
    WELCOME_MESSAGE,
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


class TestBuildPriceCardWithRequestedSize:
    """
    build_price_card's requested_ml param (parsed from the customer's
    message by app.matcher.extract_requested_size_ml) — showing ONLY the
    size they named, with delivery cost added per region and the grand
    total shown alongside it, instead of the full size grid.
    """

    # Real catalog id — has 3/5/8/10/20/30ml decant tiers + a 100ml_full,
    # all distinct prices (150/210/310/390/700/950/2800).
    PID = "afnan9pm_rebel"

    def test_requested_size_present_shows_only_that_size(self):
        card = build_price_card(self.PID, requested_ml=3)
        prices = PERFUMES[self.PID]["prices"]
        assert f"3ml  ₹{prices['3ml']:,}" in card
        assert f"₹{prices['5ml']:,}" not in card
        assert f"₹{prices['10ml']:,}" not in card
        assert "Full 100ml" not in card

    def test_requested_size_adds_delivery_and_grand_total_per_region(self):
        card = build_price_card(self.PID, requested_ml=3)
        price = PERFUMES[self.PID]["prices"]["3ml"]
        assert "Delivery + grand total:" in card
        assert f"₹65 - Delhi NCR - Total ₹{price + 65:,}" in card
        assert f"₹80 - Rest of India - Total ₹{price + 80:,}" in card
        assert f"₹100 - J&K, NE, Lakshadweep & Andaman - Total ₹{price + 100:,}" in card

    def test_requested_size_replaces_generic_shipping_card(self):
        """The generic no-total shipping card shouldn't also appear
        alongside the per-region grand totals — one or the other."""
        card = build_price_card(self.PID, requested_ml=3)
        assert SHIPPING_CARD not in card

    def test_requested_size_not_offered_falls_back_to_full_grid(self):
        """This perfume doesn't come in 9999ml — falls back to showing
        every size it DOES offer, same as if no size had been named,
        rather than a dead end."""
        card = build_price_card(self.PID, requested_ml=9999)
        assert card == build_price_card(self.PID)

    def test_no_requested_size_is_unaffected(self):
        """Sanity: omitting requested_ml behaves exactly as before."""
        card = build_price_card(self.PID)
        assert "Delivery + grand total:" not in card
        assert SHIPPING_CARD in card

    def test_full_bottle_size_requested(self):
        card = build_price_card(self.PID, requested_ml=100)
        price = PERFUMES[self.PID]["prices"]["100ml_full"]
        assert f"Full 100ml  ₹{price:,}" in card
        assert f"₹{PERFUMES[self.PID]['prices']['3ml']:,}" not in card


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


class TestBuildMultiPriceCardWithRequestedSize:
    """
    build_multi_price_card's requested_ml param — every candidate that
    offers the requested size gets one compact line, and they're all
    combined into ONE subtotal + one shared delivery/grand-total block
    (see its docstring). A candidate that doesn't offer that size gets a
    brief note instead of its full grid or being silently dropped. Only
    falls back to the full comparison grid when NONE of the candidates
    offer the requested size at all.

    Regression coverage for the real production bug: "Rare Carbon 3ml"
    matched 5 largely unrelated candidates, and because one of them
    (Ajmal Carbon EDP) only comes as a full bottle, the old all-or-nothing
    logic dumped the FULL size grid for every one of the 5 instead of
    just noting that one candidate and answering cleanly for the rest.
    """

    # Real "9pm" family ids (see TestNinePMFamilyAmbiguity) — all four have
    # 3/5/8/10/20/30ml, but only 3 of the 4 have a 100ml_full (afnanafnan_9pm
    # doesn't), which is exactly the partial-availability case below.
    FAMILY = [
        "afnan9pm_rebel",
        "afnanafnan_9pm",
        "afnan9pm_night_out",
        "afnan9pm_elixir_parfum",
    ]

    def test_all_candidates_have_size_shows_one_combined_total(self):
        card = build_multi_price_card(self.FAMILY, requested_ml=3)
        total = 0
        for pid in self.FAMILY:
            price = PERFUMES[pid]["prices"]["3ml"]
            total += price
            assert f"3ml  ₹{price:,}" in card
        assert f"Subtotal  ₹{total:,}" in card
        assert f"Total ₹{total + 65:,}" in card  # Delhi NCR region
        assert "Delivery + grand total:" in card
        assert SHIPPING_CARD not in card
        # No full grid anywhere - only the requested 3ml line per candidate,
        # never any of the other tiers these perfumes also come in.
        assert "5ml " not in card
        assert "8ml " not in card
        assert "10ml " not in card
        assert "20ml " not in card
        assert "30ml " not in card

    def test_partial_availability_combines_the_available_ones_and_notes_the_rest(self):
        """afnanafnan_9pm has no 100ml_full — requesting 100ml for the
        whole family must still total the 3 that DO offer it, and just note
        that afnanafnan_9pm doesn't, instead of falling back to the full
        grid for every candidate (the exact live bug this replaced)."""
        card = build_multi_price_card(self.FAMILY, requested_ml=100)
        priced_pids = [pid for pid in self.FAMILY if pid != "afnanafnan_9pm"]
        total = sum(PERFUMES[pid]["prices"]["100ml_full"] for pid in priced_pids)

        assert f"Subtotal  ₹{total:,}" in card
        assert "Delivery + grand total:" in card
        assert "AFNAN AFNAN 9PM - NOT AVAILABLE IN 100ML" in card.upper()
        assert card != build_multi_price_card(self.FAMILY)

    def test_no_candidate_has_the_size_falls_back_to_full_grid(self):
        """None of the family comes in an absurd 47ml - nothing to total,
        so this falls back to the full grid for all, same as no size named."""
        card = build_multi_price_card(self.FAMILY, requested_ml=47)
        assert card == build_multi_price_card(self.FAMILY)

    def test_no_requested_size_is_unaffected(self):
        card = build_multi_price_card(self.FAMILY)
        assert "Delivery + grand total:" not in card
        assert SHIPPING_CARD in card


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


class TestWelcomeMessage:
    """
    Sent once, on a sender's very first-ever message (see
    app.analytics.is_first_contact / app.main's first-contact branch).
    Character-safety rules (module docstring): no "*" (Chat Mitra's send
    API rejects the whole message outright) and no "#" fragment in a URL.
    """

    def test_no_asterisks(self):
        assert "*" not in WELCOME_MESSAGE

    def test_no_hash_fragment_in_url(self):
        assert "#" not in WELCOME_MESSAGE

    def test_no_markdown_link_brackets(self):
        assert "[" not in WELCOME_MESSAGE
        assert "]" not in WELCOME_MESSAGE

    def test_uses_the_fragment_free_catalog_url(self):
        assert CATALOG_SHEET_URL in WELCOME_MESSAGE

    def test_mentions_business_name_and_sheet_tabs(self):
        assert "Sovereign Scents" in WELCOME_MESSAGE
        assert "Men's / Unisex" in WELCOME_MESSAGE
        assert "Women's" in WELCOME_MESSAGE
        assert "Decant Pictures" in WELCOME_MESSAGE
        assert "Customer Reviews" in WELCOME_MESSAGE

    def test_includes_order_info_and_shipping_rates(self):
        assert "No Minimum Order Value" in WELCOME_MESSAGE
        assert "₹80" in WELCOME_MESSAGE
        assert "₹1000" in WELCOME_MESSAGE
        assert "Bluedart Air" in WELCOME_MESSAGE
        assert "5%" in WELCOME_MESSAGE

    def test_includes_community_link_and_contact_details(self):
        assert "https://chat.whatsapp.com/DYFroG1QrLC54DZUrBaD61" in WELCOME_MESSAGE
        assert "Himanshu" in WELCOME_MESSAGE
        assert "8826709398" in WELCOME_MESSAGE
        assert "After 9PM" in WELCOME_MESSAGE
