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

perfume_ids is a LIST, not a single id: confirmed in production that a
customer message can clearly name multiple distinct perfumes at once (e.g.
"sauvage and eros price"), and the old single-id shape silently dropped
every match but the first. The old "none"/"ambiguous" literal markers are
gone too — an empty list now means no match, and the existing
not-in-candidates filtering handles any leftover legacy literal the model
might still emit exactly like any other hallucinated id (see
test_legacy_none_or_ambiguous_literal_treated_as_no_match).
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
_THIRD_PID = list(PERFUMES)[2]

CANDIDATES = {
    _REAL_PID: PERFUMES[_REAL_PID],
    _OTHER_PID: PERFUMES[_OTHER_PID],
    _THIRD_PID: PERFUMES[_THIRD_PID],
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
    pids,
    opening: str = "Hey there!",
    closing: str = "Want me to set that aside?",
    explicit_ask: bool = True,
) -> str:
    """pids: a single id string (wrapped into a 1-item list) or a list of
    ids — matches the two shapes tests actually need to construct.
    explicit_ask defaults to True since most tests here are exercising a
    genuine "customer is asking" scenario — tests specifically about the
    explicit_ask gate itself pass it explicitly."""
    if isinstance(pids, str):
        pids = [pids]
    return json.dumps(
        {"perfume_ids": pids, "explicit_ask": explicit_ask, "opening": opening, "closing": closing}
    )


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
        # Sanity bound: three entries should produce a tiny prompt, nowhere
        # near the ~23.5K-token full-catalog prompt that broke production.
        assert len(prompt) < 3800

    def test_forbids_prices_and_asterisks_in_instructions(self):
        """The prompt itself must tell the model never to write numbers or
        asterisks — two hard-learned production lessons this session."""
        prompt = groq_client._build_system_prompt(CANDIDATES)
        assert "price" in prompt.lower()
        assert "asterisk" in prompt.lower()

    def test_mentions_json(self):
        """response_format=json_object requires the prompt to actually
        mention JSON, or the API call can error."""
        prompt = groq_client._build_system_prompt(CANDIDATES)
        assert "json" in prompt.lower()

    def test_instructs_listing_every_clearly_named_perfume(self):
        """The core new behavior: the prompt must tell the model to return
        every distinct perfume it clearly identifies, not just one — this
        is the actual fix for the "shows one random perfume's detail"
        production bug."""
        prompt = groq_client._build_system_prompt(CANDIDATES)
        assert "perfume_ids" in prompt
        assert "2+" in prompt or "multiple" in prompt.lower()

    def test_instructs_the_explicit_ask_vs_passing_mention_distinction(self):
        """The core new behavior: the prompt must tell the model to tell
        apart an actual request from a perfume name merely mentioned in
        passing — this is the fix for the "auto-fires a price card mid
        human conversation" production bug."""
        prompt = groq_client._build_system_prompt(CANDIDATES)
        assert "explicit_ask" in prompt
        assert "passing" in prompt.lower() or "mention" in prompt.lower()


