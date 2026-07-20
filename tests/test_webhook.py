"""
Integration tests for the webhook endpoint.

Uses FastAPI TestClient to simulate Chat Mitra webhook payloads.
Mocks the Chat Mitra send-message call to avoid real API calls.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.dedup import dedup_cache
from app.main import app


@pytest.fixture(autouse=True)
def clear_dedup():
    """Clear dedup cache before each test."""
    dedup_cache.clear()
    yield
    dedup_cache.clear()


@pytest.fixture
def client():
    """FastAPI test client."""
    return TestClient(app)


def _make_webhook_payload(
    message_text: str,
    sender: str = "919876543210",
    message_id: str | None = None,
    message_type: str = "text",
    event: str = "message.received",
) -> dict:
    """Build a simulated Chat Mitra webhook payload (message.received shape)."""
    import time

    message: dict = {"type": message_type}
    if message_type == "text":
        message["text"] = message_text

    return {
        "event": event,
        "message_id": message_id or f"wamid_{int(time.time())}",
        "direction": "inbound",
        "from": sender,
        "to": "919888888888",
        "timestamp": int(time.time()),
        "message": message,
    }


class TestHealthCheck:
    """Test the health check endpoint."""

    def test_health_returns_200(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestWebhookHandler:
    """Test the webhook handler."""

    @patch("app.main.send_reply", new_callable=AsyncMock, return_value=True)
    def test_text_message_gets_reply(self, mock_send, client):
        """A text message should trigger a reply."""
        payload = _make_webhook_payload("sauvage price")
        response = client.post("/webhook", json=payload)
        assert response.status_code == 200
        mock_send.assert_called_once()
        # The reply text should contain price info or be a fallback
        reply_text = mock_send.call_args[0][1]
        assert isinstance(reply_text, str)
        assert len(reply_text) > 0

    @patch("app.main.send_reply", new_callable=AsyncMock, return_value=True)
    def test_known_perfume_gets_price_card(self, mock_send, client):
        """A known perfume should get a price card reply."""
        payload = _make_webhook_payload("sauvage")
        response = client.post("/webhook", json=payload)
        assert response.status_code == 200
        reply_text = mock_send.call_args[0][1]
        assert "₹" in reply_text  # Should contain price
        assert "Prepaid" in reply_text  # Should contain shipping

    @patch("app.main.send_reply", new_callable=AsyncMock, return_value=True)
    def test_unknown_message_gets_fallback(self, mock_send, client):
        """An unrecognizable message should get a fallback reply."""
        payload = _make_webhook_payload("asdfghjkl random nonsense xyz")
        response = client.post("/webhook", json=payload)
        assert response.status_code == 200
        reply_text = mock_send.call_args[0][1]
        assert "perfume" in reply_text.lower() or "🙂" in reply_text

    @patch("app.main.send_reply", new_callable=AsyncMock, return_value=True)
    def test_non_text_message_gets_prompt(self, mock_send, client):
        """A non-text message (image, voice, etc.) should get a prompt."""
        payload = _make_webhook_payload("", message_type="image")
        response = client.post("/webhook", json=payload)
        assert response.status_code == 200
        reply_text = mock_send.call_args[0][1]
        assert "type" in reply_text.lower()

    @patch("app.main.send_reply", new_callable=AsyncMock, return_value=True)
    def test_duplicate_message_not_replied_twice(self, mock_send, client):
        """Same message ID sent twice should only get one reply."""
        payload = _make_webhook_payload("sauvage", message_id="dup_test_123")

        # First call
        response1 = client.post("/webhook", json=payload)
        assert response1.status_code == 200

        # Second call with same message_id
        response2 = client.post("/webhook", json=payload)
        assert response2.status_code == 200

        # Should only have been called once
        assert mock_send.call_count == 1

    @patch("app.main.send_reply", new_callable=AsyncMock, return_value=True)
    def test_empty_message_no_reply(self, mock_send, client):
        """Empty message should not trigger a reply."""
        payload = _make_webhook_payload("")
        response = client.post("/webhook", json=payload)
        assert response.status_code == 200
        mock_send.assert_not_called()

    def test_malformed_payload_returns_200(self, client):
        """Malformed payload should return 200 (don't trigger retries)."""
        response = client.post("/webhook", json={"random": "garbage"})
        assert response.status_code == 200

    def test_invalid_json_returns_200(self, client):
        """Invalid JSON should return 200."""
        response = client.post(
            "/webhook",
            content=b"not json",
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 200


class TestOrderConfirmationWebhook:
    """The website's 'confirm my order' template should short-circuit the matcher."""

    ORDER_MESSAGE = (
        "Hi Sovereign Scents! 👑 I'd like to confirm my order:\n\n"
        "🧾 *Order #SS1784385515072433*\n\n"
        "1. Lancôme Idole Peach 'N Roses | 3ml × 1 = ₹290\n\n"
        "💰 *Total: ₹370*\n\n"
        "📋 Order link: https://www.sovereignscents.in/admin/order/"
        "030a84473e802c7c6cd8f9efea879d86b176390c36511a5ff4a4d9146cd42381"
    )

    @patch("app.main.send_reply", new_callable=AsyncMock, return_value=True)
    def test_order_confirmation_gets_fixed_reply_not_a_price_card(self, mock_send, client):
        """It must NOT get a price card for the perfume named in the order line item."""
        payload = _make_webhook_payload(self.ORDER_MESSAGE)
        response = client.post("/webhook", json=payload)
        assert response.status_code == 200
        mock_send.assert_called_once()
        reply_text = mock_send.call_args[0][1]
        assert reply_text == "Thank you for confirming your order! We will contact you shortly!!"
        assert "₹" not in reply_text  # not a price card

    @patch("app.main.send_reply", new_callable=AsyncMock, return_value=True)
    def test_order_confirmation_deduped_like_any_other_message(self, mock_send, client):
        payload = _make_webhook_payload(self.ORDER_MESSAGE, message_id="order_dup_1")
        client.post("/webhook", json=payload)
        client.post("/webhook", json=payload)
        assert mock_send.call_count == 1

    @patch("app.main.send_reply", new_callable=AsyncMock, return_value=True)
    def test_order_confirmation_bypasses_length_limit(self, mock_send, client):
        """A long order (many line items) must not get rejected by MAX_MESSAGE_LENGTH."""
        long_order = self.ORDER_MESSAGE + "\n".join(
            f"{i}. Some Perfume Name Padding | 3ml x 1 = 100" for i in range(2, 40)
        )
        assert len(long_order) > 500  # actually exceeds the default length cutoff
        payload = _make_webhook_payload(long_order)
        response = client.post("/webhook", json=payload)
        assert response.status_code == 200
        reply_text = mock_send.call_args[0][1]
        assert "Thank you for confirming your order" in reply_text


class TestWebhookEventFiltering:
    """Chat Mitra can deliver non-message.received events to the same URL
    (message.sent, message.failed, message.status.updated) if subscribed —
    only message.received is an inbound customer message needing a reply."""

    @pytest.mark.parametrize(
        "event",
        ["message.sent", "message.failed", "message.status.updated"],
    )
    @patch("app.main.send_reply", new_callable=AsyncMock, return_value=True)
    def test_non_received_events_are_ignored(self, mock_send, client, event):
        payload = _make_webhook_payload("sauvage", event=event)
        response = client.post("/webhook", json=payload)
        assert response.status_code == 200
        mock_send.assert_not_called()

    @patch("app.main.send_reply", new_callable=AsyncMock, return_value=True)
    def test_message_received_event_still_works(self, mock_send, client):
        """Sanity check the parametrized cases above aren't vacuously true."""
        payload = _make_webhook_payload("sauvage", event="message.received")
        response = client.post("/webhook", json=payload)
        assert response.status_code == 200
        mock_send.assert_called_once()
