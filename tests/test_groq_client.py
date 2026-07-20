"""
Unit tests for the Groq LLM classification client (app/groq_client.py).

No real API calls: AsyncOpenAI is patched with a fake whose
chat.completions.create returns a canned response shape. Following this
codebase's convention (no test file uses pytest.mark.asyncio), the async
classify_perfume() is exercised via asyncio.run() inside plain sync tests.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import groq_client
from app.catalog import PERFUMES
from app.groq_client import classify_perfume

_REAL_PID = next(iter(PERFUMES))
_REAL_DISPLAY_NAME = PERFUMES[_REAL_PID]["display_name"]


def _mock_response(content):
    message = MagicMock()
    message.content = content
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]
    return response


def _mock_openai_client(response=None, exception=None):
    create = AsyncMock(return_value=response, side_effect=exception)
    fake = MagicMock()
    fake.chat.completions.create = create
    return fake, create


@pytest.fixture(autouse=True)
def fake_api_key():
    with patch.object(groq_client.settings, "GROQ_API_KEY", "test_key_123"):
        yield


class TestSystemPrompt:
    def test_built_from_live_catalog(self):
        assert _REAL_PID in groq_client._PERFUME_LIST
        assert _REAL_DISPLAY_NAME in groq_client._PERFUME_LIST
        assert _REAL_PID in groq_client._SYSTEM_PROMPT


class TestClassifyPerfume:
    def test_valid_known_id_is_returned(self):
        fake, _ = _mock_openai_client(response=_mock_response(_REAL_PID))
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            result = asyncio.run(classify_perfume("some message"))
        assert result == _REAL_PID

    def test_none_literal_returns_none(self):
        fake, _ = _mock_openai_client(response=_mock_response("none"))
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            result = asyncio.run(classify_perfume("hello there"))
        assert result is None

    def test_id_not_in_catalog_is_rejected(self):
        """Defensive validation: even if the LLM hallucinates a plausible-
        looking id, only real catalog ids are trusted."""
        fake, _ = _mock_openai_client(response=_mock_response("totally_made_up_id_xyz"))
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            result = asyncio.run(classify_perfume("some message"))
        assert result is None

    def test_quotes_whitespace_and_case_are_normalized(self):
        noisy = f'  "{_REAL_PID.upper()}"  \n'
        fake, _ = _mock_openai_client(response=_mock_response(noisy))
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            result = asyncio.run(classify_perfume("some message"))
        assert result == _REAL_PID

    def test_empty_content_returns_none(self):
        fake, _ = _mock_openai_client(response=_mock_response(None))
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            result = asyncio.run(classify_perfume("some message"))
        assert result is None

    def test_api_exception_returns_none_not_raised(self):
        fake, _ = _mock_openai_client(exception=RuntimeError("groq is down"))
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            result = asyncio.run(classify_perfume("some message"))
        assert result is None

    def test_missing_api_key_returns_none_without_a_call(self):
        fake, create = _mock_openai_client(response=_mock_response(_REAL_PID))
        with patch.object(groq_client.settings, "GROQ_API_KEY", ""):
            with patch("app.groq_client.AsyncOpenAI", return_value=fake):
                result = asyncio.run(classify_perfume("some message"))
        assert result is None
        create.assert_not_called()

    def test_sends_message_as_user_content(self):
        fake, create = _mock_openai_client(response=_mock_response("none"))
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            asyncio.run(classify_perfume("how much for sauvage"))

        _, kwargs = create.call_args
        messages = kwargs["messages"]
        assert messages[-1] == {"role": "user", "content": "how much for sauvage"}
        assert kwargs["temperature"] == 0
