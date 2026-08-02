"""
Unit tests for the human-handoff feature (app/handoff.py): once the owner
personally messages a customer directly through Chat Mitra (not through this
bot), the bot should back off from that conversation for a configurable
window — see app.main's message.sent webhook handling and pause-check
branch.

The Supabase boundary is mocked throughout (get_client/require_client and
the row-level helper functions) so these are pure unit tests of the
aggregation/decision logic, same convention as tests/test_analytics.py.
Following this codebase's convention (no test file uses pytest.mark.asyncio),
async functions are exercised via asyncio.run() inside plain sync tests.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app import handoff
from app.db import SupabaseUnavailable


@pytest.fixture(autouse=True)
def clear_own_sends():
    """Isolate the module-level echo cache between tests."""
    handoff.clear_own_sends()
    yield
    handoff.clear_own_sends()


class TestOwnSendEchoDetection:
    def test_recorded_send_is_recognized_as_own(self):
        handoff.record_own_send("919876543210", "Hello there!")
        assert handoff.was_sent_by_bot("919876543210", "Hello there!") is True

    def test_unrecorded_send_is_not_recognized(self):
        assert handoff.was_sent_by_bot("919876543210", "Never sent this") is False

    def test_different_recipient_is_not_matched(self):
        handoff.record_own_send("919876543210", "Hello there!")
        assert handoff.was_sent_by_bot("911111111111", "Hello there!") is False

    def test_different_text_is_not_matched(self):
        handoff.record_own_send("919876543210", "Hello there!")
        assert handoff.was_sent_by_bot("919876543210", "Something else entirely") is False

    def test_expired_entry_is_no_longer_recognized(self):
        fake_now = [1_000_000.0]
        with patch("app.handoff.time.time", side_effect=lambda: fake_now[0]):
            handoff.record_own_send("919876543210", "Hello there!")
            fake_now[0] += handoff._OWN_SEND_TTL_SECONDS + 1
            assert handoff.was_sent_by_bot("919876543210", "Hello there!") is False

    def test_clear_own_sends_resets_state(self):
        handoff.record_own_send("919876543210", "Hello there!")
        handoff.clear_own_sends()
        assert handoff.was_sent_by_bot("919876543210", "Hello there!") is False


class TestIsPaused:
    def test_false_when_supabase_unconfigured(self):
        with patch("app.handoff.get_client", return_value=None):
            result = asyncio.run(handoff.is_paused("919876543210"))
        assert result is False

    def test_false_when_no_pause_row(self):
        with patch("app.handoff.get_client", return_value=MagicMock()):
            with patch("app.handoff._fetch_pause_row", return_value=None):
                result = asyncio.run(handoff.is_paused("919876543210"))
        assert result is False

    def test_true_when_paused_until_is_in_the_future(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
        with patch("app.handoff.get_client", return_value=MagicMock()):
            with patch("app.handoff._fetch_pause_row", return_value={"paused_until": future}):
                result = asyncio.run(handoff.is_paused("919876543210"))
        assert result is True

    def test_false_when_paused_until_is_in_the_past(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with patch("app.handoff.get_client", return_value=MagicMock()):
            with patch("app.handoff._fetch_pause_row", return_value={"paused_until": past}):
                result = asyncio.run(handoff.is_paused("919876543210"))
        assert result is False

    def test_false_on_query_exception(self):
        with patch("app.handoff.get_client", return_value=MagicMock()):
            with patch("app.handoff._fetch_pause_row", side_effect=RuntimeError("db down")):
                result = asyncio.run(handoff.is_paused("919876543210"))  # must not raise
        assert result is False

    def test_false_on_malformed_paused_until(self):
        with patch("app.handoff.get_client", return_value=MagicMock()):
            with patch("app.handoff._fetch_pause_row", return_value={"paused_until": "not-a-date"}):
                result = asyncio.run(handoff.is_paused("919876543210"))
        assert result is False


class TestStartPause:
    def test_noop_when_supabase_unconfigured(self):
        with patch("app.handoff.get_client", return_value=None):
            with patch("app.handoff._upsert_pause_row") as upsert_mock:
                asyncio.run(handoff.start_pause("919876543210"))
        upsert_mock.assert_not_called()

    def test_upserts_with_configured_duration(self):
        async def _fake_get_hours():
            return 6.0

        with patch("app.handoff.get_client", return_value=MagicMock()):
            with patch("app.handoff.get_pause_duration_hours", side_effect=_fake_get_hours):
                with patch("app.handoff._upsert_pause_row") as upsert_mock:
                    before = datetime.now(timezone.utc)
                    asyncio.run(handoff.start_pause("919876543210"))
                    after = datetime.now(timezone.utc)

        upsert_mock.assert_called_once()
        _, sender, paused_until_iso = upsert_mock.call_args[0]
        assert sender == "919876543210"
        paused_until = datetime.fromisoformat(paused_until_iso)
        assert before + timedelta(hours=6) <= paused_until <= after + timedelta(hours=6)

    def test_swallows_exceptions(self):
        async def _fake_get_hours():
            return 24.0

        with patch("app.handoff.get_client", return_value=MagicMock()):
            with patch("app.handoff.get_pause_duration_hours", side_effect=_fake_get_hours):
                with patch("app.handoff._upsert_pause_row", side_effect=RuntimeError("db down")):
                    asyncio.run(handoff.start_pause("919876543210"))  # must not raise


class TestGetPauseDurationHours:
    def test_default_when_supabase_unconfigured(self):
        with patch("app.handoff.get_client", return_value=None):
            result = asyncio.run(handoff.get_pause_duration_hours())
        assert result == handoff.DEFAULT_PAUSE_HOURS

    def test_default_when_no_setting_row(self):
        with patch("app.handoff.get_client", return_value=MagicMock()):
            with patch("app.handoff._fetch_setting_row", return_value=None):
                result = asyncio.run(handoff.get_pause_duration_hours())
        assert result == handoff.DEFAULT_PAUSE_HOURS

    def test_reads_stored_value(self):
        with patch("app.handoff.get_client", return_value=MagicMock()):
            with patch("app.handoff._fetch_setting_row", return_value={"value": 48}):
                result = asyncio.run(handoff.get_pause_duration_hours())
        assert result == 48.0

    def test_default_on_exception(self):
        with patch("app.handoff.get_client", return_value=MagicMock()):
            with patch("app.handoff._fetch_setting_row", side_effect=RuntimeError("db down")):
                result = asyncio.run(handoff.get_pause_duration_hours())  # must not raise
        assert result == handoff.DEFAULT_PAUSE_HOURS

    def test_default_on_malformed_value(self):
        with patch("app.handoff.get_client", return_value=MagicMock()):
            with patch("app.handoff._fetch_setting_row", return_value={"value": "not-a-number"}):
                result = asyncio.run(handoff.get_pause_duration_hours())
        assert result == handoff.DEFAULT_PAUSE_HOURS


class TestAdminPauseDurationHours:
    """The dashboard Settings-page-facing get/set — unlike the best-effort
    pair above, these raise SupabaseUnavailable so a failed save is never
    silently swallowed on the owner's dashboard."""

    def test_get_raises_when_supabase_unconfigured(self):
        with patch("app.handoff.require_client", side_effect=SupabaseUnavailable("nope")):
            with pytest.raises(SupabaseUnavailable):
                asyncio.run(handoff.admin_get_pause_duration_hours())

    def test_get_default_when_no_setting_row(self):
        with patch("app.handoff.require_client", return_value=MagicMock()):
            with patch("app.handoff._fetch_setting_row", return_value=None):
                result = asyncio.run(handoff.admin_get_pause_duration_hours())
        assert result == handoff.DEFAULT_PAUSE_HOURS

    def test_get_reads_stored_value(self):
        with patch("app.handoff.require_client", return_value=MagicMock()):
            with patch("app.handoff._fetch_setting_row", return_value={"value": 12}):
                result = asyncio.run(handoff.admin_get_pause_duration_hours())
        assert result == 12.0

    def test_set_raises_when_supabase_unconfigured(self):
        with patch("app.handoff.require_client", side_effect=SupabaseUnavailable("nope")):
            with pytest.raises(SupabaseUnavailable):
                asyncio.run(handoff.admin_set_pause_duration_hours(48))

    def test_set_upserts_and_returns_value(self):
        with patch("app.handoff.require_client", return_value=MagicMock()):
            with patch("app.handoff._upsert_setting_row") as upsert_mock:
                result = asyncio.run(handoff.admin_set_pause_duration_hours(48))
        assert result == 48
        upsert_mock.assert_called_once()
        _, key, value = upsert_mock.call_args[0]
        assert key == "human_handoff_pause_hours"
        assert value == 48


