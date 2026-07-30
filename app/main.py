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

from app.analytics import is_first_contact, log_message_event
from app.chatmitra import send_reply
from app.config import settings
from app.conversation import record_bot_reply, record_customer_message, recent_turns
from app.dedup import dedup_cache
from app.formatter import (
    FALLBACK_MESSAGE,
    NON_TEXT_MESSAGE,
    ORDER_CONFIRMATION_MESSAGE,
    WELCOME_MESSAGE,
    build_multi_price_card,
    build_not_in_stock_message,
    build_price_card,
)
from app.greeting import is_catalog_request, is_greeting_or_catalog_request
from app.handoff import is_paused, record_own_send, start_pause, was_sent_by_bot
from app.matcher import (
    MatchResult,
    extract_requested_size_ml,
    has_confident_keyword_match,
    match_perfume,
    normalize_message,
)
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

    # Build the matcher's index now rather than lazily on the first inbound
    # message, so no customer pays for it. It is quick (~20ms over 1,200
    # entries) and derived entirely from the catalog just loaded above.
    try:
        from app.name_index import build_index

        await asyncio.to_thread(build_index)
        logger.info("Perfume name index built")
    except Exception:
        logger.exception("Failed to pre-build the perfume name index — it will build on first use")

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
    inbound customer message. message.sent is handled separately, before
    this function ever runs (see _handle_message_sent) — everything else
    returns None here and is silently acknowledged by the caller.

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


def _log_inbound(message_id: str, sender: str, message_type: str, message_text: str) -> None:
    """
    Full, untruncated inbound log for real-time debugging. Safe to log in
    full — inbound text is naturally bounded by MAX_MESSAGE_LENGTH before it
    ever gets this far in most paths, and even the order-confirmation
    template (which bypasses that cutoff) is short enough to log whole.
    """
    logger.info(
        ">>> INBOUND  id=%s  from=%s  type=%s\n"
        "----- MESSAGE TEXT START -----\n%s\n----- MESSAGE TEXT END -----",
        message_id,
        sender,
        message_type or "(unknown)",
        message_text if message_text else "(empty)",
    )


def _log_outbound(sender: str, reply_text: str, success: bool, **context) -> None:
    """Full outbound reply log — the exact text sent, plus any match context
    the caller has (layer, confidence, matched perfume, etc.)."""
    extra = "  ".join(f"{k}={v}" for k, v in context.items())
    logger.info(
        "<<< OUTBOUND  to=%s  sent=%s%s\n"
        "----- REPLY TEXT START -----\n%s\n----- REPLY TEXT END -----",
        sender,
        success,
        f"  {extra}" if extra else "",
        reply_text,
    )


async def _send_and_record(sender: str, reply_text: str) -> bool:
    """
    send_reply, plus recording the send so app.handoff can recognize Chat
    Mitra echoing it back as a "message.sent" webhook event (see
    _handle_message_sent below) instead of mistaking the bot's own reply
    for the owner taking over the conversation. Every reply this bot ever
    sends goes through here — there's no other call to send_reply in this
    module — so that recognition is never accidentally skipped for one
    reply path but not another.
    """
    success = await send_reply(sender, reply_text)
    if success:
        record_own_send(sender, reply_text)
    return success


async def _handle_message_sent(payload: dict) -> None:
    """
    Handle a "message.sent" webhook event — Chat Mitra reports every
    outbound message this way regardless of whether it went out through
    this bot's own API call (see _send_and_record above) or because the
    owner personally typed a reply to the customer through Chat Mitra's own
    dashboard/app. Their documentation doesn't expose a field distinguishing
    the two (see app.handoff's module docstring), so an outbound message
    that doesn't match one of the bot's own recent sends is treated as the
    owner taking over — which pauses the bot for that customer (see
    app.handoff.start_pause / the pause-check branch in webhook_handler).
    """
    to = payload.get("to", "")
    if not to:
        return

    message = payload.get("message") or {}
    text = message.get("text", "") if message.get("type") == "text" else ""

    if was_sent_by_bot(to, text):
        return

    logger.info(
        "Human agent message detected to %s — pausing bot for this conversation", to
    )
    await start_pause(to)


# Beyond this many words, a message is a conversation rather than someone
# naming a product, and asking the LLM about it is a wasted call.
_UNSTOCKED_MAX_WORDS = 10


