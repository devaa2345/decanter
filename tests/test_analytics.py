"""
Unit tests for the bot analytics aggregation logic (app/analytics.py).

The Supabase query chain itself is not under test here — _require_client and
_fetch_events_since are patched so each test supplies a canned list of event
dicts (the shape _fetch_events_since actually returns) and verifies the pure
aggregation logic on top of it: date bucketing, counting, grouping, and
critically the order_confirmation categorization fix (previously verified
only once, by hand, via a single live webhook call).

Following this codebase's convention (no test file uses pytest.mark.asyncio),
async functions are exercised via asyncio.run() inside plain sync tests.
"""

import asyncio
from unittest.mock import MagicMock, patch

from app import analytics
from app.catalog import PERFUMES

_REAL_PID = next(iter(PERFUMES))
_REAL_DISPLAY_NAME = PERFUMES[_REAL_PID]["display_name"]
_FAKE_PID = "totally_fake_pid_not_in_catalog"


def _run_with_events(coro_factory, events):
    """Patch the DB boundary so aggregation functions operate on a canned
    event list instead of hitting Supabase."""
    with patch("app.analytics._require_client", return_value=MagicMock()):
        with patch("app.analytics._fetch_events_since", return_value=events):
            return asyncio.run(coro_factory())


# Six events spanning two days, one of each outcome category.
MIXED_EVENTS = [
    {
        "message_text": "sauvage",
        "perfume_id": _REAL_PID,
        "layer": "exact",
        "ambiguous": False,
        "reply_sent": True,
        "created_at": "2026-07-18T10:00:00+00:00",
    },
    {
        "message_text": "savage",
        "perfume_id": _REAL_PID,
        "layer": "fuzzy",
        "ambiguous": False,
        "reply_sent": True,
        "created_at": "2026-07-18T11:00:00+00:00",
    },
    {
        "message_text": "9 pm rebel",
        "perfume_id": _FAKE_PID,
        "layer": "llm",
        "ambiguous": False,
        "reply_sent": True,
        "created_at": "2026-07-18T12:00:00+00:00",
    },
    {
        "message_text": "sauvage vs bleu de chanel",
        "perfume_id": None,
        "layer": None,
        "ambiguous": True,
        "reply_sent": True,
        "created_at": "2026-07-19T09:00:00+00:00",
    },
    {
        "message_text": "Hi Sovereign Scents! confirm my order #SS123",
        "perfume_id": None,
        "layer": "order_confirmation",
        "ambiguous": False,
        "reply_sent": True,
        "created_at": "2026-07-19T10:00:00+00:00",
    },
    {
        "message_text": "hello there",
        "perfume_id": None,
        "layer": None,
        "ambiguous": False,
        "reply_sent": True,
        "created_at": "2026-07-19T11:00:00+00:00",
    },
]


class TestGetOverview:
    def test_counts_by_category(self):
        result = _run_with_events(lambda: analytics.get_overview(days=30), MIXED_EVENTS)
        assert result["total_queries"] == 6
        assert result["matched"] == 3
        assert result["ambiguous"] == 1
        assert result["order_confirmations"] == 1
        assert result["unmatched"] == 1

    def test_order_confirmations_excluded_from_unmatched(self):
        """The categorization fix: an order-confirmation event must not
        inflate 'unmatched' (that bucket implies a catalog gap, which this
        is not)."""
        only_order_confirmations = [MIXED_EVENTS[4]] * 3
        result = _run_with_events(lambda: analytics.get_overview(days=30), only_order_confirmations)
        assert result["total_queries"] == 3
        assert result["order_confirmations"] == 3
        assert result["unmatched"] == 0

    def test_match_and_unmatched_rates(self):
        result = _run_with_events(lambda: analytics.get_overview(days=30), MIXED_EVENTS)
        assert result["match_rate"] == 50.0  # 3/6
        assert result["unmatched_rate"] == round(1 / 6 * 100, 1)

    def test_zero_events_gives_none_rates_not_a_crash(self):
        result = _run_with_events(lambda: analytics.get_overview(days=30), [])
        assert result["total_queries"] == 0
        assert result["match_rate"] is None
        assert result["unmatched_rate"] is None

    def test_catalog_size_reflects_live_catalog(self):
        result = _run_with_events(lambda: analytics.get_overview(days=30), [])
        assert result["catalog_size"] == len(PERFUMES)


