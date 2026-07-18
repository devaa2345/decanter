"""
FastAPI application — webhook handler + health check.

This is the entry point for the Decanter Price Bot.
Receives inbound WhatsApp messages via AiSensy webhooks,
runs the matching pipeline, and sends price card replies.
"""

import asyncio
import hashlib
import hmac
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.aisensy import send_reply
from app.analytics import log_message_event
from app.config import settings
from app.dedup import dedup_cache
from app.formatter import (
    AMBIGUOUS_MESSAGE,
    FALLBACK_MESSAGE,
    NON_TEXT_MESSAGE,
    build_price_card,
)
from app.matcher import match_perfume
from app.routes_admin import router as admin_router

# --- Logging setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Startup: best-effort pull the active catalog version from Supabase down
    to catalog_data.json before serving traffic, so a redeploy picks up the
    latest dashboard-published catalog instead of whatever was baked into
    the deploy image. Falls back silently to the bundled file — see
    app.catalog_upload.sync_active_catalog_to_disk.
    """
    try:
        from app.catalog_upload import sync_active_catalog_to_disk

        synced = await asyncio.to_thread(sync_active_catalog_to_disk)
        if synced:
            logger.info("Loaded active catalog version from Supabase")
    except Exception:
        logger.exception("Startup catalog sync from Supabase failed — using bundled catalog_data.json")

    yield


# --- FastAPI app ---
app = FastAPI(
    title="Decanter Price Bot",
    description="WhatsApp price-query bot for Sovereign Scents",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(admin_router)

app.mount(
    "/dashboard",
    StaticFiles(directory="app/static/dashboard", html=True),
    name="dashboard",
)


@app.get("/", include_in_schema=False)
async def root_redirect():
    return RedirectResponse(url="/dashboard/index.html")


@app.get("/api/config")
async def public_config():
    """
    Public runtime config for the dashboard frontend — safe to expose: the
    anon key can only start an Auth login, and RLS locks every table down
    for anon access (see supabase/migrations/0001_init.sql). No secrets here.
    """
    return {
        "supabase_url": settings.SUPABASE_URL,
        "supabase_anon_key": settings.SUPABASE_ANON_KEY,
        "configured": bool(settings.SUPABASE_URL and settings.SUPABASE_ANON_KEY),
    }


@app.get("/health")
async def health_check():
    """
    Health check endpoint for UptimeRobot pings.

    Returns 200 OK with no side effects, no external API calls.
    Keeps the Render free-tier service warm.
    """
    return {"status": "ok"}


def _verify_webhook_signature(request: Request, body: bytes) -> bool:
    """
    Verify the AiSensy webhook signature (if configured).

    Returns True if verification passes or is not configured.
    Returns False if verification fails.
    """
    secret = settings.AISENSY_WEBHOOK_SECRET
    if not secret:
        # No secret configured — skip verification
        return True

    # Check for signature header (common patterns)
    signature = (
        request.headers.get("x-hub-signature-256")
        or request.headers.get("x-signature")
        or request.headers.get("x-aisensy-signature")
    )

    if not signature:
        logger.warning("Webhook signature header missing")
        return False

    # Strip "sha256=" prefix if present
    if signature.startswith("sha256="):
        signature = signature[7:]

    # Calculate expected HMAC
    expected = hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(signature, expected)


def _extract_message_data(payload: dict) -> dict | None:
    """
    Extract message fields from the AiSensy webhook payload.

    Tries multiple payload structures defensively since the exact
    format must be confirmed with AiSensy support.

    Returns dict with keys: message_id, sender, message_type, message_text
    Or None if the payload is not a valid inbound message.
    """
    # --- Attempt 1: Standard WhatsApp Cloud API nested structure ---
    # AiSensy may forward the raw WhatsApp Cloud API payload
    entry = payload.get("entry", [{}])
    if entry:
        changes = entry[0].get("changes", [{}]) if isinstance(entry, list) else []
        if changes:
            value = changes[0].get("value", {})
            messages = value.get("messages", [])
            if messages:
                msg = messages[0]
                contacts = value.get("contacts", [{}])
                sender_name = contacts[0].get("profile", {}).get("name", "") if contacts else ""
                return {
                    "message_id": msg.get("id", ""),
                    "sender": msg.get("from", ""),
                    "sender_name": sender_name,
                    "message_type": msg.get("type", ""),
                    "message_text": msg.get("text", {}).get("body", "")
                        if msg.get("type") == "text"
                        else "",
                }

    # --- Attempt 2: Flat AiSensy structure ---
    # Based on AiSensy webhook documentation research
    if "from" in payload or "sender" in payload:
        msg_type = payload.get("type", "")
        if not msg_type:
            msg_obj = payload.get("message", {})
            if isinstance(msg_obj, dict):
                msg_type = msg_obj.get("type", "text")
            else:
                msg_type = "text"

        msg_text = ""
        if msg_type == "text":
            msg_obj = payload.get("message", {})
            if isinstance(msg_obj, dict):
                text_obj = msg_obj.get("text", {})
                if isinstance(text_obj, dict):
                    msg_text = text_obj.get("body", "")
                elif isinstance(text_obj, str):
                    msg_text = text_obj
            elif isinstance(msg_obj, str):
                msg_text = msg_obj

            # Fallback: top-level text/body fields
            if not msg_text:
                msg_text = payload.get("text", payload.get("body", payload.get("message", "")))
                if isinstance(msg_text, dict):
                    msg_text = msg_text.get("body", "")

        return {
            "message_id": payload.get("id", payload.get("message_id", str(time.time()))),
            "sender": payload.get("from", payload.get("sender", "")),
            "sender_name": payload.get("senderName", payload.get("name", "")),
            "message_type": msg_type,
            "message_text": str(msg_text) if msg_text else "",
        }

    logger.warning("Could not parse webhook payload: %s", str(payload)[:300])
    return None


@app.post("/webhook")
async def webhook_handler(request: Request):
    """
    Main webhook endpoint — receives inbound WhatsApp messages from AiSensy.

    Pipeline:
    1. Verify signature (if configured)
    2. Parse payload
    3. Sanity checks (message length, type, dedup)
    4. Run matching pipeline
    5. Build reply text
    6. Send via AiSensy
    7. Return 200 immediately
    """
    # Read raw body for signature verification
    body = await request.body()

    # Step 1: Verify webhook signature
    if not _verify_webhook_signature(request, body):
        logger.warning("Webhook signature verification failed")
        return Response(status_code=403, content="Forbidden")

    # Parse JSON payload
    try:
        payload = await request.json()
    except Exception:
        logger.warning("Invalid JSON in webhook payload")
        return Response(status_code=200, content="OK")

    # Step 2: Extract message data
    msg_data = _extract_message_data(payload)
    if not msg_data:
        # Not a recognizable message event — silently acknowledge
        return Response(status_code=200, content="OK")

    message_id = msg_data["message_id"]
    sender = msg_data["sender"]
    message_type = msg_data["message_type"]
    message_text = msg_data["message_text"]

    logger.info(
        "Inbound message: id=%s, from=%s, type=%s, text=%s",
        message_id,
        sender,
        message_type,
        message_text[:100] if message_text else "(empty)",
    )

    # Step 3a: Non-text message types
    if message_type and message_type != "text":
        logger.info("Non-text message type '%s' from %s — sending prompt", message_type, sender)
        await send_reply(sender, NON_TEXT_MESSAGE)
        return Response(status_code=200, content="OK")

    # Step 3b: Empty or missing message text
    if not message_text or not message_text.strip():
        logger.info("Empty message from %s", sender)
        return Response(status_code=200, content="OK")

    # Step 3c: Message too long
    if len(message_text) > settings.MAX_MESSAGE_LENGTH:
        logger.info("Message too long (%d chars) from %s", len(message_text), sender)
        await send_reply(sender, FALLBACK_MESSAGE)
        return Response(status_code=200, content="OK")

    # Step 4: Dedup check
    if dedup_cache.is_duplicate(message_id):
        logger.info("Duplicate message %s — skipping", message_id)
        return Response(status_code=200, content="OK")

    # Step 5: Run matching pipeline
    result = await match_perfume(message_text)

    # Step 6: Build reply
    if result.ambiguous:
        reply_text = AMBIGUOUS_MESSAGE
    elif result.perfume_id:
        reply_text = build_price_card(result.perfume_id)
    else:
        reply_text = FALLBACK_MESSAGE

    # Step 7: Send reply
    success = await send_reply(sender, reply_text)

    # Step 8: Log the event for the analytics dashboard (best-effort, never
    # blocks or fails the customer-facing reply — see app/analytics.py).
    await log_message_event(
        message_id=message_id,
        sender=sender,
        message_text=message_text,
        perfume_id=result.perfume_id,
        layer=result.layer,
        confidence=result.confidence,
        ambiguous=result.ambiguous,
        reply_sent=success,
    )

    logger.info(
        "Reply sent: to=%s, matched=%s, layer=%s, confidence=%s, sent=%s",
        sender,
        result.perfume_id or "(none)",
        result.layer or "(none)",
        result.confidence,
        success,
    )

    return Response(status_code=200, content="OK")


# --- Webhook verification (GET) for initial setup ---
@app.get("/webhook")
async def webhook_verify(request: Request):
    """
    Handle webhook verification challenge from AiSensy/WhatsApp.

    Some BSPs require responding to a GET request with a challenge token
    during initial webhook setup.
    """
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and challenge:
        logger.info("Webhook verification challenge received")
        return Response(content=challenge, media_type="text/plain")

    return Response(status_code=200, content="OK")
