"""
Supabase client singleton.

Analytics logging and the catalog upload/retrain dashboard feature are both
optional add-ons on top of the core WhatsApp bot: if Supabase isn't
configured (no SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY), everything that
depends on this module no-ops instead of raising — the same degrade-gracefully
pattern groq_client.py already uses for a missing GROQ_API_KEY.
"""

import logging

from app.config import settings

logger = logging.getLogger(__name__)

# Private Storage bucket holding each catalog version's full JSON blob.
CATALOG_BUCKET = "catalog-versions"

_client = None
_attempted_init = False


class SupabaseUnavailable(Exception):
    """Raised when a Supabase-backed feature is used without it being configured."""


def get_client():
    """
    Return a cached Supabase client (service_role — server-side only), or
    None if Supabase isn't configured or the client failed to initialize.
    """
    global _client, _attempted_init

    if _attempted_init:
        return _client

    _attempted_init = True

    if not settings.SUPABASE_URL or not settings.SUPABASE_SERVICE_ROLE_KEY:
        logger.info("Supabase not configured — analytics/catalog-upload features disabled")
        return None

    try:
        from supabase import create_client

        _client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)
    except Exception:
        logger.exception("Failed to initialize Supabase client")
        _client = None

    return _client


def is_configured() -> bool:
    """True if Supabase is configured and the client initialized successfully."""
    return get_client() is not None


def require_client():
    """Return a configured Supabase client or raise SupabaseUnavailable."""
    client = get_client()
    if client is None:
        raise SupabaseUnavailable("Supabase is not configured")
    return client