class TestParseResponse:
    """_parse_response is the defensive-parsing boundary — malformed model
    output must degrade gracefully, never raise."""

    def test_well_formed_json_single_id(self):
        pids, explicit_ask, opening, closing = groq_client._parse_response(
            _json_response(_REAL_PID)
        )
        assert pids == [_REAL_PID]
        assert explicit_ask is True
        assert opening == "Hey there!"
        assert closing == "Want me to set that aside?"

    def test_well_formed_json_multiple_ids(self):
        pids, _, _, _ = groq_client._parse_response(_json_response([_REAL_PID, _OTHER_PID]))
        assert pids == [_REAL_PID, _OTHER_PID]

    def test_case_normalized(self):
        pids, _, _, _ = groq_client._parse_response(_json_response(_REAL_PID.upper()))
        assert pids == [_REAL_PID]

    def test_duplicate_ids_deduplicated_preserving_order(self):
        pids, _, _, _ = groq_client._parse_response(
            _json_response([_REAL_PID, _OTHER_PID, _REAL_PID])
        )
        assert pids == [_REAL_PID, _OTHER_PID]

    def test_missing_perfume_ids_key_returns_empty_list(self):
        pids, _, opening, _ = groq_client._parse_response(json.dumps({"opening": "Hi"}))
        assert pids == []
        assert opening == "Hi"

    def test_perfume_ids_not_a_list_returns_empty_list(self):
        pids, _, _, _ = groq_client._parse_response(json.dumps({"perfume_ids": _REAL_PID}))
        assert pids == []

    def test_non_string_entries_in_list_are_skipped(self):
        pids, _, _, _ = groq_client._parse_response(
            json.dumps({"perfume_ids": [_REAL_PID, 123, None, _OTHER_PID]})
        )
        assert pids == [_REAL_PID, _OTHER_PID]

    def test_empty_list_returns_empty_list(self):
        pids, _, _, closing = groq_client._parse_response(
            json.dumps({"perfume_ids": [], "opening": "Not sure!", "closing": ""})
        )
        assert pids == []
        # The prompt asks for closing="" when unmatched — must not surface
        # as a literal empty-string closing line in a reply.
        assert closing is None

    def test_invalid_json_returns_all_empty(self):
        pids, explicit_ask, opening, closing = groq_client._parse_response("uh, what?")
        assert pids == []
        assert explicit_ask is False
        assert opening is None
        assert closing is None

    def test_json_array_instead_of_object_returns_all_empty(self):
        pids, explicit_ask, opening, closing = groq_client._parse_response(
            json.dumps(["not", "an", "object"])
        )
        assert pids == []
        assert explicit_ask is False
        assert opening is None
        assert closing is None

    def test_non_string_opening_closing_ignored_gracefully(self):
        pids, _, opening, closing = groq_client._parse_response(
            json.dumps({"perfume_ids": [_REAL_PID], "opening": None, "closing": ["a", "list"]})
        )
        assert pids == [_REAL_PID]
        assert opening is None
        assert closing is None

    def test_the_real_observed_production_failure_mode_no_longer_applies(self):
        """The exact failure this fix addresses: the old free-text format
        let the model emit 'id: name' instead of a proper label — JSON mode
        structurally can't produce that shape at all, so there's nothing
        equivalent to test here except confirming plain non-JSON text is
        rejected cleanly (covered by test_invalid_json_returns_all_empty)."""
        pids, _, _, _ = groq_client._parse_response("dior_sauvage_edt: Dior Sauvage EDT")
        assert pids == []

    def test_explicit_ask_true_is_parsed(self):
        _, explicit_ask, _, _ = groq_client._parse_response(
            _json_response(_REAL_PID, explicit_ask=True)
        )
        assert explicit_ask is True

    def test_explicit_ask_false_is_parsed(self):
        _, explicit_ask, _, _ = groq_client._parse_response(
            _json_response(_REAL_PID, explicit_ask=False)
        )
        assert explicit_ask is False

    def test_explicit_ask_missing_defaults_to_false(self):
        """Fail closed: an older/malformed response without the field must
        never be silently treated as a confident ask."""
        _, explicit_ask, _, _ = groq_client._parse_response(
            json.dumps({"perfume_ids": [_REAL_PID], "opening": "Hi", "closing": "Ok"})
        )
        assert explicit_ask is False

    def test_explicit_ask_non_boolean_defaults_to_false(self):
        _, explicit_ask, _, _ = groq_client._parse_response(
            json.dumps({"perfume_ids": [_REAL_PID], "explicit_ask": "yes"})
        )
        assert explicit_ask is False


