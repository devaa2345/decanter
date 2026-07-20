"""
FastAPI application — webhook handler + health check.

This is the entry point for the Decanter Price Bot.
Receives inbound WhatsApp messages via Chat Mitra webhooks,
runs the matching pipeline, and sends price card replies.
"""

import asyncio
import hashlib
import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.analytics import log_message_event
from app.chatmitra import send_reply
from app.config import settings
from app.dedup import dedup_cache
from app.formatter import (
    AMBIGUOUS_MESSAGE,
    FALLBACK_MESSAGE,
    NON_TEXT_MESSAGE,
    ORDER_CONFIRMATION_MESSAGE,
    build_price_card,
)
from app.matcher import match_perfume
from app.order_confirmation import is_order_confirmation
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
    Verify the Chat Mitra webhook signature (if configured).

    Chat Mitra signs the raw request body with HMAC-SHA256 using the webhook
    secret and sends the hex digest in the X-Webhook-Signature header — see
    https://chatmitra.com/documentation/whatsapp-business-api/webhooks/.

    Returns True if verification passes or CHATMITRA_WEBHOOK_SECRET is not
    configured (local dev only — must be set before go-live).
    Returns False if verification fails.
    """
    secret = settings.CHATMITRA_WEBHOOK_SECRET
    if not secret:
        # No secret configured — skip verification
        return True

    signature = request.headers.get("x-webhook-signature")

    if not signature:
        logger.warning("Webhook signature header missing")
        return False

    expected = hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(signature, expected)


def _extract_message_data(payload: dict) -> dict | None:
    """
    Extract message fields from a Chat Mitra webhook payload.

    Chat Mitra has one confirmed flat schema (unlike AiSensy, which had to be
    guessed defensively) — see
    https://chatmitra.com/documentation/whatsapp-business-api/webhooks/.
    A webhook can also deliver message.sent / message.failed /
    message.status.updated events if subscribed; only message.received is an
    inbound customer message, so anything else returns None here and is
    silently acknowledged by the caller.

    Returns dict with keys: message_id, sender, message_type, message_text
    Or None if this isn't an inbound text-bearing message event.
    """
    if payload.get("event") != "message.received":
        return None

    message = payload.get("message") or {}
    msg_type = message.get("type", "")
    msg_text = message.get("text", "") if msg_type == "text" else ""

    return {
        "message_id": payload.get("message_id", ""),
        "sender": payload.get("from", ""),
        "message_type": msg_type,
        "message_text": str(msg_text) if msg_text else "",
    }


@app.post("/webhook")
async def webhook_handler(request: Request):
    """
    Main webhook endpoint — receives inbound WhatsApp messages from Chat Mitra.

    Pipeline:
    1. Verify signature (if configured)
    2. Parse payload
    3. Dedup check (covers every reply path below uniformly)
    4. Sanity checks (message type, empty text)
    5. Order-confirmation short-circuit (before the length cutoff — an order
       with many line items can be long, and it must never reach the matcher)
    6. Message-too-long check
    7. Run matching pipeline
    8. Build + send reply, log for analytics
    9. Return 200 immediately
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

    # Step 3: Dedup check — moved ahead of every reply-sending branch below
    # (previously only guarded the matching pipeline, so a retried webhook
    # for a non-text message or an order confirmation could double-reply).
    if dedup_cache.is_duplicate(message_id):
        logger.info("Duplicate message %s — skipping", message_id)
        return Response(status_code=200, content="OK")

    # Step 4a: Non-text message types
    if message_type and message_type != "text":
        logger.info("Non-text message type '%s' from %s — sending prompt", message_type, sender)
        await send_reply(sender, NON_TEXT_MESSAGE)
        return Response(status_code=200, content="OK")

    # Step 4b: Empty or missing message text
    if not message_text or not message_text.strip():
        logger.info("Empty message from %s", sender)
        return Response(status_code=200, content="OK")

    # Step 5: Order-confirmation short-circuit — the "confirm my order"
    # template from the website (order number, line items, order link).
    # This runs BEFORE the length cutoff below: an order with many line
    # items can legitimately exceed MAX_MESSAGE_LENGTH, and it must never
    # fall through to the perfume matcher (the line items are real perfume
    # names and would otherwise get quoted a price card instead of an
    # order acknowledgment).
    if is_order_confirmation(message_text):
        success = await send_reply(sender, ORDER_CONFIRMATION_MESSAGE)
        await log_message_event(
            message_id=message_id,
            sender=sender,
            message_text=message_text,
            perfume_id=None,
            layer="order_confirmation",
            confidence=None,
            ambiguous=False,
            reply_sent=success,
        )
        logger.info("Order confirmation from %s — sent acknowledgment (sent=%s)", sender, success)
        return Response(status_code=200, content="OK")

    # Step 6: Message too long
    if len(message_text) > settings.MAX_MESSAGE_LENGTH:
        logger.info("Message too long (%d chars) from %s", len(message_text), sender)
        await send_reply(sender, FALLBACK_MESSAGE)
        return Response(status_code=200, content="OK")

    # Step 7: Run matching pipeline
    result = await match_perfume(message_text)

    # Step 8: Build reply
    if result.ambiguous:
        reply_text = AMBIGUOUS_MESSAGE
    elif result.perfume_id:
        reply_text = build_price_card(result.perfume_id)
    else:
        reply_text = FALLBACK_MESSAGE

    # Step 9: Send reply
    success = await send_reply(sender, reply_text)

    # Step 10: Log the event for the analytics dashboard (best-effort, never
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