class TestThePauseHoldsWithoutSupabase:
    """
    Reported from production: the owner took over a conversation and the bot
    carried on answering as soon as the customer named a perfume.

    The cause was that the pause lived only in Supabase and every write path
    swallowed its exception, so an unconfigured URL, a missing migration or
    an RLS rule blocking the key all produced a pause that was never stored
    — and nothing in the logs said so. The pause now takes effect in memory
    first, which is what makes it hold regardless.
    """

    def test_pause_holds_when_supabase_is_unconfigured(self):
        async def _fake_get_hours():
            return 24.0

        with patch("app.handoff.get_client", return_value=None):
            with patch("app.handoff.get_pause_duration_hours", side_effect=_fake_get_hours):
                persisted = asyncio.run(handoff.start_pause("919876543210"))
                assert asyncio.run(handoff.is_paused("919876543210")) is True
        assert persisted is False

    def test_pause_holds_when_the_write_fails(self):
        async def _fake_get_hours():
            return 24.0

        with patch("app.handoff.get_client", return_value=MagicMock()):
            with patch("app.handoff.get_pause_duration_hours", side_effect=_fake_get_hours):
                with patch("app.handoff._upsert_pause_row", side_effect=RuntimeError("db down")):
                    persisted = asyncio.run(handoff.start_pause("919876543210"))
                assert asyncio.run(handoff.is_paused("919876543210")) is True
        assert persisted is False

    def test_a_successful_write_says_so(self):
        async def _fake_get_hours():
            return 24.0

        with patch("app.handoff.get_client", return_value=MagicMock()):
            with patch("app.handoff.get_pause_duration_hours", side_effect=_fake_get_hours):
                with patch("app.handoff._upsert_pause_row"):
                    assert asyncio.run(handoff.start_pause("919876543210")) is True

    def test_an_expired_memory_pause_stops_holding(self):
        async def _fake_get_hours():
            return -1.0  # already elapsed

        with patch("app.handoff.get_client", return_value=None):
            with patch("app.handoff.get_pause_duration_hours", side_effect=_fake_get_hours):
                asyncio.run(handoff.start_pause("919876543210"))
                assert asyncio.run(handoff.is_paused("919876543210")) is False

    def test_a_pause_for_one_customer_does_not_silence_another(self):
        async def _fake_get_hours():
            return 24.0

        with patch("app.handoff.get_client", return_value=None):
            with patch("app.handoff.get_pause_duration_hours", side_effect=_fake_get_hours):
                asyncio.run(handoff.start_pause("919876543210"))
                assert asyncio.run(handoff.is_paused("919000000001")) is False


