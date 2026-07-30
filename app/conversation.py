"""
Per-sender conversation memory — the bot's short-term recall.

Without this the bot answered every message as if it were the first one it
had ever seen, which shows up immediately in real chats:

    Customer: 9pm rebel price
    Bot:      (full price grid)
    Customer: and 5ml?
    Bot:      (silence — no perfume named in that message)

The follow-up is a completely ordinary thing to say and the bot had no way
to understand it. Recent turns are now kept per sender and used three ways
(see app.matcher):

  * A follow-up naming no perfume ("and 5ml?", "iska price kya hai", "how
    much for that one") resolves to whatever was just being discussed.
  * Groq is shown the recent turns, so it can read intent in context and
    resolve references like "the second one" or "the EDP one" against the
    cards it actually showed.
  * A message that names a perfume outright still wins on its own — context
    never overrides what the customer just said.

STORAGE
-------
In-memory per process, TTL-bounded, with Supabase as a cold-start backstop.
Turns are small and short-lived, and this bot serves a single WhatsApp
number, so an in-memory ring buffer is the right primary store — no DB
round trip on the hot path.

The backstop matters because the service restarts (redeploys, and Render's
free tier spinning down between conversations) would otherwise drop context
mid-chat. Nothing new is persisted for it: app.analytics already logs every
inbound message with its sender and resolved perfume_id, so a cold buffer
is rebuilt by reading those rows back. If Supabase is not configured, the
in-memory buffer simply stands alone — same graceful-degradation contract
as the rest of the optional features.
"""

import asyncio
import logging
import time
from collections import OrderedDict

from app.config import settings
from app.db import get_client

logger = logging.getLogger(__name__)

# sender -> (last_touched_epoch, [turn, ...] oldest first)
_turns: "OrderedDict[str, tuple[float, list[dict]]]" = OrderedDict()

# Cap on senders held in memory at once. This bot serves one WhatsApp
# number; even a busy day is far below this, so the bound exists to make
# unbounded growth impossible rather than because it is expected to bind.
_MAX_SENDERS = 2000

# Senders whose history has already been rebuilt from Supabase this process
# lifetime — so a genuinely new customer doesn't hit the database on every
# single message just because they have no history to find.
_warmed: set[str] = set()


def _now() -> float:
    return time.time()


def _evict_expired() -> None:
    cutoff = _now() - settings.CONVERSATION_TTL_SECONDS
    stale = [s for s, (touched, _) in _turns.items() if touched < cutoff]
    for sender in stale:
        _turns.pop(sender, None)
        _warmed.discard(sender)


def _append(sender: str, turn: dict) -> None:
    _evict_expired()
    touched, turns = _turns.pop(sender, (_now(), []))
    turns.append(turn)
    del turns[: max(0, len(turns) - settings.CONVERSATION_TURNS)]
    _turns[sender] = (_now(), turns)
    while len(_turns) > _MAX_SENDERS:
        evicted, _ = _turns.popitem(last=False)
        _warmed.discard(evicted)


def record_customer_message(sender: str, text: str) -> None:
    """Remember what the customer just said."""
    if sender and text:
        _append(sender, {"role": "customer", "text": text})


def record_bot_reply(sender: str, text: str, perfume_ids: list[str] | None = None) -> None:
    """
    Remember what the bot just replied, and — crucially — which perfumes it
    showed cards for, in the order they appeared. That ordering is what lets
    "the second one" mean anything.
    """
    if not sender:
        return

    ids = [pid for pid in (perfume_ids or []) if pid]
    names: list[str] = []
    if ids:
        from app.catalog import PERFUMES

        names = [PERFUMES[pid]["display_name"] for pid in ids if pid in PERFUMES]

    _append(
        sender,
        {"role": "bot", "text": text or "", "perfume_ids": ids, "perfume_names": names},
    )


def _fetch_recent_events(client, sender: str, limit: int):
    resp = (
        client.table("message_events")
        .select("message_text,perfume_id,created_at")
        .eq("sender", sender)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return resp.data or []


async def _warm_from_supabase(sender: str) -> None:
    """
    Rebuild a cold in-memory history from the analytics log after a restart.

    Best-effort in every direction: no Supabase, a failed query, or no rows
    all mean the conversation simply starts fresh — never an error on the
    customer-facing path. Reconstructed turns carry the customer's message
    and the perfume the bot resolved it to, which is everything the
    follow-up resolver and the Groq prompt actually read.
    """
    if sender in _warmed or sender in _turns:
        return
    _warmed.add(sender)

    client = get_client()
    if client is None:
        return

    try:
        rows = await asyncio.to_thread(
            _fetch_recent_events, client, sender, settings.CONVERSATION_TURNS
        )
    except Exception:
        logger.exception("Failed to warm conversation history for %s", sender)
        return

    for row in reversed(rows):  # oldest first
        text = row.get("message_text") or ""
        if text:
            _append(sender, {"role": "customer", "text": text})
        pid = row.get("perfume_id")
        if pid:
            record_bot_reply(sender, "", [pid])


async def recent_turns(sender: str) -> list[dict]:
    """
    Recent conversation for this sender, oldest first — what app.matcher
    passes to the follow-up resolver and to Groq.

    Returns [] for a sender with no history, which every caller treats as
    "answer this message on its own".
    """
    if not sender:
        return []

    _evict_expired()
    if sender not in _turns:
        await _warm_from_supabase(sender)

    entry = _turns.get(sender)
    return list(entry[1]) if entry else []


def last_discussed_perfumes(history: list[dict] | None) -> list[str]:
    """
    The perfumes the most recent price card showed, in the order shown.

    Only the LATEST card counts, not everything ever mentioned: "and 5ml?"
    means the thing just discussed, and reaching further back would answer a
    question the customer didn't ask.
    """
    for turn in reversed(history or []):
        if turn.get("role") == "bot" and turn.get("perfume_ids"):
            return list(turn["perfume_ids"])
    return []


def clear(sender: str | None = None) -> None:
    """Forget one sender's history, or everything. Used by the human-handoff
    flow and by tests."""
    if sender is None:
        _turns.clear()
        _warmed.clear()
    else:
        _turns.pop(sender, None)
        _warmed.discard(sender)