async def _unstocked_perfume_name(message_text: str, history: list[dict] | None) -> str | None:
    """
    The perfume this customer is asking for, when the catalog has no such
    product — or None if they weren't naming one.

    Two gates before the LLM is asked anything, because this runs on
    messages that would otherwise cost nothing at all:

      1. Length. A long message is a conversation, not a product name.
      2. Content. The message must contain at least one word that isn't
         conversational filler. "thanks bhai", "ok", "kab aayega" are
         entirely filler and never reach Groq.

    Returns None on any doubt or failure. The behaviour being improved on is
    silence, so failing back to silence costs nothing.
    """
    from app.groq_client import identify_unstocked_perfume
    from app.name_index import MESSAGE_STOPWORDS, tokenize_message

    if len(message_text.split()) > _UNSTOCKED_MAX_WORDS:
        return None

    tokens = tokenize_message(message_text)
    if not any(len(t) >= 3 and t not in MESSAGE_STOPWORDS for t in tokens):
        return None

    result = await identify_unstocked_perfume(message_text, history)
    if result is None:
        return None

    logger.info("Customer asked for a perfume we don't stock: %r", result.name)
    return result.name


@app.post("/webhook")
async def webhook_handler(request: Request):
    """
    Main webhook endpoint — receives inbound WhatsApp messages from Chat Mitra.

    Pipeline:
    1. Verify signature (if configured)
    2. Parse payload
    2b. message.sent short-circuit (detects the owner personally messaging
        a customer directly, to start a human-handoff pause — see
        _handle_message_sent)
    3. Dedup check (covers every reply path below uniformly)
    3a. Human-handoff pause short-circuit (the bot stays completely silent
        for a sender the owner is already personally handling)
    3b. First-contact welcome short-circuit (the very first message ever
        seen from this sender gets WELCOME_MESSAGE regardless of content)
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

    # Step 2b: message.sent events (see _handle_message_sent) are handled
    # here, before _extract_message_data — which only recognizes
    # message.received and would otherwise silently swallow this one too.
    if payload.get("event") == "message.sent":
        await _handle_message_sent(payload)
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

    _log_inbound(message_id, sender, message_type, message_text)

    # Step 3: Dedup check — moved ahead of every reply-sending branch below
    # (previously only guarded the matching pipeline, so a retried webhook
    # for a non-text message or an order confirmation could double-reply).
    if dedup_cache.is_duplicate(message_id):
        logger.info("Duplicate message %s — skipping", message_id)
        return Response(status_code=200, content="OK")

    # Step 3a: Human-handoff pause — the owner already personally messaged
    # this sender directly (see _handle_message_sent), so the bot stays
    # completely out of the conversation until the configured window
    # elapses. Checked before even the first-contact welcome below, since
    # the owner taking over should override everything else the bot might
    # otherwise do. Still logged for the dashboard (excluded from
    # "unmatched"/catalog-gap counts — see app.analytics), just never
    # replied to.
    if await is_paused(sender):
        logger.info(
            "<<< SILENT  to=%s  (human handoff pause active — bot staying out)", sender
        )
        await log_message_event(
            message_id=message_id,
            sender=sender,
            message_text=message_text,
            perfume_id=None,
            layer="human_handoff_pause",
            confidence=None,
            ambiguous=False,
            reply_sent=False,
        )
        return Response(status_code=200, content="OK")

    # Step 3b: First-contact welcome — the very first message the bot has
    # ever received from this sender gets WELCOME_MESSAGE no matter what it
    # says (image, empty, order confirmation, gibberish, all of it). Checked
    # before every other branch below so none of them can pre-empt it, and
    # returns immediately so the matching pipeline never runs for it.
    if await is_first_contact(sender):
        success = await _send_and_record(sender, WELCOME_MESSAGE)
        _log_outbound(sender, WELCOME_MESSAGE, success, reason="welcome_first_contact")
        await log_message_event(
            message_id=message_id,
            sender=sender,
            message_text=message_text,
            perfume_id=None,
            layer="welcome_first_contact",
            confidence=None,
            ambiguous=False,
            reply_sent=success,
        )
        return Response(status_code=200, content="OK")

    # Step 4a: Non-text message types
    if message_type and message_type != "text":
        success = await _send_and_record(sender, NON_TEXT_MESSAGE)
        _log_outbound(sender, NON_TEXT_MESSAGE, success, reason="non_text_message_type")
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
        success = await _send_and_record(sender, ORDER_CONFIRMATION_MESSAGE)
        _log_outbound(sender, ORDER_CONFIRMATION_MESSAGE, success, reason="order_confirmation")
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
        return Response(status_code=200, content="OK")

    # Step 6: Message too long
    if len(message_text) > settings.MAX_MESSAGE_LENGTH:
        success = await _send_and_record(sender, FALLBACK_MESSAGE)
        _log_outbound(sender, FALLBACK_MESSAGE, success, reason=f"too_long_{len(message_text)}_chars")
        return Response(status_code=200, content="OK")

    # Step 7: Run matching pipeline.
    #
    # A catalog-phrase message ("catalogue", "send me the catalogue
    # please") is vetoed straight to the catalog reply here, BEFORE Groq or
    # the fuzzy matcher ever run — confirmed in production that both of
    # those layers could hijack it with a wrong guess instead: Groq's
    # candidate shortlist is never actually empty (it's always the top 25
    # nearest-by-fuzzy-score perfumes, however irrelevant), so it would
    # occasionally return one anyway despite nothing really matching, and
    # separately the fuzzy matcher scored "please" against the keyword
    # "pleasure" at 85.7% and matched it. has_confident_keyword_match is a
    # narrow escape hatch for the rare case a catalog phrase and a real
    # product both appear together (e.g. "show me sauvage price" must
    # still return the Sauvage price card, not the catalog link).
    #
    # history is the recent conversation with THIS sender (see
    # app.conversation) — it lets a follow-up that names no perfume at all
    # ("and 5ml?", "how much for the second one") resolve against the card
    # the customer is replying to, and lets Groq read intent in context
    # instead of from one isolated sentence.
    history = await recent_turns(sender)
    record_customer_message(sender, message_text)

    if is_catalog_request(message_text) and not has_confident_keyword_match(message_text):
        result = MatchResult()
    else:
        result = await match_perfume(message_text, history=history)

    # Step 8: Build reply — silence by default. Every unmatched message used
    # to get the catalog fallback, which spends a Chat Mitra conversation
    # credit on things like "thanks" or "order kab aayega" that aren't
    # asking about a perfume at all. Now only a greeting/explicit catalog
    # ask or an actual perfume match gets a reply; anything else stays
    # silent — but is still logged below so the "catalog gaps" dashboard
    # still sees what customers asked that the bot didn't answer.
    #
    # matched_perfume_ids (2+ perfumes) takes priority over the ambiguous
    # flag alone: it's populated whenever the customer's message resolved
    # to multiple real candidates — whether they clearly named several
    # distinct products or a single mention was ambiguous among close
    # variants — and either way the reply shows a full card for each.
    #
    # requested_ml: if the customer named a specific ml size ("9pm rebel
    # 3ml"), the reply shows only that size's price with delivery cost
    # added per region and the grand total shown alongside — see
    # app.formatter.build_price_card/build_multi_price_card — instead of
    # the full size grid. Parsed independently of which layer matched the
    # perfume(s), so it applies the same way whether Groq or the
    # deterministic fallback found the match.
    #
    # sizes: a customer ordering several decants sizes them individually
    # ("9pm rebel 3ml, khamrah 5ml, kaaf 10ml"). Each product takes the size
    # written next to its own name; requested_ml stays the fallback for any
    # product they did not size — see app.matcher.sizes_per_perfume.
    requested_ml = extract_requested_size_ml(normalize_message(message_text))
    sizes = result.sizes

    if result.matched_perfume_ids:
        reply_text = build_multi_price_card(
            result.matched_perfume_ids,
            result.opening,
            result.closing,
            requested_ml=requested_ml,
            sizes=sizes,
        )
    elif result.perfume_id:
        reply_text = build_price_card(
            result.perfume_id, result.opening, result.closing, requested_ml=requested_ml
        )
    elif is_greeting_or_catalog_request(message_text):
        reply_text = FALLBACK_MESSAGE
    else:
        # Nothing in the catalog matched. Before falling silent, check
        # whether the customer was nonetheless naming a perfume — one we
        # simply don't stock. Silence there reads as being ignored
        # mid-conversation, and the customer is a real buyer who just told
        # us exactly what they want. See _unstocked_perfume_name.
        unstocked = await _unstocked_perfume_name(message_text, history)
        if unstocked is not None:
            reply_text = build_not_in_stock_message(unstocked)
            result.layer = "unstocked_perfume"
        else:
            reply_text = None

    # Step 9: Send reply (skipped entirely when staying silent)
    if reply_text is not None:
        success = await _send_and_record(sender, reply_text)
        # Remember which perfumes this reply showed, in the order shown —
        # that ordering is what makes a later "the second one" resolvable.
        record_bot_reply(
            sender,
            reply_text,
            result.matched_perfume_ids
            or ([result.perfume_id] if result.perfume_id else []),
        )
        _log_outbound(
            sender,
            reply_text,
            success,
            matched=result.perfume_id or "(none)",
            layer=result.layer or "(none)",
            confidence=result.confidence,
            ambiguous=result.ambiguous,
        )
    else:
        success = False
        logger.info(
            "<<< SILENT  to=%s  (not a greeting/catalog request and no perfume match — no reply sent, no credit spent)",
            sender,
        )

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

    return Response(status_code=200, content="OK")
