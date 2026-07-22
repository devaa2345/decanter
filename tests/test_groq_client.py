"""
Unit tests for the Groq LLM classification client (app/groq_client.py).

No real API calls: AsyncOpenAI is patched with a fake whose
chat.completions.create returns a canned response shape. Following this
codebase's convention (no test file uses pytest.mark.asyncio), the async
classify_perfume() is exercised via asyncio.run() inside plain sync tests.

candidates is a required parameter (no "full catalog" default) — this is
deliberate: production hit a real bug where the old module-level prompt
listed all 1200+ perfumes and blew through Groq's 6000 TPM rate limit on
every single call (~23.5K tokens requested). Tests here use a small fixed
shortlist throughout, mirroring how app.matcher._top_candidates_for_llm
actually calls this in production.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import groq_client
from app.catalog import PERFUMES
from app.groq_client import classify_perfume

_REAL_PID = next(iter(PERFUMES))
_REAL_DISPLAY_NAME = PERFUMES[_REAL_PID]["display_name"]
_OTHER_PID = list(PERFUMES)[1]

CANDIDATES = {
    _REAL_PID: PERFUMES[_REAL_PID],
    _OTHER_PID: PERFUMES[_OTHER_PID],
}


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


class TestBuildSystemPrompt:
    def test_scoped_to_given_candidates_only(self):
        prompt = groq_client._build_system_prompt(CANDIDATES)
        assert _REAL_PID in prompt
        assert _REAL_DISPLAY_NAME in prompt
        assert _OTHER_PID in prompt

    def test_does_not_leak_the_rest_of_the_catalog(self):
        """The whole point of the fix: a small shortlist must not balloon
        back into a full-catalog-sized prompt."""
        prompt = groq_client._build_system_prompt(CANDIDATES)
        excluded_pid = next(p for p in PERFUMES if p not in CANDIDATES)
        assert excluded_pid not in prompt
        # Sanity bound: two entries should produce a tiny prompt, nowhere
        # near the ~23.5K-token full-catalog prompt that broke production.
        assert len(prompt) < 2000


class TestClassifyPerfume:
    def test_valid_known_id_is_returned(self):
        fake, _ = _mock_openai_client(response=_mock_response(_REAL_PID))
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            result = asyncio.run(classify_perfume("some message", candidates=CANDIDATES))
        assert result == _REAL_PID

    def test_none_literal_returns_none(self):
        fake, _ = _mock_openai_client(response=_mock_response("none"))
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            result = asyncio.run(classify_perfume("hello there", candidates=CANDIDATES))
        assert result is None

    def test_id_not_in_candidates_is_rejected(self):
        """Defensive validation: even if the LLM hallucinates a plausible-
        looking id, only ids we actually offered it are trusted — including
        real catalog ids that just weren't in THIS shortlist."""
        excluded_pid = next(p for p in PERFUMES if p not in CANDIDATES)
        fake, _ = _mock_openai_client(response=_mock_response(excluded_pid))
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            result = asyncio.run(classify_perfume("some message", candidates=CANDIDATES))
        assert result is None

    def test_quotes_whitespace_and_case_are_normalized(self):
        noisy = f'  "{_REAL_PID.upper()}"  \n'
        fake, _ = _mock_openai_client(response=_mock_response(noisy))
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            result = asyncio.run(classify_perfume("some message", candidates=CANDIDATES))
        assert result == _REAL_PID

    def test_empty_content_returns_none(self):
        fake, _ = _mock_openai_client(response=_mock_response(None))
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            result = asyncio.run(classify_perfume("some message", candidates=CANDIDATES))
        assert result is None

    def test_api_exception_returns_none_not_raised(self):
        fake, _ = _mock_openai_client(exception=RuntimeError("groq is down"))
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            result = asyncio.run(classify_perfume("some message", candidates=CANDIDATES))
        assert result is None

    def test_missing_api_key_returns_none_without_a_call(self):
        fake, create = _mock_openai_client(response=_mock_response(_REAL_PID))
        with patch.object(groq_client.settings, "GROQ_API_KEY", ""):
            with patch("app.groq_client.AsyncOpenAI", return_value=fake):
                result = asyncio.run(classify_perfume("some message", candidates=CANDIDATES))
        assert result is None
        create.assert_not_called()

    def test_empty_candidates_returns_none_without_a_call(self):
        """No point calling Groq if there's nothing to choose from."""
        fake, create = _mock_openai_client(response=_mock_response(_REAL_PID))
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            result = asyncio.run(classify_perfume("some message", candidates={}))
        assert result is None
        create.assert_not_called()

    def test_sends_message_as_user_content(self):
        fake, create = _mock_openai_client(response=_mock_response("none"))
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            asyncio.run(classify_perfume("how much for sauvage", candidates=CANDIDATES))

        _, kwargs = create.call_args
        messages = kwargs["messages"]
        assert messages[-1] == {"role": "user", "content": "how much for sauvage"}
        assert kwargs["temperature"] == 0

    def test_prompt_sent_is_scoped_to_candidates(self):
        """End-to-end check that the actual API call carries the scoped
        prompt, not the module reaching back into the full catalog."""
        fake, create = _mock_openai_client(response=_mock_response("none"))
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            asyncio.run(classify_perfume("some message", candidates=CANDIDATES))

        _, kwargs = create.call_args
        system_content = kwargs["messages"][0]["content"]
        excluded_pid = next(p for p in PERFUMES if p not in CANDIDATES)
        assert _REAL_PID in system_content
        assert excluded_pid not in system_content