class TestClassifyAndPhrase:
    def test_valid_response_returns_full_classification(self):
        fake, _ = _mock_openai_client(response=_mock_response(_json_response(_REAL_PID)))
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            result = asyncio.run(classify_and_phrase("some message", candidates=CANDIDATES))
        assert result == GroqClassification(
            perfume_ids=[_REAL_PID],
            explicit_ask=True,
            opening="Hey there!",
            closing="Want me to set that aside?",
        )

    def test_multiple_distinct_perfumes_are_all_returned(self):
        """The core new behavior this file exists to cover: a message that
        clearly names 2+ different perfumes must surface ALL of them, not
        just the first — the old single-id shape had no way to do this and
        silently dropped every match but one."""
        fake, _ = _mock_openai_client(
            response=_mock_response(_json_response([_REAL_PID, _OTHER_PID]))
        )
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            result = asyncio.run(
                classify_and_phrase("sauvage and eros price", candidates=CANDIDATES)
            )
        assert result.perfume_ids == [_REAL_PID, _OTHER_PID]

    def test_empty_list_returns_empty_classification(self):
        fake, _ = _mock_openai_client(
            response=_mock_response(_json_response([], opening="Not sure which one!", closing=""))
        )
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            result = asyncio.run(classify_and_phrase("hello there", candidates=CANDIDATES))
        assert result == GroqClassification()

    def test_legacy_none_or_ambiguous_literal_treated_as_no_match(self):
        """The old prompt asked for a literal "none"/"ambiguous" string in
        place of a real id; even if the model still emits one of those
        (imperfect prompt compliance during the migration), it isn't a
        real candidate id, so the normal not-in-candidates filtering
        discards it exactly like any other hallucinated id — no
        special-casing needed."""
        for literal in ("none", "ambiguous"):
            fake, _ = _mock_openai_client(response=_mock_response(_json_response(literal)))
            with patch("app.groq_client.AsyncOpenAI", return_value=fake):
                result = asyncio.run(classify_and_phrase("hello there", candidates=CANDIDATES))
            assert result == GroqClassification()

    def test_explicit_ask_false_collapses_to_empty_classification(self):
        """The core new behavior this file exists to cover: a perfume
        mentioned only in passing (explicit_ask=false) must be treated
        exactly like no match at all, even though a real candidate id was
        returned — confirmed in production that a bare name mention mid-
        conversation was still auto-firing a price card."""
        fake, _ = _mock_openai_client(
            response=_mock_response(_json_response(_REAL_PID, explicit_ask=False))
        )
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            result = asyncio.run(
                classify_and_phrase(
                    "the owner told me sauvage is really nice apparently",
                    candidates=CANDIDATES,
                )
            )
        assert result == GroqClassification()

    def test_explicit_ask_missing_from_response_collapses_to_empty_classification(self):
        """Same as above, but for a response that omits the field entirely
        (older/imperfect model compliance) rather than setting it false."""
        fake, _ = _mock_openai_client(
            response=_mock_response(
                json.dumps({"perfume_ids": [_REAL_PID], "opening": "Hi", "closing": "Ok"})
            )
        )
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            result = asyncio.run(classify_and_phrase("some message", candidates=CANDIDATES))
        assert result == GroqClassification()

    def test_ids_not_in_candidates_are_filtered_out(self):
        """Defensive validation: even if the LLM hallucinates a plausible-
        looking id, only ids we actually offered it are trusted — including
        real catalog ids that just weren't in THIS shortlist. A response
        with one valid and one invalid id keeps only the valid one."""
        excluded_pid = next(p for p in PERFUMES if p not in CANDIDATES)
        fake, _ = _mock_openai_client(
            response=_mock_response(_json_response([_REAL_PID, excluded_pid]))
        )
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            result = asyncio.run(classify_and_phrase("some message", candidates=CANDIDATES))
        assert result.perfume_ids == [_REAL_PID]

    def test_all_ids_invalid_returns_empty_classification(self):
        excluded_pid = next(p for p in PERFUMES if p not in CANDIDATES)
        fake, _ = _mock_openai_client(response=_mock_response(_json_response(excluded_pid)))
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            result = asyncio.run(classify_and_phrase("some message", candidates=CANDIDATES))
        assert result == GroqClassification()

    def test_hedging_opening_on_a_confident_match_is_discarded(self):
        """Confirmed directly against the real model: it sometimes returns
        a specific perfume_id but still writes a hedging opening ("We have
        several options, can you give me a hint?") despite the prompt rule
        against it. That reads as contradictory next to a confident price
        card, so it must be discarded — perfume_ids/closing stay intact,
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
        assert result.perfume_ids == [_REAL_PID]
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
            response=_mock_response(_json_response([], opening="Hmm not sure!", closing=""))
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

    def test_api_exception_returns_none_not_raised(self):
        """None, not an empty GroqClassification — a real API failure means
        Groq was never actually asked, so app.matcher must fall through to
        the deterministic fallback rather than trusting a "confident no" it
        never actually gave (see the None-vs-empty split in the module
        docstring)."""
        fake, _ = _mock_openai_client(exception=RuntimeError("groq is down"))
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            result = asyncio.run(classify_and_phrase("some message", candidates=CANDIDATES))
        assert result is None

    def test_missing_api_key_returns_none_without_a_call(self):
        fake, create = _mock_openai_client(response=_mock_response(_json_response(_REAL_PID)))
        with patch.object(groq_client.settings, "GROQ_API_KEY", ""):
            with patch("app.groq_client.AsyncOpenAI", return_value=fake):
                result = asyncio.run(classify_and_phrase("some message", candidates=CANDIDATES))
        assert result is None
        create.assert_not_called()

    def test_empty_candidates_returns_none_without_a_call(self):
        """No point calling Groq if there's nothing to choose from."""
        fake, create = _mock_openai_client(response=_mock_response(_json_response(_REAL_PID)))
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            result = asyncio.run(classify_and_phrase("some message", candidates={}))
        assert result is None
        create.assert_not_called()

    def test_sends_message_as_user_content(self):
        fake, create = _mock_openai_client(response=_mock_response(_json_response([])))
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            asyncio.run(classify_and_phrase("how much for sauvage", candidates=CANDIDATES))

        _, kwargs = create.call_args
        messages = kwargs["messages"]
        assert messages[-1] == {"role": "user", "content": "how much for sauvage"}

    def test_uses_the_specified_fast_model(self):
        fake, create = _mock_openai_client(response=_mock_response(_json_response([])))
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            asyncio.run(classify_and_phrase("some message", candidates=CANDIDATES))

        _, kwargs = create.call_args
        assert kwargs["model"] == "llama-3.1-8b-instant"

    def test_requests_json_mode(self):
        """The actual fix: the API call must request json_object mode, not
        just hope the model follows a text convention."""
        fake, create = _mock_openai_client(response=_mock_response(_json_response([])))
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            asyncio.run(classify_and_phrase("some message", candidates=CANDIDATES))

        _, kwargs = create.call_args
        assert kwargs["response_format"] == {"type": "json_object"}

    def test_prompt_sent_is_scoped_to_candidates(self):
        """End-to-end check that the actual API call carries the scoped
        prompt, not the module reaching back into the full catalog."""
        fake, create = _mock_openai_client(response=_mock_response(_json_response([])))
        with patch("app.groq_client.AsyncOpenAI", return_value=fake):
            asyncio.run(classify_and_phrase("some message", candidates=CANDIDATES))

        _, kwargs = create.call_args
        system_content = kwargs["messages"][0]["content"]
        excluded_pid = next(p for p in PERFUMES if p not in CANDIDATES)
        assert _REAL_PID in system_content
        assert excluded_pid not in system_content
