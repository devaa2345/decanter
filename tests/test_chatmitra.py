"""
Unit tests for the Chat Mitra send-message client (app/chatmitra.py).

No real HTTP calls: httpx.AsyncClient is patched with a fake that records the
request and returns a canned httpx.Response. Following this codebase's
existing convention (no test file uses pytest.mark.asyncio), the async
send_reply() is exercised via asyncio.run() inside plain sync test functions.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app import chatmitra
from app.chatmitra import CHATMITRA_SEND_URL, send_reply


@pytest.fixture(autouse=True)
def fake_token():
    """A truthy CHATMITRA_API_TOKEN for every test except the one that
    specifically wants it unset (which re-patches it locally, overriding
    this)."""
    with patch.object(chatmitra.settings, "CHATMITRA_API_TOKEN", "test_token_123"):
        yield


def _mock_client(response=None, side_effect=None):
    """A fake httpx.AsyncClient supporting `async with ... as client: await client.post(...)`."""
    post = AsyncMock(return_value=response, side_effect=side_effect)
    fake = AsyncMock()
    fake.post = post
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=False)
    return fake, post


class TestSendReplyRequestShape:
    def test_correct_url_headers_and_payload(self):
        response = httpx.Response(202, json={"status": "success", "jobId": "job_abc"})
        fake, post = _mock_client(response=response)

        with patch("app.chatmitra.httpx.AsyncClient", return_value=fake):
            result = asyncio.run(send_reply("919876543210", "Hello there"))

        assert result is True
        post.assert_called_once()
        _, kwargs = post.call_args
        assert post.call_args.args[0] == CHATMITRA_SEND_URL
        assert kwargs["headers"]["Authorization"] == "Bearer test_token_123"
        assert kwargs["headers"]["Content-Type"] == "application/json"
        assert kwargs["json"] == {
            "recipient_mobile_number": "919876543210",
            "messages": [
                {
                    "kind": "raw",
                    "payload": {"type": "text", "text": {"body": "Hello there"}},
                }
            ],
        }


class TestSendReplyResponseHandling:
    def test_202_is_treated_as_success(self):
        response = httpx.Response(202, json={"status": "success", "jobId": "job_1"})
        fake, _ = _mock_client(response=response)
        with patch("app.chatmitra.httpx.AsyncClient", return_value=fake):
            assert asyncio.run(send_reply("919876543210", "hi")) is True

    def test_200_is_also_treated_as_success(self):
        """is_success covers the whole 2xx range, not a hardcoded == 202."""
        response = httpx.Response(200, json={"status": "success"})
        fake, _ = _mock_client(response=response)
        with patch("app.chatmitra.httpx.AsyncClient", return_value=fake):
            assert asyncio.run(send_reply("919876543210", "hi")) is True

    def test_4xx_is_failure(self):
        response = httpx.Response(401, text="invalid token")
        fake, _ = _mock_client(response=response)
        with patch("app.chatmitra.httpx.AsyncClient", return_value=fake):
            assert asyncio.run(send_reply("919876543210", "hi")) is False

    def test_5xx_is_failure(self):
        response = httpx.Response(500, text="server error")
        fake, _ = _mock_client(response=response)
        with patch("app.chatmitra.httpx.AsyncClient", return_value=fake):
            assert asyncio.run(send_reply("919876543210", "hi")) is False

    def test_timeout_returns_false_not_an_exception(self):
        fake, _ = _mock_client(side_effect=httpx.TimeoutException("timed out"))
        with patch("app.chatmitra.httpx.AsyncClient", return_value=fake):
            assert asyncio.run(send_reply("919876543210", "hi")) is False

    def test_unexpected_exception_returns_false_not_an_exception(self):
        fake, _ = _mock_client(side_effect=RuntimeError("boom"))
        with patch("app.chatmitra.httpx.AsyncClient", return_value=fake):
            assert asyncio.run(send_reply("919876543210", "hi")) is False


class TestSendReplyMissingToken:
    def test_missing_token_returns_false_without_a_network_call(self):
        fake, post = _mock_client(response=httpx.Response(202, json={}))
        with patch.object(chatmitra.settings, "CHATMITRA_API_TOKEN", ""):
            with patch("app.chatmitra.httpx.AsyncClient", return_value=fake):
                result = asyncio.run(send_reply("919876543210", "hi"))

        assert result is False
        post.assert_not_called()