class TestPhoneNumberFormatting:
    """
    The second half of the same production failure. An inbound
    message.received carries the customer in `from` and an outbound
    message.sent carries them in `to`, and nothing guarantees the two are
    punctuated the same way. Keyed on the raw strings, a pause written for
    "+919876543210" was invisible to a lookup for "919876543210" — and the
    bot's own reply echoing back under the other spelling would have been
    read as the owner taking over.
    """

    @pytest.mark.parametrize(
        "written,read",
        [
            ("+919876543210", "919876543210"),
            ("919876543210", "+91 98765 43210"),
            ("919876543210@c.us", "919876543210"),
            ("91-98765-43210", "919876543210"),
        ],
    )
    def test_a_pause_is_found_however_the_number_is_written(self, written, read):
        async def _fake_get_hours():
            return 24.0

        with patch("app.handoff.get_client", return_value=None):
            with patch("app.handoff.get_pause_duration_hours", side_effect=_fake_get_hours):
                asyncio.run(handoff.start_pause(written))
                assert asyncio.run(handoff.is_paused(read)) is True

    def test_the_bots_own_echo_is_recognised_however_the_number_is_written(self):
        handoff.record_own_send("919876543210", "Here are the prices")
        assert handoff.was_sent_by_bot("+919876543210", "Here are the prices") is True

    def test_trailing_whitespace_does_not_break_echo_detection(self):
        """An echo that failed to match would be read as the owner taking
        over — pausing the bot on its own reply."""
        handoff.record_own_send("919876543210", "Here are the prices")
        assert handoff.was_sent_by_bot("919876543210", "Here are the prices\n") is True

    def test_a_genuinely_different_message_is_still_not_an_echo(self):
        handoff.record_own_send("919876543210", "Here are the prices")
        assert handoff.was_sent_by_bot("919876543210", "sending it today bhai") is False

    def test_the_supabase_row_is_keyed_on_the_normalised_number(self):
        """Otherwise a restart would look the pause up under a key the write
        never used."""
        async def _fake_get_hours():
            return 24.0

        with patch("app.handoff.get_client", return_value=MagicMock()):
            with patch("app.handoff.get_pause_duration_hours", side_effect=_fake_get_hours):
                with patch("app.handoff._upsert_pause_row") as upsert_mock:
                    asyncio.run(handoff.start_pause("+91 98765 43210"))
        _client, sender, _until = upsert_mock.call_args[0]
        assert sender == "919876543210"
