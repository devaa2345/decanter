"""
Owner-only auth for the dashboard API.

The dashboard frontend logs in against Supabase Auth directly (email +
password, via supabase-js in the browser) and gets back a JWT. Every
/api/admin/* route requires that JWT as a Bearer token; this module verifies
it against Supabase and checks the email matches the configured OWNER_EMAIL —
belt-and-suspenders, since this dashboard is meant for a single owner login.
"""

import asyncio
import logging

from fastapi import Header, HTTPException

from app.config import settings
from app.db import get_client

logger = logging.getLogger(__name__)


async def require_owner(authorization: str | None = Header(default=None)) -> str:
    """
    FastAPI dependency: validates the Bearer JWT against Supabase Auth and
    confirms it belongs to the configured owner account.

    Returns the authenticated user's email on success.
    Raises HTTPException(401/403/503) otherwise.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")

    token = authorization.split(" ", 1)[1].strip()

    client = get_client()
    if client is None:
        raise HTTPException(status_code=503, detail="Dashboard is not configured (Supabase unset)")

    try:
        response = await asyncio.to_thread(client.auth.get_user, token)
        user = response.user if response else None
    except Exception:
        logger.warning("Dashboard auth: token validation failed")
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    if not user or not user.email:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    if settings.OWNER_EMAIL and user.email.lower() != settings.OWNER_EMAIL.lower():
        logger.warning("Dashboard auth: rejected non-owner email %s", user.email)
        raise HTTPException(status_code=403, detail="Not authorized")

    return user.email
