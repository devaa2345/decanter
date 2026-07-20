"""
Unit tests for the order-confirmation message detector.

Covers:
- The real template (order link present) -> detected
- Variations on line items/totals/order numbers -> still detected via the link
- The link-less fallback (phrase + order number both required)
- Real customer messages that must NOT be caught (price questions, generic
  "order" mentions like delivery-status questions)
"""

from app.order_confirmation import is_order_confirmation


class TestOrderLinkSignal:
    """The order-link URL is treated as conclusive on its own."""

    def test_real_template_message(self):
        message = (
            "Hi Sovereign Scents! 👑 I'd like to confirm my order:\n\n"
            "🧾 *Order #SS1784385515072433*\n\n"
            "1. Lancôme Idole Peach 'N Roses | 3ml × 1 = ₹290\n\n"
            "💰 *Total: ₹370*\n\n"
            "📋 Order link: https://www.sovereignscents.in/admin/order/"
            "030a84473e802c7c6cd8f9efea879d86b176390c36511a5ff4a4d9146cd42381"
        )
        assert is_order_confirmation(message) is True

    def test_different_order_and_items_still_detected(self):
        message = (
            "Hi Sovereign Scents! I'd like to confirm my order:\n"
            "Order #SS9999999999\n"
            "1. Dior Sauvage | 10ml x 2 = 780\n"
            "2. Bleu de Chanel | 5ml x 1 = 210\n"
            "Total: 990\n"
            "Order link: https://www.sovereignscents.in/admin/order/abc123"
        )
        assert is_order_confirmation(message) is True

    def test_link_alone_is_conclusive(self):
        assert is_order_confirmation(
            "sovereignscents.in/admin/order/xyz"
        ) is True

    def test_link_case_insensitive(self):
        assert is_order_confirmation(
            "SOVEREIGNSCENTS.IN/ADMIN/ORDER/xyz"
        ) is True


class TestFallbackSignal:
    """Without the link, both the phrase AND an order number are required."""

    def test_phrase_and_order_number_without_link(self):
        assert is_order_confirmation("I'd like to confirm my order, Order #SS12345") is True

    def test_phrase_alone_is_not_enough(self):
        # No order number/link — nothing to actually confirm, let it fall through.
        assert is_order_confirmation("I want to confirm my order please") is False

    def test_order_number_alone_is_not_enough(self):
        assert is_order_confirmation("Order #SS12345 what does this mean?") is False


class TestRealCustomerMessagesNotCaught:
    def test_price_question(self):
        assert is_order_confirmation("how much for sauvage 10ml") is False

    def test_delivery_status_question(self):
        assert is_order_confirmation("order kab aayega") is False

    def test_greeting(self):
        assert is_order_confirmation("hello bro") is False

    def test_empty_message(self):
        assert is_order_confirmation("") is False
        assert is_order_confirmation(None) is False

    def test_generic_order_word_without_confirm_phrase(self):
        assert is_order_confirmation("can I place an order for 3ml sauvage") is False
