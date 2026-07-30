"""
Human handoff — once the owner personally replies to a customer through
Chat Mitra's own dashboard/app (not through this bot), the bot backs off
from that conversation for a configurable window (default 24h) so it
doesn't talk over a human agent who's already settling the order (address,
payment, etc.) — see app.main's message.sent webhook handling and its
pause-check branch in webhook_handler.

Chat Mitra's documentation doesn't expose any field that distinguishes a
message the owner sent through their dashboard from one this bot sent
through the API — both surface as the same "message.sent" webhook event
(direction=outbound). The only way to tell them apart is to recognize the
bot's OWN sends: every successful send_reply() call is recorded here for a
short window (see record_own_send), and any "message.sent" event whose
(recipient, text) doesn't match one of the bot's own recent sends must
have come from the owner directly — see was_sent_by_bot.

Everything here follows the same graceful-degradation pattern as
app.analytics: best-effort on the customer-facing path (is_paused,
start_pause never raise — Supabase being unset just means the pause
feature quietly can't do anything, never that the bot breaks), but the
admin-facing settings get/set raise SupabaseUnavailable so a failed save
is never silently swallowed on the owner's dashboard.
"""

import asyncio
import logging
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from app.db import get_client, require_client

logger = logging.getLogger(__name__)

DEFAULT_PAUSE_HOURS = 24.0
_PAUSE_HOURS_SETTING_KEY = "human_handoff_pause_hours"

# How long we remember our own outbound sends, purely to recognize Chat
# Mitra echoing them back as a "message.sent" event — generous vs. any
# realistic webhook delivery delay, small enough to never meaningfully
# grow memory usage (this bot serves one WhatsApp number).
_OWN_SEND_TTL_SECONDS = 300
_OWN_SEND_MAX_SIZE = 2000

_own_sends: "OrderedDict[tuple[str, str], float]" = OrderedDict()


# --- Own-send echo detection (in-memory, no Supabase) -----------------------

def _evict_expired_own_sends() -> None:
    cutoff = time.time() - _OWN_SEND_TTL_SECONDS
    while _own_sends:
        key, ts = next(iter(_own_sends.items()))
        if ts < cutoff:
            _own_sends.pop(key)
        else:
            break


def record_own_send(recipient: str, message_text: str) -> None:
    """Call this right after a successful send_reply() so a later
    message.sent echo of THIS reply is never mistaken for the owner
    taking over the conversation."""
    _evict_expired_own_sends()
    _own_sends[(recipient, message_text)] = time.time()
    while len(_own_sends) > _OWN_SEND_MAX_SIZE:
        _own_sends.popitem(last=False)


def was_sent_by_bot(recipient: str, message_text: str) -> bool:
    """True if this exact (recipient, text) matches a recent record_own_send
    call — i.e. this message.sent webhook event is just Chat Mitra echoing
    back the bot's own reply, not the owner sending something new."""
    _evict_expired_own_sends()
    return (recipient, message_text) in _own_sends


def clear_own_sends() -> None:
    """Test-only reset."""
    _own_sends.clear()


# --- Pause state (Supabase-backed, best-effort on the customer-facing path) -

def _fetch_pause_row(client, sender: str) -> dict | None:
    resp = (
        client.table("human_takeovers")
        .select("paused_until")
        .eq("sender", sender)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return rows[0] if rows else None


def _upsert_pause_row(client, sender: str, paused_until_iso: str) -> None:
    client.table("human_takeovers").upsert(
        {"sender": sender, "paused_until": paused_until_iso}
    ).execute()


async def is_paused(sender: str) -> bool:
    """
    True if the bot should stay completely silent for this sender right
    now — the owner messaged them directly and the configured pause
    window hasn't elapsed yet (see app.main's pause-check branch).

    Best-effort: if Supabase isn't configured, or the query fails, there's
    no way to know a pause is active, so this returns False (bot behaves
    normally) rather than risk staying silent forever with no way to
    recover.
    """
    client = get_client()
    if client is None:
        return False

    try:
        row = await asyncio.to_thread(_fetch_pause_row, client, sender)
    except Exception:
        logger.exception("Failed to check human-handoff pause state in Supabase")
        return False

    if not row:
        return False

    try:
        paused_until = datetime.fromisoformat(row["paused_until"])
    except (TypeError, ValueError):
        return False

    return datetime.now(timezone.utc) < paused_until


async def start_pause(sender: str) -> None:
    """
    Called when the owner is detected sending a message directly to this
    sender (see app.main's message.sent handling) — pauses the bot for
    this sender for the currently configured duration, starting now.
    Best-effort: a Supabase hiccup here means the pause silently doesn't
    take effect, same tradeoff as everything else in this module.
    """
    client = get_client()
    if client is None:
        return

    hours = await get_pause_duration_hours()
    paused_until = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()

    try:
        await asyncio.to_thread(_upsert_pause_row, client, sender, paused_until)
    except Exception:
        logger.exception("Failed to record human-handoff pause in Supabase")


# --- Configurable pause duration (owner dashboard Settings page) ------------

def _fetch_setting_row(client, key: str) -> dict | None:
    resp = client.table("bot_settings").select("value").eq("key", key).limit(1).execute()
    rows = resp.data or []
    return rows[0] if rows else None


def _upsert_setting_row(client, key: str, value: float) -> None:
    client.table("bot_settings").upsert({"key": key, "value": value}).execute()


async def get_pause_duration_hours() -> float:
    """Best-effort read used on the customer-facing path (start_pause) —
    defaults to DEFAULT_PAUSE_HOURS whenever Supabase is unset, the setting
    was never saved, or the read fails, so a dashboard hiccup never breaks
    the pause feature outright."""
    client = get_client()
    if client is None:
        return DEFAULT_PAUSE_HOURS

    try:
        row = await asyncio.to_thread(_fetch_setting_row, client, _PAUSE_HOURS_SETTING_KEY)
    except Exception:
        logger.exception("Failed to read %s setting", _PAUSE_HOURS_SETTING_KEY)
        return DEFAULT_PAUSE_HOURS

    if not row:
        return DEFAULT_PAUSE_HOURS

    try:
        return float(row["value"])
    except (TypeError, ValueError):
        return DEFAULT_PAUSE_HOURS


async def admin_get_pause_duration_hours() -> float:
    """Dashboard-facing read (Settings page) — raises SupabaseUnavailable
    rather than silently defaulting, so the owner sees a clear error
    instead of a value that was never actually being read from anywhere."""
    client = require_client()
    row = await asyncio.to_thread(_fetch_setting_row, client, _PAUSE_HOURS_SETTING_KEY)
    if not row:
        return DEFAULT_PAUSE_HOURS
    try:
        return float(row["value"])
    except (TypeError, ValueError):
        return DEFAULT_PAUSE_HOURS


async def admin_set_pause_duration_hours(hours: float) -> float:
    """Dashboard-facing write (Settings page) — raises SupabaseUnavailable
    if there's nowhere to actually save it, so a "successful" save always
    means it was really persisted."""
    client = require_client()
    await asyncio.to_thread(_upsert_setting_row, client, _PAUSE_HOURS_SETTING_KEY, hours)
    return hours
