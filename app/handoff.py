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
start_pause never raise), while the admin-facing settings get/set raise
SupabaseUnavailable so a failed save is never silently swallowed on the
owner's dashboard.

"Best-effort" used to mean the pause simply did not happen when Supabase
could not be reached, and said nothing about it. That was reported from
production as the bot talking over the owner mid-conversation. The pause
now takes effect in memory first and Supabase second, so it holds for the
life of the process regardless, and a failed write is logged loudly
instead of swallowed.
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

# Active pauses, in memory. Supabase remains the durable record — this
# survives neither a restart nor a second worker — but the pause has to
# work even when Supabase does not, and previously it did not: every write
# path here swallows its exception, so an unconfigured URL, a missing
# migration or an RLS rule blocking the key all produced a pause that was
# never stored and a bot that carried on talking over the owner, with
# nothing in the logs to say so. Reported from production exactly that way.
_paused_until: dict[str, float] = {}

# Digits only. The two webhook events do not agree on formatting: an
# inbound message.received carries the customer in `from`, an outbound
# message.sent carries them in `to`, and Chat Mitra is free to write one
# with a leading "+", a country-code prefix or a "@c.us" suffix and the
# other without. Every key in this module — own-send records, pause rows,
# pause lookups — is derived through normalize_sender, so a difference in
# punctuation can never again mean the pause is written under one name and
# read under another.


def normalize_sender(value: str) -> str:
    """A phone number reduced to the digits that identify it.

    Trailing suffixes ("919876543210@c.us") and formatting ("+91 98765
    43210") are stripped, and a leading zero or a bare 10-digit Indian
    number is left as-is rather than guessed at — the goal is only that the
    SAME number always produces the same key, not that it round-trips to
    anything canonical.
    """
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits.lstrip("0") or digits


# --- Own-send echo detection (in-memory, no Supabase) -----------------------

def _evict_expired_own_sends() -> None:
    cutoff = time.time() - _OWN_SEND_TTL_SECONDS
    while _own_sends:
        key, ts = next(iter(_own_sends.items()))
        if ts < cutoff:
            _own_sends.pop(key)
        else:
            break


def _echo_key(recipient: str, message_text: str) -> tuple[str, str]:
    """The identity of one outbound message, as this module compares them:
    the recipient by digits alone, and the text with surrounding whitespace
    removed. Trailing whitespace is the kind of thing a messaging platform
    normalizes on the way through, and an echo that failed to match would
    be read as the owner taking over — pausing the bot on its own reply."""
    return (normalize_sender(recipient), (message_text or "").strip())


def record_own_send(recipient: str, message_text: str) -> None:
    """Call this right after a successful send_reply() so a later
    message.sent echo of THIS reply is never mistaken for the owner
    taking over the conversation."""
    _evict_expired_own_sends()
    _own_sends[_echo_key(recipient, message_text)] = time.time()
    while len(_own_sends) > _OWN_SEND_MAX_SIZE:
        _own_sends.popitem(last=False)


def was_sent_by_bot(recipient: str, message_text: str) -> bool:
    """True if this (recipient, text) matches a recent record_own_send call
    — i.e. this message.sent webhook event is just Chat Mitra echoing back
    the bot's own reply, not the owner sending something new."""
    _evict_expired_own_sends()
    return _echo_key(recipient, message_text) in _own_sends


def clear_own_sends() -> None:
    """Test-only reset."""
    _own_sends.clear()
    _paused_until.clear()


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
    now — the owner messaged them directly and the configured pause window
    hasn't elapsed yet (see app.main's pause-check branch).

    Memory first, Supabase second, and the order matters: a pause this
    process recorded is authoritative even if it never reached the
    database. Supabase is still consulted, because the pause has to hold
    across a restart and across workers.

    If neither can answer, this returns False — the bot behaves normally
    rather than risking silence forever with no way to recover.
    """
    key = normalize_sender(sender)
    until = _paused_until.get(key)
    if until is not None:
        if time.time() < until:
            return True
        _paused_until.pop(key, None)

    client = get_client()
    if client is None:
        return False

    try:
        row = await asyncio.to_thread(_fetch_pause_row, client, key)
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


async def start_pause(sender: str) -> bool:
    """
    Called when the owner is detected sending a message directly to this
    sender (see app.main's message.sent handling) — pauses the bot for this
    sender for the currently configured duration, starting now.

    The pause takes effect IN MEMORY before Supabase is touched, so it
    holds whether or not the write lands. It used to be Supabase-only with
    every failure swallowed, which meant an unconfigured URL, a missing
    migration or an RLS rule blocking the key produced a bot that carried
    on talking over the owner and said nothing about why. Returns whether
    the pause was also persisted, so the caller can say so in the log.
    """
    key = normalize_sender(sender)
    hours = await get_pause_duration_hours()
    _paused_until[key] = time.time() + hours * 3600

    client = get_client()
    if client is None:
        logger.warning(
            "Human-handoff pause for %s is in memory only — Supabase is not "
            "configured, so it will not survive a restart",
            sender,
        )
        return False

    paused_until = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    try:
        await asyncio.to_thread(_upsert_pause_row, client, key, paused_until)
    except Exception:
        logger.exception(
            "Failed to record human-handoff pause for %s in Supabase — the pause is "
            "active in memory but will not survive a restart. Check that migration "
            "0003_human_handoff.sql has been applied and the service key can write "
            "to human_takeovers.",
            sender,
        )
        return False
    return True


async def self_check() -> dict:
    """
    Can the handoff pause actually be stored? Answered at startup rather
    than the first time the owner takes over a conversation.

    Every failure on this path is deliberately swallowed so a Supabase
    hiccup can never stop a customer getting a price — which is right, and
    which also meant the pause could be a complete no-op for months with
    nothing to show for it but a bot that talked over the owner. The
    difference between "working" and "silently doing nothing" is one query,
    and this is it.

    Returns {"durable": bool, "detail": str} — never raises.
    """
    client = get_client()
    if client is None:
        return {
            "durable": False,
            "detail": (
                "Supabase is not configured (SUPABASE_URL / "
                "SUPABASE_SERVICE_ROLE_KEY). The handoff pause will work "
                "within a single running process but will not survive a "
                "restart — and on a free plan the service sleeps when idle, "
                "so a customer replying an hour later gets answered by the bot."
            ),
        }

    try:
        await asyncio.to_thread(_fetch_pause_row, client, "__self_check__")
    except Exception as exc:
        return {
            "durable": False,
            "detail": (
                f"Supabase is configured but human_takeovers could not be read "
                f"({type(exc).__name__}: {exc}). Apply "
                f"supabase/migrations/0003_human_handoff.sql. Until then the "
                f"handoff pause holds in memory only."
            ),
        }

    return {"durable": True, "detail": "human_takeovers is readable"}


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
