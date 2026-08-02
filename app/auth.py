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

from fastapi import Header, HTTPException, Request

from app.config import settings
from app.db import get_client

logger = logging.getLogger(__name__)


LOCAL_OWNER = "local@localhost"


def _is_local_only(request: Request) -> bool:
    """
    True when this request cannot be coming from the internet AND there is
    no owner account to authenticate against.

    The dashboard authenticates against Supabase Auth. With Supabase unset
    there is no account, no token can ever be issued, and every /api/admin
    route answers 401 forever — which would make the console unusable on a
    developer's machine, including the chat tester that used to run with no
    auth at all as its own local script.

    Three conditions, all required, because getting this wrong exposes the
    catalog to the internet:

      1. Supabase is not configured — so this is not a real deployment, and
         there is no owner account being bypassed.
      2. The request came from loopback.
      3. No X-Forwarded-For header — anything reaching a deployed app goes
         through a proxy that sets one, so its absence is a second, harder
         signal that nothing forwarded this.

    A deployed instance has Supabase configured and fails condition 1 before
    the others are even considered.
    """
    if get_client() is not None:
        return False
    if request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip"):
        return False
    host = (request.client.host if request.client else "") or ""
    return host in {"127.0.0.1", "::1", "localhost"}


async def require_owner(
    request: Request, authorization: str | None = Header(default=None)
) -> str:
    """
    FastAPI dependency: validates the Bearer JWT against Supabase Auth and
    confirms it belongs to the configured owner account.

    Returns the authenticated user's email on success.
    Raises HTTPException(401/403/503) otherwise.
    """
    if _is_local_only(request):
        return LOCAL_OWNER

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