class TestGetTimeseries:
    def test_buckets_by_day_oldest_first(self):
        result = _run_with_events(lambda: analytics.get_timeseries(days=30), MIXED_EVENTS)
        assert [row["date"] for row in result] == ["2026-07-18", "2026-07-19"]

    def test_day_one_has_the_three_match_layers(self):
        result = _run_with_events(lambda: analytics.get_timeseries(days=30), MIXED_EVENTS)
        day1 = result[0]
        assert day1 == {
            "date": "2026-07-18",
            "exact": 1,
            "fuzzy": 1,
            "llm": 1,
            "ambiguous": 0,
            "unmatched": 0,
            "order_confirmation": 0,
            "total": 3,
        }

    def test_order_confirmation_gets_its_own_bucket_not_unmatched(self):
        """The other half of the categorization fix — verified at the
        per-day bucketing level, not just the overview totals."""
        result = _run_with_events(lambda: analytics.get_timeseries(days=30), MIXED_EVENTS)
        day2 = result[1]
        assert day2["date"] == "2026-07-19"
        assert day2["order_confirmation"] == 1
        assert day2["unmatched"] == 1  # only the genuine "hello there" miss
        assert day2["ambiguous"] == 1
        assert day2["total"] == 3

    def test_events_without_created_at_are_skipped(self):
        events = [{**MIXED_EVENTS[0], "created_at": ""}]
        result = _run_with_events(lambda: analytics.get_timeseries(days=30), events)
        assert result == []


class TestGetTopPerfumes:
    def test_counts_and_sorts_descending(self):
        result = _run_with_events(lambda: analytics.get_top_perfumes(days=30), MIXED_EVENTS)
        assert result[0]["perfume_id"] == _REAL_PID
        assert result[0]["count"] == 2
        assert result[1]["perfume_id"] == _FAKE_PID
        assert result[1]["count"] == 1

    def test_known_id_uses_live_display_name(self):
        result = _run_with_events(lambda: analytics.get_top_perfumes(days=30), MIXED_EVENTS)
        known = next(r for r in result if r["perfume_id"] == _REAL_PID)
        assert known["display_name"] == _REAL_DISPLAY_NAME

    def test_unknown_id_falls_back_to_raw_id_as_display_name(self):
        result = _run_with_events(lambda: analytics.get_top_perfumes(days=30), MIXED_EVENTS)
        unknown = next(r for r in result if r["perfume_id"] == _FAKE_PID)
        assert unknown["display_name"] == _FAKE_PID

    def test_ambiguous_events_excluded_even_if_they_somehow_have_a_pid(self):
        events = [{**MIXED_EVENTS[0], "ambiguous": True}]
        result = _run_with_events(lambda: analytics.get_top_perfumes(days=30), events)
        assert result == []

    def test_limit_is_respected(self):
        events = [{**MIXED_EVENTS[0], "perfume_id": f"pid_{i}"} for i in range(5)]
        result = _run_with_events(lambda: analytics.get_top_perfumes(days=30, limit=2), events)
        assert len(result) == 2


class TestGetUnmatchedQueries:
    UNMATCHED_EVENTS = [
        {
            "message_text": "Hello There",
            "perfume_id": None,
            "ambiguous": False,
            "layer": None,
            "created_at": "2026-07-19T09:00:00+00:00",
        },
        {
            "message_text": "hello there",  # same normalized text, later
            "perfume_id": None,
            "ambiguous": False,
            "layer": None,
            "created_at": "2026-07-19T10:00:00+00:00",
        },
        {
            "message_text": "what is shipping cost",
            "perfume_id": None,
            "ambiguous": False,
            "layer": None,
            "created_at": "2026-07-19T08:00:00+00:00",
        },
        {
            "message_text": "confirm my order text",
            "perfume_id": None,
            "ambiguous": False,
            "layer": "order_confirmation",
            "created_at": "2026-07-19T11:00:00+00:00",
        },
        {
            "message_text": "sauvage vs bleu",
            "perfume_id": None,
            "ambiguous": True,
            "layer": None,
            "created_at": "2026-07-19T12:00:00+00:00",
        },
        {
            "message_text": "   ",
            "perfume_id": None,
            "ambiguous": False,
            "layer": None,
            "created_at": "2026-07-19T13:00:00+00:00",
        },
    ]

    def test_order_confirmations_excluded(self):
        """Critical regression guard: this list feeds the dashboard's
        'catalog gaps' screen directly — an order confirmation showing up
        here would look like a missing perfume the owner needs to add."""
        result = _run_with_events(
            lambda: analytics.get_unmatched_queries(days=30), self.UNMATCHED_EVENTS
        )
        texts = [g["message_text"] for g in result]
        assert not any("confirm my order" in t.lower() for t in texts)

    def test_ambiguous_excluded(self):
        result = _run_with_events(
            lambda: analytics.get_unmatched_queries(days=30), self.UNMATCHED_EVENTS
        )
        texts = [g["message_text"] for g in result]
        assert not any("vs" in t for t in texts)

    def test_blank_message_text_skipped(self):
        result = _run_with_events(
            lambda: analytics.get_unmatched_queries(days=30), self.UNMATCHED_EVENTS
        )
        assert all(g["message_text"].strip() for g in result)

    def test_case_insensitive_grouping_and_count(self):
        result = _run_with_events(
            lambda: analytics.get_unmatched_queries(days=30), self.UNMATCHED_EVENTS
        )
        hello_group = next(g for g in result if g["message_text"].lower() == "hello there")
        assert hello_group["count"] == 2

    def test_last_seen_is_the_max_created_at_in_the_group(self):
        result = _run_with_events(
            lambda: analytics.get_unmatched_queries(days=30), self.UNMATCHED_EVENTS
        )
        hello_group = next(g for g in result if g["message_text"].lower() == "hello there")
        assert hello_group["last_seen"] == "2026-07-19T10:00:00+00:00"

    def test_sorted_by_count_then_recency_descending(self):
        result = _run_with_events(
            lambda: analytics.get_unmatched_queries(days=30), self.UNMATCHED_EVENTS
        )
        # "hello there" (count=2) must outrank "what is shipping cost" (count=1)
        assert result[0]["message_text"].lower() == "hello there"

    def test_limit_is_respected(self):
        events = [
            {
                "message_text": f"unique message {i}",
                "perfume_id": None,
                "ambiguous": False,
                "layer": None,
                "created_at": "2026-07-19T09:00:00+00:00",
            }
            for i in range(10)
        ]
        result = _run_with_events(
            lambda: analytics.get_unmatched_queries(days=30, limit=3), events
        )
        assert len(result) == 3


