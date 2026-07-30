"""
Unit tests for the Supabase client singleton (app/db.py).

get_client() caches its result in module globals (_client, _attempted_init)
after the first call — every test resets and restores that cache around
itself so tests can't leak state into each other or into the rest of the
suite (other test files rely on the real cached client from this
environment's actual Supabase config).
"""

from unittest.mock import MagicMock, patch

import pytest

from app import db
from app.db import SupabaseUnavailable


@pytest.fixture(autouse=True)
def reset_db_singleton():
    original_client, original_attempted = db._client, db._attempted_init
    db._client, db._attempted_init = None, False
    yield
    db._client, db._attempted_init = original_client, original_attempted


class TestGetClientUnconfigured:
    def test_none_when_url_missing(self):
        with patch.object(db.settings, "SUPABASE_URL", ""):
            with patch.object(db.settings, "SUPABASE_SERVICE_ROLE_KEY", "some_key"):
                assert db.get_client() is None

    def test_none_when_service_role_key_missing(self):
        with patch.object(db.settings, "SUPABASE_URL", "https://example.supabase.co"):
            with patch.object(db.settings, "SUPABASE_SERVICE_ROLE_KEY", ""):
                assert db.get_client() is None

    def test_none_when_both_missing(self):
        with patch.object(db.settings, "SUPABASE_URL", ""):
            with patch.object(db.settings, "SUPABASE_SERVICE_ROLE_KEY", ""):
                assert db.get_client() is None


class TestGetClientCaching:
    def test_result_cached_across_calls_without_reevaluating_settings(self):
        with patch.object(db.settings, "SUPABASE_URL", ""):
            first = db.get_client()
        assert first is None

        # If get_client() re-read settings on the second call instead of
        # using the cache, this would attempt a real client construction.
        with patch.object(db.settings, "SUPABASE_URL", "https://example.supabase.co"):
            with patch.object(db.settings, "SUPABASE_SERVICE_ROLE_KEY", "fake_key"):
                second = db.get_client()

        assert second is None
        assert first is second


class TestGetClientSuccess:
    def test_successful_init_returns_and_caches_client(self):
        fake_client = MagicMock()
        with patch.object(db.settings, "SUPABASE_URL", "https://example.supabase.co"):
            with patch.object(db.settings, "SUPABASE_SERVICE_ROLE_KEY", "fake_key"):
                with patch("supabase.create_client", return_value=fake_client) as create_mock:
                    result = db.get_client()

        assert result is fake_client
        create_mock.assert_called_once_with("https://example.supabase.co", "fake_key")

    def test_init_exception_returns_none_not_raised(self):
        with patch.object(db.settings, "SUPABASE_URL", "https://example.supabase.co"):
            with patch.object(db.settings, "SUPABASE_SERVICE_ROLE_KEY", "fake_key"):
                with patch("supabase.create_client", side_effect=RuntimeError("network error")):
                    result = db.get_client()  # must not raise

        assert result is None


class TestIsConfigured:
    def test_false_when_unconfigured(self):
        with patch.object(db.settings, "SUPABASE_URL", ""):
            assert db.is_configured() is False

    def test_true_when_configured(self):
        with patch.object(db.settings, "SUPABASE_URL", "https://example.supabase.co"):
            with patch.object(db.settings, "SUPABASE_SERVICE_ROLE_KEY", "fake_key"):
                with patch("supabase.create_client", return_value=MagicMock()):
                    assert db.is_configured() is True


class TestRequireClient:
    def test_raises_supabase_unavailable_when_unconfigured(self):
        with patch.object(db.settings, "SUPABASE_URL", ""):
            with pytest.raises(SupabaseUnavailable):
                db.require_client()

    def test_returns_client_when_configured(self):
        fake_client = MagicMock()
        with patch.object(db.settings, "SUPABASE_URL", "https://example.supabase.co"):
            with patch.object(db.settings, "SUPABASE_SERVICE_ROLE_KEY", "fake_key"):
                with patch("supabase.create_client", return_value=fake_client):
                    assert db.require_client() is fake_client
