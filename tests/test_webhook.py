"""
Integration tests for the webhook endpoint.

Uses FastAPI TestClient to simulate Chat Mitra webhook payloads.
Mocks the Chat Mitra send-message call to avoid real API calls.
"""

import hashlib
import hmac
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app import main
from app.dedup import dedup_cache
from app.formatter import FALLBACK_MESSAGE
from app.main import app


@pytest.fixture(autouse=True)
def clear_dedup():
    """Clear dedup cache before each test."""
    dedup_cache.clear()
    yield
    dedup_cache.clear()


@pytest.fixture(autouse=True)
def no_webhook_secret():
    """
    Blank CHATMITRA_WEBHOOK_SECRET for every test in this file except the
    ones in TestWebhookSignatureVerification (which explicitly re-patch it).

    Without this, these tests silently depend on whatever's in the local
    .env — they were written to exercise payload parsing/matching/dedup,
    not signature verification, but once a real webhook secret got
    configured for actual Chat Mitra use, every one of them started
    failing with 403 instead of what they were actually testing.
    """
    with patch.object(main.settings, "CHATMITRA_WEBHOOK_SECRET", ""):
        yield


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


class TestWebhookSignatureVerification:
    """
    Chat Mitra signs the raw body with HMAC-SHA256 and sends the hex digest
    in X-Webhook-Signature (see app.main._verify_webhook_signature). No
    coverage existed for this before — every other test in this file runs
    with the secret blanked (see the no_webhook_secret fixture above), which
    is correct for what THEY'RE testing, but left the actual verification
    logic itself unexercised.
    """

    SECRET = "test_webhook_secret_abc123"

    def _signed_headers(self, body: bytes) -> dict:
        signature = hmac.new(self.SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
        return {"x-webhook-signature": signature, "content-type": "application/json"}

    @patch("app.main.send_reply", new_callable=AsyncMock, return_value=True)
    def test_valid_signature_is_accepted(self, mock_send, client):
        payload = _make_webhook_payload("sauvage")
        import json

        body = json.dumps(payload).encode("utf-8")
        with patch.object(main.settings, "CHATMITRA_WEBHOOK_SECRET", self.SECRET):
            response = client.post("/webhook", content=body, headers=self._signed_headers(body))
        assert response.status_code == 200
        mock_send.assert_called_once()

    def test_missing_signature_header_is_rejected(self, client):
        payload = _make_webhook_payload("sauvage")
        with patch.object(main.settings, "CHATMITRA_WEBHOOK_SECRET", self.SECRET):
            response = client.post("/webhook", json=payload)
        assert response.status_code == 403

    def test_wrong_signature_is_rejected(self, client):
        payload = _make_webhook_payload("sauvage")
        with patch.object(main.settings, "CHATMITRA_WEBHOOK_SECRET", self.SECRET):
            response = client.post(
                "/webhook", json=payload, headers={"x-webhook-signature": "0" * 64}
            )
        assert response.status_code == 403

    def test_signature_computed_for_a_different_secret_is_rejected(self, client):
        payload = _make_webhook_payload("sauvage")
        import json

        body = json.dumps(payload).encode("utf-8")
        wrong_secret_sig = hmac.new(b"not-the-real-secret", body, hashlib.sha256).hexdigest()
        with patch.object(main.settings, "CHATMITRA_WEBHOOK_SECRET", self.SECRET):
            response = client.post(
                "/webhook",
                content=body,
                headers={"x-webhook-signature": wrong_secret_sig, "content-type": "application/json"},
            )
        assert response.status_code == 403

    @patch("app.main.send_reply", new_callable=AsyncMock, return_value=True)
    def test_no_secret_configured_skips_verification(self, mock_send, client):
        """Already covered implicitly by every other test via the
        no_webhook_secret fixture, but spelled out explicitly here."""
        payload = _make_webhook_payload("sauvage")
        with patch.object(main.settings, "CHATMITRA_WEBHOOK_SECRET", ""):
            response = client.post("/webhook", json=payload)
        assert response.status_code == 200


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
    def test_unrecognizable_message_stays_silent(self, mock_send, client):
        """Gibberish that isn't a greeting/catalog request and doesn't match
        a perfume should NOT get a reply — silence by default, so the bot
        doesn't spend a Chat Mitra credit on messages worth nothing."""
        payload = _make_webhook_payload("asdfghjkl random nonsense xyz")
        response = client.post("/webhook", json=payload)
        assert response.status_code == 200
        mock_send.assert_not_called()

    @patch("app.main.send_reply", new_callable=AsyncMock, return_value=True)
    def test_greeting_gets_fallback_reply(self, mock_send, client):
        """A plain greeting IS one of the two cases worth replying to."""
        payload = _make_webhook_payload("hi")
        response = client.post("/webhook", json=payload)
        assert response.status_code == 200
        mock_send.assert_called_once()
        reply_text = mock_send.call_args[0][1]
        assert "perfume" in reply_text.lower() or "🙂" in reply_text

    @patch("app.main.match_perfume", new_callable=AsyncMock)
    @patch("app.main.send_reply", new_callable=AsyncMock, return_value=True)
    def test_catalog_phrase_never_reaches_the_matcher(self, mock_send, mock_match, client):
        """Regression guard for the real production bug: 'catalogue' (and
        other catalog phrases) were getting a wrong perfume's price card
        instead of the catalog link, because Groq/fuzzy only got checked
        AFTER the matcher already guessed something. The veto in app.main
        must short-circuit to the catalog reply WITHOUT ever calling
        match_perfume — mocking it here proves that directly rather than
        just checking the reply text."""
        for i, msg in enumerate(("catalogue", "catalog", "send me the catalogue please", "Catalogue")):
            mock_send.reset_mock()
            mock_match.reset_mock()
            # Distinct message_id per iteration — the default auto-generated
            # id is second-precision and these all run within one second,
            # which would otherwise trip the dedup cache and make later
            # iterations look like retried duplicates.
            payload = _make_webhook_payload(msg, message_id=f"catalog_veto_test_{i}")
            response = client.post("/webhook", json=payload)
            assert response.status_code == 200
            mock_match.assert_not_called()
            mock_send.assert_called_once()
            reply_text = mock_send.call_args[0][1]
            assert reply_text == FALLBACK_MESSAGE

    @patch("app.main.send_reply", new_callable=AsyncMock, return_value=True)
    def test_catalog_phrase_with_a_real_perfume_still_gets_the_price_card(self, mock_send, client):
        """The narrow escape hatch: a message that's BOTH a catalog phrase
        AND names a specific product precisely (exact keyword match) must
        still return that product's price card, not the catalog link."""
        payload = _make_webhook_payload("show me sauvage price")
        response = client.post("/webhook", json=payload)
        assert response.status_code == 200
        reply_text = mock_send.call_args[0][1]
        assert "₹" in reply_text
        assert "Prepaid" in reply_text

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

    @patch("app.main.send_reply", new_callable=AsyncMock, return_value=True)
    @patch("app.main.match_perfume", new_callable=AsyncMock)
    def test_multiple_distinct_perfumes_named_get_a_card_each(self, mock_match, mock_send, client):
        """The reported production bug: a customer naming 2+ distinct
        perfumes in one message (e.g. "sauvage and eros price") must get a
        full price card for EACH one, not just one random perfume's detail.
        match_perfume is patched directly here so this is a deterministic
        check of the webhook's reply-building branch, independent of which
        underlying layer (Groq or the free fallback matchers) actually
        resolved the multiple ids."""
        from app.catalog import PERFUMES
        from app.matcher import MatchResult

        pid_a, pid_b = list(PERFUMES)[:2]
        mock_match.return_value = MatchResult(ambiguous=True, matched_perfume_ids=[pid_a, pid_b])

        payload = _make_webhook_payload("sauvage and eros price please")
        response = client.post("/webhook", json=payload)
        assert response.status_code == 200
        mock_send.assert_called_once()
        reply_text = mock_send.call_args[0][1]
        assert PERFUMES[pid_a]["display_name"].upper() in reply_text
        assert PERFUMES[pid_b]["display_name"].upper() in reply_text
        assert reply_text.count("Prepaid only") == 1

    @patch("app.main.send_reply", new_callable=AsyncMock, return_value=True)
    def test_ambiguous_9pm_lists_all_real_candidates(self, mock_send, client):
        """A bare '9pm' must get a full price card for every real candidate
        (name + prices), not the old content-free 'which one?' message.
        Card headers are uppercased (see app.formatter._build_card_block)."""
        payload = _make_webhook_payload("9pm")
        response = client.post("/webhook", json=payload)
        assert response.status_code == 200
        reply_text = mock_send.call_args[0][1]
        assert "AFNAN 9PM REBEL" in reply_text
        assert "AFNAN 9PM NIGHT OUT" in reply_text
        assert "AFNAN 9PM ELIXIR PARFUM" in reply_text
        assert "₹" in reply_text


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