class TestGetAmbiguousQueries:
    def test_only_ambiguous_events_included(self):
        result = _run_with_events(lambda: analytics.get_ambiguous_queries(days=30), MIXED_EVENTS)
        assert len(result) == 1
        assert result[0]["message_text"] == "sauvage vs bleu de chanel"

    def test_sorted_by_created_at_descending(self):
        events = [
            {"message_text": "a", "ambiguous": True, "created_at": "2026-07-18T09:00:00+00:00"},
            {"message_text": "b", "ambiguous": True, "created_at": "2026-07-19T09:00:00+00:00"},
        ]
        result = _run_with_events(lambda: analytics.get_ambiguous_queries(days=30), events)
        assert [r["message_text"] for r in result] == ["b", "a"]

    def test_limit_is_respected(self):
        events = [
            {"message_text": f"msg{i}", "ambiguous": True, "created_at": "2026-07-19T09:00:00+00:00"}
            for i in range(10)
        ]
        result = _run_with_events(lambda: analytics.get_ambiguous_queries(days=30, limit=4), events)
        assert len(result) == 4


class TestGetCatalogStats:
    """No DB mocking needed — pure computation over the live in-memory catalog."""

    def test_totals_are_internally_consistent(self):
        result = asyncio.run(analytics.get_catalog_stats())
        assert result["total_perfumes"] == len(PERFUMES)
        assert result["clones"] + result["originals"] == result["total_perfumes"]

    def test_price_tiers_have_sane_min_max_avg(self):
        result = asyncio.run(analytics.get_catalog_stats())
        assert len(result["price_tiers"]) > 0
        for tier in result["price_tiers"]:
            assert tier["min"] <= tier["avg"] <= tier["max"]
            assert tier["count"] > 0

    def test_brand_breakdown_is_none_or_a_list(self):
        result = asyncio.run(analytics.get_catalog_stats())
        assert result["brand_breakdown"] is None or isinstance(result["brand_breakdown"], list)


class TestLogMessageEvent:
    BASE_KWARGS = dict(
        message_id="wamid_1",
        sender="919876543210",
        message_text="sauvage price",
        perfume_id=_REAL_PID,
        layer="exact",
        confidence=100.0,
        ambiguous=False,
        reply_sent=True,
    )

    def test_noop_when_supabase_unconfigured(self):
        with patch("app.analytics.get_client", return_value=None):
            with patch("app.analytics._insert_message_event") as insert_mock:
                asyncio.run(analytics.log_message_event(**self.BASE_KWARGS))
        insert_mock.assert_not_called()

    def test_inserts_record_with_correct_shape(self):
        with patch("app.analytics.get_client", return_value=MagicMock()):
            with patch("app.analytics._insert_message_event") as insert_mock:
                asyncio.run(analytics.log_message_event(**self.BASE_KWARGS))

        insert_mock.assert_called_once()
        _, record = insert_mock.call_args[0]
        assert record["message_id"] == "wamid_1"
        assert record["perfume_id"] == _REAL_PID
        assert record["layer"] == "exact"
        assert record["ambiguous"] is False

    def test_message_text_truncated_to_2000_chars(self):
        kwargs = {**self.BASE_KWARGS, "message_text": "x" * 3000}
        with patch("app.analytics.get_client", return_value=MagicMock()):
            with patch("app.analytics._insert_message_event") as insert_mock:
                asyncio.run(analytics.log_message_event(**kwargs))

        _, record = insert_mock.call_args[0]
        assert len(record["message_text"]) == 2000

    def test_insert_exception_is_swallowed(self):
        with patch("app.analytics.get_client", return_value=MagicMock()):
            with patch("app.analytics._insert_message_event", side_effect=RuntimeError("db down")):
                asyncio.run(analytics.log_message_event(**self.BASE_KWARGS))  # must not raise
