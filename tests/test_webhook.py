"""
Integration tests for the webhook endpoint.

Uses FastAPI TestClient to simulate AiSensy webhook payloads.
Mocks the AiSensy send-message call to avoid real API calls.
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
) -> dict:
    """Build a simulated AiSensy webhook payload."""
    import time

    return {
        "id": message_id or f"wamid_{int(time.time())}",
        "from": sender,
        "message": {
            "type": message_type,
            "text": {
                "body": message_text,
            } if message_type == "text" else {},
        },
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


class TestWebhookCloudAPIPayload:
    """Test with WhatsApp Cloud API nested payload format."""

    @patch("app.main.send_reply", new_callable=AsyncMock, return_value=True)
    def test_cloud_api_format(self, mock_send, client):
        """Should handle WhatsApp Cloud API nested structure."""
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "id": "wamid_cloud_123",
                                        "from": "919876543210",
                                        "type": "text",
                                        "text": {"body": "sauvage 5ml price"},
                                    }
                                ],
                                "contacts": [
                                    {
                                        "profile": {"name": "Test User"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
        response = client.post("/webhook", json=payload)
        assert response.status_code == 200
        mock_send.assert_called_once()
