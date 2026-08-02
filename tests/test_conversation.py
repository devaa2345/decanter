"""
Tests for per-sender conversation memory (app/conversation.py).

The behaviour under test is what makes "and 5ml?" answerable: the bot
remembers what it just showed this customer, in the order it showed it.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from app import conversation
from app.catalog import PERFUMES
from app.config import settings
from app.conversation import (
    clear,
    last_discussed_perfumes,
    recent_turns,
    record_bot_reply,
    record_customer_message,
)

SENDER = "919876543210"


def run(coro):
    return asyncio.run(coro)


def some_pids(n: int = 2) -> list[str]:
    return list(PERFUMES)[:n]


@pytest.fixture(autouse=True)
def clean_state():
    clear()
    yield
    clear()


@pytest.fixture(autouse=True)
def no_supabase():
    """
    Pin the Supabase boundary shut for every test that does not explicitly
    open it.

    recent_turns() warms itself from message_events when a client exists, so
    with real credentials in the local .env these tests were reading a live
    production conversation and asserting against it — "cleared, so this is
    empty" failed because the database still had the customer's history.
    Same reasoning as tests/test_auth.py: a test must not depend on, or
    leak into, whatever happens to be configured on this machine.

    The four tests that DO exercise the warm-start patch get_client
    themselves, and their patch wins over this one.
    """
    with patch("app.conversation.get_client", return_value=None):
        yield


class TestRecording:
    def test_customer_message_is_remembered(self):
        record_customer_message(SENDER, "9pm rebel price")
        turns = run(recent_turns(SENDER))
        assert turns == [{"role": "customer", "text": "9pm rebel price"}]

    def test_bot_reply_records_which_perfumes_it_showed(self):
        pid = some_pids(1)[0]
        record_bot_reply(SENDER, "(price card)", [pid])
        turn = run(recent_turns(SENDER))[0]
        assert turn["role"] == "bot"
        assert turn["perfume_ids"] == [pid]
        assert turn["perfume_names"] == [PERFUMES[pid]["display_name"]]

    def test_ordering_is_preserved(self):
        """'the second one' only means something if the order the cards were
        shown in survives."""
        pids = some_pids(3)
        record_bot_reply(SENDER, "(cards)", pids)
        assert run(recent_turns(SENDER))[0]["perfume_ids"] == pids

    def test_turns_are_oldest_first(self):
        record_customer_message(SENDER, "first")
        record_bot_reply(SENDER, "second", [])
        record_customer_message(SENDER, "third")
        assert [t["text"] for t in run(recent_turns(SENDER))] == ["first", "second", "third"]

    def test_unknown_perfume_ids_are_dropped_from_names(self):
        record_bot_reply(SENDER, "(card)", ["not_a_real_pid"])
        turn = run(recent_turns(SENDER))[0]
        assert turn["perfume_ids"] == ["not_a_real_pid"]
        assert turn["perfume_names"] == []

    def test_empty_sender_or_text_is_ignored(self):
        record_customer_message("", "hello")
        record_customer_message(SENDER, "")
        assert run(recent_turns(SENDER)) == []


class TestBounds:
    def test_history_is_capped_at_the_configured_length(self):
        for i in range(settings.CONVERSATION_TURNS * 3):
            record_customer_message(SENDER, f"msg {i}")
        turns = run(recent_turns(SENDER))
        assert len(turns) == settings.CONVERSATION_TURNS
        # The cap must drop the OLDEST turns, not the newest.
        assert turns[-1]["text"] == f"msg {settings.CONVERSATION_TURNS * 3 - 1}"

    def test_expired_history_is_forgotten(self, monkeypatch):
        record_customer_message(SENDER, "9pm rebel")
        monkeypatch.setattr(
            conversation, "_now", lambda: __import__("time").time() + settings.CONVERSATION_TTL_SECONDS + 1
        )
        assert run(recent_turns(SENDER)) == []

    def test_senders_do_not_leak_into_each_other(self):
        record_customer_message(SENDER, "sauvage")
        record_customer_message("919999999999", "eros")
        assert [t["text"] for t in run(recent_turns(SENDER))] == ["sauvage"]

    def test_sender_count_is_bounded(self):
        for i in range(conversation._MAX_SENDERS + 50):
            record_customer_message(f"sender{i}", "hi")
        assert len(conversation._turns) <= conversation._MAX_SENDERS


class TestLastDiscussedPerfumes:
    def test_returns_the_most_recent_card(self):
        """Only the LATEST card counts — 'and 5ml?' means the thing just
        discussed, and reaching further back answers a question the customer
        did not ask."""
        first, second = some_pids(2)
        history = [
            {"role": "bot", "text": "", "perfume_ids": [first]},
            {"role": "customer", "text": "what about the other one"},
            {"role": "bot", "text": "", "perfume_ids": [second]},
            {"role": "customer", "text": "and 5ml?"},
        ]
        assert last_discussed_perfumes(history) == [second]

    def test_returns_every_candidate_from_an_ambiguous_card(self):
        pids = some_pids(3)
        assert last_discussed_perfumes([{"role": "bot", "perfume_ids": pids}]) == pids

    def test_bot_turns_without_a_card_are_skipped(self):
        pid = some_pids(1)[0]
        history = [
            {"role": "bot", "text": "welcome", "perfume_ids": [pid]},
            {"role": "bot", "text": "catalog link", "perfume_ids": []},
        ]
        assert last_discussed_perfumes(history) == [pid]

    def test_no_history_is_empty(self):
        assert last_discussed_perfumes([]) == []
        assert last_discussed_perfumes(None) == []

    def test_customer_only_history_is_empty(self):
        assert last_discussed_perfumes([{"role": "customer", "text": "hi"}]) == []


class TestSupabaseWarmup:
    """
    Cold-start backstop: after a restart (a redeploy, or Render's free tier
    spinning down) the in-memory buffer is empty mid-conversation. Nothing
    extra is persisted for this — app.analytics already logs every inbound
    message with its sender and resolved perfume_id.
    """

    def test_history_is_rebuilt_from_the_analytics_log(self):
        pid = some_pids(1)[0]
        client = MagicMock()
        client.table.return_value.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value.data = [
            {"message_text": "9pm rebel price", "perfume_id": pid, "created_at": "2026-01-01T00:00:00Z"},
        ]

        with patch("app.conversation.get_client", return_value=client):
            turns = run(recent_turns(SENDER))

        assert [t["role"] for t in turns] == ["customer", "bot"]
        assert turns[0]["text"] == "9pm rebel price"
        assert turns[1]["perfume_ids"] == [pid]

    def test_supabase_is_only_consulted_once_per_sender(self):
        client = MagicMock()
        client.table.return_value.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value.data = []

        with patch("app.conversation.get_client", return_value=client) as mock_get:
            run(recent_turns(SENDER))
            run(recent_turns(SENDER))
            run(recent_turns(SENDER))

        assert mock_get.call_count == 1

    def test_no_supabase_configured_is_not_an_error(self):
        with patch("app.conversation.get_client", return_value=None):
            assert run(recent_turns(SENDER)) == []

    def test_a_failing_query_is_not_an_error(self):
        """Best-effort in every direction — a Supabase hiccup must never
        break a customer-facing reply."""
        client = MagicMock()
        client.table.side_effect = RuntimeError("supabase down")
        with patch("app.conversation.get_client", return_value=client):
            assert run(recent_turns(SENDER)) == []

    def test_in_memory_history_skips_the_lookup_entirely(self):
        record_customer_message(SENDER, "sauvage")
        with patch("app.conversation.get_client") as mock_get:
            run(recent_turns(SENDER))
        mock_get.assert_not_called()


class TestClear:
    def test_clears_one_sender(self):
        record_customer_message(SENDER, "a")
        record_customer_message("other", "b")
        clear(SENDER)
        assert run(recent_turns(SENDER)) == []
        assert run(recent_turns("other")) != []

    def test_clears_everything(self):
        record_customer_message(SENDER, "a")
        record_customer_message("other", "b")
        clear()
        assert run(recent_turns(SENDER)) == []
        assert run(recent_turns("other")) == []

    def test_empty_sender_returns_nothing(self):
        assert run(recent_turns("")) == []
