"""
Unit tests for the Groq LLM client (app/groq_client.py) — primary
classification + reply phrasing, one combined call, JSON mode.

No real API calls: AsyncOpenAI is patched with a fake whose
chat.completions.create returns a canned response shape. Following this
codebase's convention (no test file uses pytest.mark.asyncio), the async
classify_and_phrase() is exercised via asyncio.run() inside plain sync tests.

candidates is a required parameter (no "full catalog" default) — this is
deliberate: production hit a real bug where the old module-level prompt
listed all 1200+ perfumes and blew through Groq's 6000 TPM rate limit on
every single call (~23.5K tokens requested). Tests here use a small fixed
shortlist throughout, mirroring how app.matcher._top_candidates_for_llm
actually calls this in production.

JSON mode, not a free-text label format: confirmed directly against
llama-3.1-8b-instant that a "PERFUME_ID: ..." text convention isn't
followed reliably (a real response came back as "dior_sauvage_edt: Dior
Sauvage EDT" instead of the requested label), silently discarded every
time and always falling through to the free matcher. JSON mode
(response_format={"type": "json_object"}) is an API-level guarantee of
well-formed output, not a hope that a small/fast model follows a text
convention.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import groq_client
from app.catalog import PERFUMES
from app.groq_client import GroqClassification, classify_and_phrase

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


def _json_response(
    pid: str, opening: str = "Hey there!", closing: str = "Want me to set that aside?"
) -> str:
    return json.dumps({"perfume_id": pid, "opening": opening, "closing": closing})


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

    def test_forbids_prices_and_asterisks_in_instructions(self):
        """The prompt itself must tell the model never to write numbers or
        asterisks — the two hard-learned production lessons this session."""
        prompt = groq_client._build_system_prompt(CANDIDATES)
        assert "price" in prompt.lower()
        assert "asterisk" in prompt.lower()

    def test_mentions_json(self):
        """response_format=json_object requires the prompt to actually
        mention JSON, or the API call can error."""
        prompt = groq_client._build_system_prompt(CANDIDATES)
        assert "json" in prompt.lower()


class TestParseResponse:
    """_parse_response is the defensive-parsing boundary — malformed model
    output must degrade gracefully, never raise."""

    def test_well_formed_json(self):
        pid, opening, closing = groq_client._parse_response(_json_response(_REAL_PID))
        assert pid == _REAL_PID
        assert opening == "Hey there!"
        assert closing == "Want me to set that aside?"

    def test_case_normalized(self):
        pid, _, _ = groq_client._parse_response(_json_response(_REAL_PID.upper()))
        assert pid == _REAL_PID

    def test_missing_keys_return_none_for_those_fields(self):
        pid, opening, closing = groq_client._parse_response(json.dumps({"perfume_id": _REAL_PID}))
        assert pid == _REAL_PID
        assert opening is None
        assert closing is None

    def test_empty_string_fields_treated_as_none(self):
        """The prompt asks for closing="" when unmatched — must not surface
        as a literal empty-string closing line in a reply."""
        pid, opening, closing = groq_client._parse_response(
            json.dumps({"perfume_id": "none", "opening": "Not sure!", "closing": ""})
        )
        assert closing is None

    def test_invalid_json_returns_all_none(self):
        pid, opening, closing = groq_client._parse_response("uh, what?")
        assert pid is None
        assert opening is None
        assert closing is None

    def test_json_array_instead_of_object_returns_all_none(self):
        pid, opening, closing = groq_client._parse_response(json.dumps(["not", "an", "object"]))
        assert pid is None
        assert opening is None
        assert closing is None

    def test_non_string_field_values_ignored_gracefully(self):
        pid, opening, closing = groq_client._parse_response(
            json.dumps({"perfume_id": 12345, "opening": None, "closing": ["a", "list"]})
        )
        assert pid is None
        assert opening is None
        assert closing is None

    def test_the_real_observed_production_failure_mode_no_longer_applies(self):
        """The exact failure this fix addresses: the old free-text format
        let the model emit 'id: name' instead of a proper label — JSON mode
        structurally can't produce that shape at all, so there's nothing
        equivalent to test here except confirming plain non-JSON text is
        rejected cleanly (covered by test_invalid_json_returns_all_none)."""
        pid, _, _ = groq_client._parse_response("dior_sauvage_edt: Dior Sauvage EDT")
        assert pid is None


class TestClassifyAndPhrase:
    def test_valid_response_returns_full_classification(self):
        fake, _ = _mock_openai_client(response=_mock_response(_json_response(_REAL_PID)))
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            result = asyncio.run(classify_and_phrase("some message", candidates=CANDIDATES))
        assert result == GroqClassification(
            perfume_id=_REAL_PID, opening="Hey there!", closing="Want me to set that aside?"
        )

    def test_none_literal_returns_empty_classification(self):
        fake, _ = _mock_openai_client(
            response=_mock_response(_json_response("none", opening="Not sure which one!", closing=""))
        )
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            result = asyncio.run(classify_and_phrase("hello there", candidates=CANDIDATES))
        assert result == GroqClassification()

    def test_ambiguous_literal_returns_empty_classification(self):
        """"ambiguous" falls through to the free matcher (which has its own
        real candidate-listing logic) rather than Groq guessing between
        genuinely different products."""
        fake, _ = _mock_openai_client(
            response=_mock_response(
                _json_response("ambiguous", opening="A couple could match!", closing="")
            )
        )
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            result = asyncio.run(classify_and_phrase("9pm", candidates=CANDIDATES))
        assert result.perfume_id is None

    def test_id_not_in_candidates_is_rejected(self):
        """Defensive validation: even if the LLM hallucinates a plausible-
        looking id, only ids we actually offered it are trusted — including
        real catalog ids that just weren't in THIS shortlist."""
        excluded_pid = next(p for p in PERFUMES if p not in CANDIDATES)
        fake, _ = _mock_openai_client(response=_mock_response(_json_response(excluded_pid)))
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            result = asyncio.run(classify_and_phrase("some message", candidates=CANDIDATES))
        assert result.perfume_id is None

    def test_hedging_opening_on_a_confident_match_is_discarded(self):
        """Confirmed directly against the real model: it sometimes returns
        a specific perfume_id but still writes a hedging opening ("We have
        several options, can you give me a hint?") despite the prompt rule
        against it. That reads as contradictory next to a confident price
        card, so it must be discarded — perfume_id/closing stay intact,
        only the bad opening is dropped."""
        fake, _ = _mock_openai_client(
            response=_mock_response(
                _json_response(
                    _REAL_PID,
                    opening="We have several options, can you give me a hint?",
                    closing="Happy to help!",
                )
            )
        )
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            result = asyncio.run(classify_and_phrase("some message", candidates=CANDIDATES))
        assert result.perfume_id == _REAL_PID
        assert result.opening is None
        assert result.closing == "Happy to help!"

    def test_confident_opening_without_a_question_mark_is_kept(self):
        fake, _ = _mock_openai_client(
            response=_mock_response(_json_response(_REAL_PID, opening="Great pick, we have that!"))
        )
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            result = asyncio.run(classify_and_phrase("some message", candidates=CANDIDATES))
        assert result.opening == "Great pick, we have that!"

    def test_opening_closing_not_populated_when_no_valid_id(self):
        """Opening/closing are useless without a matched perfume — the
        caller falls through to the free matcher entirely in that case, so
        there's no mismatched 'friendly text + wrong/no card' combination."""
        fake, _ = _mock_openai_client(
            response=_mock_response(_json_response("none", opening="Hmm not sure!", closing=""))
        )
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            result = asyncio.run(classify_and_phrase("some message", candidates=CANDIDATES))
        assert result.opening is None
        assert result.closing is None

    def test_malformed_response_returns_empty_classification_not_raised(self):
        fake, _ = _mock_openai_client(response=_mock_response("uh, what?"))
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            result = asyncio.run(classify_and_phrase("some message", candidates=CANDIDATES))
        assert result == GroqClassification()

    def test_empty_content_returns_empty_classification(self):
        fake, _ = _mock_openai_client(response=_mock_response(None))
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            result = asyncio.run(classify_and_phrase("some message", candidates=CANDIDATES))
        assert result == GroqClassification()

    def test_api_exception_returns_empty_classification_not_raised(self):
        fake, _ = _mock_openai_client(exception=RuntimeError("groq is down"))
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            result = asyncio.run(classify_and_phrase("some message", candidates=CANDIDATES))
        assert result == GroqClassification()

    def test_missing_api_key_returns_empty_without_a_call(self):
        fake, create = _mock_openai_client(response=_mock_response(_json_response(_REAL_PID)))
        with patch.object(groq_client.settings, "GROQ_API_KEY", ""):
            with patch("app.groq_client.AsyncOpenAI", return_value=fake):
                result = asyncio.run(classify_and_phrase("some message", candidates=CANDIDATES))
        assert result == GroqClassification()
        create.assert_not_called()

    def test_empty_candidates_returns_empty_without_a_call(self):
        """No point calling Groq if there's nothing to choose from."""
        fake, create = _mock_openai_client(response=_mock_response(_json_response(_REAL_PID)))
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            result = asyncio.run(classify_and_phrase("some message", candidates={}))
        assert result == GroqClassification()
        create.assert_not_called()

    def test_sends_message_as_user_content(self):
        fake, create = _mock_openai_client(response=_mock_response(_json_response("none")))
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            asyncio.run(classify_and_phrase("how much for sauvage", candidates=CANDIDATES))

        _, kwargs = create.call_args
        messages = kwargs["messages"]
        assert messages[-1] == {"role": "user", "content": "how much for sauvage"}

    def test_uses_the_specified_fast_model(self):
        fake, create = _mock_openai_client(response=_mock_response(_json_response("none")))
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            asyncio.run(classify_and_phrase("some message", candidates=CANDIDATES))

        _, kwargs = create.call_args
        assert kwargs["model"] == "llama-3.1-8b-instant"

    def test_requests_json_mode(self):
        """The actual fix: the API call must request json_object mode, not
        just hope the model follows a text convention."""
        fake, create = _mock_openai_client(response=_mock_response(_json_response("none")))
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            asyncio.run(classify_and_phrase("some message", candidates=CANDIDATES))

        _, kwargs = create.call_args
        assert kwargs["response_format"] == {"type": "json_object"}

    def test_prompt_sent_is_scoped_to_candidates(self):
        """End-to-end check that the actual API call carries the scoped
        prompt, not the module reaching back into the full catalog."""
        fake, create = _mock_openai_client(response=_mock_response(_json_response("none")))
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            asyncio.run(classify_and_phrase("some message", candidates=CANDIDATES))

        _, kwargs = create.call_args
        system_content = kwargs["messages"][0]["content"]
        excluded_pid = next(p for p in PERFUMES if p not in CANDIDATES)
        assert _REAL_PID in system_content
        assert excluded_pid not in system_content
