"""
Unit tests for the owner-only dashboard auth dependency (app/auth.py).

require_owner is called directly as a plain async function (bypassing
FastAPI's dependency-injection Header() resolution, which only matters when
a real request comes in) via asyncio.run(), per this codebase's convention
of not using pytest.mark.asyncio. app.auth.get_client is always explicitly
patched — this environment's real .env has live Supabase/OWNER_EMAIL values
configured, and tests must not depend on or leak those.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app import auth
from app.auth import require_owner


def _remote_request(headers=None):
    """A request that could have come from the internet, which is what
    every test here is about. app.auth grants an unauthenticated pass ONLY
    to loopback requests made while Supabase is unconfigured (see
    _is_local_only), so these must not look local or they would all take
    that path instead of the one under test."""
    request = MagicMock()
    request.client.host = "203.0.113.7"
    request.headers = headers or {"x-forwarded-for": "203.0.113.7"}
    return request


def _fake_user_response(email):
    user = MagicMock()
    user.email = email
    response = MagicMock()
    response.user = user
    return response


def _fake_client(get_user_return=None, get_user_exception=None):
    """client.auth.get_user is called via asyncio.to_thread, so it must be
    a plain sync callable, not an AsyncMock."""
    client = MagicMock()
    client.auth.get_user = MagicMock(return_value=get_user_return, side_effect=get_user_exception)
    return client


class TestMissingOrMalformedHeader:
    def test_missing_header_raises_401(self):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(require_owner(_remote_request(), authorization=None))
        assert exc_info.value.status_code == 401

    def test_header_without_bearer_prefix_raises_401(self):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(require_owner(_remote_request(), authorization="Token abc123"))
        assert exc_info.value.status_code == 401

    def test_empty_string_header_raises_401(self):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(require_owner(_remote_request(), authorization=""))
        assert exc_info.value.status_code == 401

    def test_bearer_prefix_is_case_insensitive(self):
        client = _fake_client(get_user_return=_fake_user_response("owner@example.com"))
        with patch.object(auth.settings, "OWNER_EMAIL", ""):
            with patch("app.auth.get_client", return_value=client):
                result = asyncio.run(require_owner(_remote_request(), authorization="bearer sometoken"))
        assert result == "owner@example.com"


class TestSupabaseUnavailable:
    def test_unconfigured_supabase_raises_503(self):
        with patch("app.auth.get_client", return_value=None):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(require_owner(_remote_request(), authorization="Bearer sometoken"))
        assert exc_info.value.status_code == 503


class TestTokenValidation:
    def test_get_user_exception_raises_401(self):
        client = _fake_client(get_user_exception=RuntimeError("invalid jwt"))
        with patch("app.auth.get_client", return_value=client):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(require_owner(_remote_request(), authorization="Bearer badtoken"))
        assert exc_info.value.status_code == 401

    def test_null_response_raises_401(self):
        client = _fake_client(get_user_return=None)
        with patch("app.auth.get_client", return_value=client):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(require_owner(_remote_request(), authorization="Bearer sometoken"))
        assert exc_info.value.status_code == 401

    def test_response_with_no_user_raises_401(self):
        response = MagicMock()
        response.user = None
        client = _fake_client(get_user_return=response)
        with patch("app.auth.get_client", return_value=client):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(require_owner(_remote_request(), authorization="Bearer sometoken"))
        assert exc_info.value.status_code == 401

    def test_user_with_no_email_raises_401(self):
        client = _fake_client(get_user_return=_fake_user_response(None))
        with patch("app.auth.get_client", return_value=client):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(require_owner(_remote_request(), authorization="Bearer sometoken"))
        assert exc_info.value.status_code == 401

    def test_token_is_parsed_from_header(self):
        client = _fake_client(get_user_return=_fake_user_response("owner@example.com"))
        with patch.object(auth.settings, "OWNER_EMAIL", ""):
            with patch("app.auth.get_client", return_value=client):
                asyncio.run(require_owner(_remote_request(), authorization="Bearer  my-jwt-token  "))
        client.auth.get_user.assert_called_once_with("my-jwt-token")


class TestOwnerEmailGate:
    def test_matching_owner_email_succeeds(self):
        client = _fake_client(get_user_return=_fake_user_response("owner@example.com"))
        with patch.object(auth.settings, "OWNER_EMAIL", "owner@example.com"):
            with patch("app.auth.get_client", return_value=client):
                result = asyncio.run(require_owner(_remote_request(), authorization="Bearer sometoken"))
        assert result == "owner@example.com"

    def test_matching_is_case_insensitive(self):
        client = _fake_client(get_user_return=_fake_user_response("Owner@Example.com"))
        with patch.object(auth.settings, "OWNER_EMAIL", "owner@example.com"):
            with patch("app.auth.get_client", return_value=client):
                result = asyncio.run(require_owner(_remote_request(), authorization="Bearer sometoken"))
        assert result == "Owner@Example.com"

    def test_mismatched_email_raises_403(self):
        client = _fake_client(get_user_return=_fake_user_response("someone-else@example.com"))
        with patch.object(auth.settings, "OWNER_EMAIL", "owner@example.com"):
            with patch("app.auth.get_client", return_value=client):
                with pytest.raises(HTTPException) as exc_info:
                    asyncio.run(require_owner(_remote_request(), authorization="Bearer sometoken"))
        assert exc_info.value.status_code == 403

    def test_blank_owner_email_allows_any_authenticated_user(self):
        """Documented (and previously flagged) behavior: an unset
        OWNER_EMAIL means ANY successfully authenticated Supabase user
        passes, not just a specific owner — belt-and-suspenders is only
        actually fastened once OWNER_EMAIL is configured."""
        client = _fake_client(get_user_return=_fake_user_response("literally-anyone@example.com"))
        with patch.object(auth.settings, "OWNER_EMAIL", ""):
            with patch("app.auth.get_client", return_value=client):
                result = asyncio.run(require_owner(_remote_request(), authorization="Bearer sometoken"))
        assert result == "literally-anyone@example.com"


class TestLocalModeCannotBeReachedFromTheInternet:
    """
    app.auth grants an unauthenticated pass so the console is usable on a
    developer machine, where Supabase is unset and no token can ever be
    issued. That pass is the one piece of this module that could expose the
    catalog, so every condition it depends on is pinned here.

    All three must hold: no Supabase, loopback client, and no proxy header.
    A deployed instance fails the first before the others are considered.
    """

    def _request(self, host="127.0.0.1", headers=None):
        request = MagicMock()
        request.client.host = host
        request.headers = headers or {}
        return request

    def test_loopback_with_no_supabase_is_allowed(self):
        with patch("app.auth.get_client", return_value=None):
            assert asyncio.run(require_owner(self._request(), authorization=None)) == auth.LOCAL_OWNER

    @pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
    def test_every_loopback_spelling_counts(self, host):
        with patch("app.auth.get_client", return_value=None):
            assert asyncio.run(require_owner(self._request(host), authorization=None)) == auth.LOCAL_OWNER

    def test_a_configured_supabase_always_requires_a_token(self):
        """The condition that matters in production: a real deployment has
        Supabase set, so the pass is never even considered."""
        with patch("app.auth.get_client", return_value=_fake_client()):
            with pytest.raises(HTTPException) as excinfo:
                asyncio.run(require_owner(self._request(), authorization=None))
        assert excinfo.value.status_code == 401

    @pytest.mark.parametrize("host", ["203.0.113.7", "10.0.0.4", "", None])
    def test_a_non_loopback_client_is_refused(self, host):
        with patch("app.auth.get_client", return_value=None):
            with pytest.raises(HTTPException) as excinfo:
                asyncio.run(require_owner(self._request(host), authorization=None))
        assert excinfo.value.status_code in (401, 503)

    @pytest.mark.parametrize("header", ["x-forwarded-for", "x-real-ip"])
    def test_a_proxied_request_is_refused_even_from_loopback(self, header):
        """Anything reaching a deployed app comes through a proxy that sets
        one of these. A loopback client address alone is not proof that
        nothing forwarded the request."""
        with patch("app.auth.get_client", return_value=None):
            with pytest.raises(HTTPException) as excinfo:
                asyncio.run(
                    require_owner(self._request(headers={header: "203.0.113.7"}), authorization=None)
                )
        assert excinfo.value.status_code in (401, 503)

    def test_a_missing_client_is_refused(self):
        """request.client is None for some transports — absence of an
        address is not evidence of a local one."""
        request = MagicMock()
        request.client = None
        request.headers = {}
        with patch("app.auth.get_client", return_value=None):
            with pytest.raises(HTTPException) as excinfo:
                asyncio.run(require_owner(request, authorization=None))
        assert excinfo.value.status_code in (401, 503)
