"""
The bot's words, editable by the owner — and the rules that stop an edit
from silently breaking every reply.

WHY THIS NEEDS VALIDATION AT ALL
--------------------------------
Chat Mitra does not fail loudly on a message it dislikes. A reply with the
wrong characters in it comes back as a 2xx and never reaches the customer,
which from the dashboard is indistinguishable from working. This codebase
learned that the hard way, and app.formatter still carries the scars:

  * Asterisks were isolated as the actual cause of messages not arriving.
    Not "sometimes"; the price card has been asterisk-free ever since.
  * Of the emoji tried, only a few are individually confirmed to send.
    Others were suspected and dropped rather than risked.

So an owner editing the welcome message can, with one asterisk, stop every
new customer from being greeted, and find out weeks later. Validation here
is not tidiness. It is the difference between an edit and an outage.

WHAT IS NOT DONE HERE
---------------------
No test message is sent. Validation is static: the text is checked against
the constraints above and shown as a live preview. That is a deliberate
choice — the alternative worth having is sending a real WhatsApp to the
owner's own number and requiring them to confirm it arrived, which is a
bigger feature than this and is not what was asked for.
"""

import logging

from app.db import get_client

logger = logging.getLogger(__name__)

_SETTINGS_KEY = "bot_messages"

# Emoji individually confirmed to send through Chat Mitra. Anything outside
# this set is warned about rather than blocked — the list is what has been
# proven, not what is possible, and an owner who knows a new one works
# should not be stopped by a list that has not caught up.
CONFIRMED_EMOJI = {"📋", "🙂", "👋", "🔗", "📦", "🚚", "🎁", "✈️", "🔥", "📍", "⭐", "🤝"}

# Characters that have been observed to stop a message being delivered.
# Blocking, not warning: a message that does not arrive is not a style
# problem.
FORBIDDEN_CHARS = {
    "*": "asterisks stop the message being delivered — this was isolated as the actual cause when replies silently stopped arriving",
}

# Chat Mitra sends a WhatsApp text message, which caps at 4096 characters.
# The bot's own cutoff is lower so a price card can be appended to a
# greeting without either being truncated mid-price.
MAX_MESSAGE_CHARS = 3500

# What each editable message is for, and what it must contain. A template
# missing its placeholder is not a style choice — a price card with no
# {prices} in it is a price card that quotes nothing.
TEMPLATES: dict[str, dict] = {
    "welcome": {
        "label": "First-time welcome",
        "help": "Sent once, on a customer's first bare hello. Never sent again.",
        "required": [],
    },
    "fallback": {
        "label": "Catalog reply",
        "help": "Sent when someone says hi again, or asks for the catalog without naming a perfume.",
        "required": [],
    },
    "non_text": {
        "label": "Non-text message",
        "help": "Sent when a customer sends an image, sticker or voice note.",
        "required": [],
    },
    "order_confirmation": {
        "label": "Order confirmed",
        "help": "Sent when a customer sends back the filled-in order form.",
        "required": [],
    },
    "closing_line": {
        "label": "Price card closing line",
        "help": "The last line under every price card.",
        "required": [],
    },
    "ambiguous": {
        "label": "Could not narrow it down",
        "help": "Rarely seen — sent when a mention matches nothing specific enough to price.",
        "required": [],
    },
}


def defaults() -> dict[str, str]:
    """The shipped wording, which is also what a blank field falls back to."""
    from app.formatter import (
        AMBIGUOUS_MESSAGE,
        FALLBACK_MESSAGE,
        NON_TEXT_MESSAGE,
        ORDER_CONFIRMATION_MESSAGE,
        WELCOME_MESSAGE,
        WILL_CONTACT_LINE,
    )

    return {
        "welcome": WELCOME_MESSAGE,
        "fallback": FALLBACK_MESSAGE,
        "non_text": NON_TEXT_MESSAGE,
        "order_confirmation": ORDER_CONFIRMATION_MESSAGE,
        "closing_line": WILL_CONTACT_LINE,
        "ambiguous": AMBIGUOUS_MESSAGE,
    }


# Read once and kept in memory: every outbound reply reads these, and a
# Supabase round-trip per message would put the database on the customer's
# critical path for no benefit. Refreshed on save and at startup.
_overrides: dict[str, str] = {}


def validate(key: str, text: str) -> dict:
    """
    Whether this text is safe to send, and what to change if it is not.

    Returns {"ok", "errors", "warnings", "chars"}. Errors block saving —
    each one is something observed to stop delivery. Warnings do not; they
    are things worth knowing that have not been proven fatal.
    """
    text = text or ""
    errors: list[str] = []
    warnings: list[str] = []

    if not text.strip():
        errors.append("This message is empty. Leave it blank only if you mean to fall back to the built-in wording.")

    for char, why in FORBIDDEN_CHARS.items():
        if char in text:
            count = text.count(char)
            errors.append(
                f"Remove the {count} asterisk{'s' if count > 1 else ''} — {why}."
                if char == "*"
                else f"Remove {char!r} — {why}."
            )

    if len(text) > MAX_MESSAGE_CHARS:
        errors.append(
            f"This is {len(text):,} characters. WhatsApp cuts off around {MAX_MESSAGE_CHARS:,}, "
            f"so trim about {len(text) - MAX_MESSAGE_CHARS:,}."
        )

    for placeholder in TEMPLATES.get(key, {}).get("required", []):
        if placeholder not in text:
            errors.append(f"This message has to contain {placeholder} — without it the reply says nothing useful.")

    unknown = sorted(
        {ch for ch in text if ord(ch) > 0x2100 and ch not in CONFIRMED_EMOJI and ch.strip()}
    )
    if unknown:
        warnings.append(
            "Not-yet-confirmed emoji: "
            + " ".join(unknown[:8])
            + ". Only a few are known to send reliably — if replies stop arriving after this change, take these out first."
        )

    if "_" in text or "~" in text:
        warnings.append(
            "Underscores and tildes are WhatsApp formatting marks. They will show as italics or "
            "strikethrough rather than as characters."
        )

    return {"ok": not errors, "errors": errors, "warnings": warnings, "chars": len(text)}


def validate_all(messages: dict[str, str]) -> dict:
    """Validate a whole set, keyed the same way it came in."""
    results = {key: validate(key, text) for key, text in (messages or {}).items()}
    return {"ok": all(r["ok"] for r in results.values()), "results": results}


def current() -> dict[str, str]:
    """What the bot is saying right now — defaults with any saved edits on top."""
    merged = defaults()
    merged.update({k: v for k, v in _overrides.items() if v})
    return merged


def get(key: str) -> str:
    return current().get(key, "")


def load_from_db() -> None:
    """Pull saved wording into memory. Best-effort — an unreachable database
    means the bot speaks its built-in words, which is a working bot."""
    global _overrides
    client = get_client()
    if client is None:
        return
    try:
        resp = (
            client.table("bot_settings").select("value").eq("key", _SETTINGS_KEY).limit(1).execute()
        )
    except Exception:
        logger.exception("Could not read saved bot messages — using the built-in wording")
        return
    rows = resp.data or []
    if rows and isinstance(rows[0].get("value"), dict):
        _overrides = {k: v for k, v in rows[0]["value"].items() if isinstance(v, str)}


def save(messages: dict[str, str]) -> dict:
    """
    Save edited wording, refusing anything that would not be delivered.

    Validation runs before the write, not after, so a message that cannot
    send never becomes the message the bot is trying to send.
    """
    from app.db import require_client

    incoming = {k: v for k, v in (messages or {}).items() if k in TEMPLATES}
    if not incoming:
        raise ValueError("Nothing to save.")

    verdict = validate_all(incoming)
    if not verdict["ok"]:
        broken = [
            f"{TEMPLATES[k]['label']}: {r['errors'][0]}"
            for k, r in verdict["results"].items()
            if not r["ok"]
        ]
        raise ValueError(" ".join(broken))

    client = require_client()
    merged = dict(_overrides)
    merged.update(incoming)
    client.table("bot_settings").upsert({"key": _SETTINGS_KEY, "value": merged}).execute()

    _overrides.update(incoming)
    return current()


def reset(key: str) -> dict:
    """Put one message back to the shipped wording."""
    from app.db import require_client

    if key not in TEMPLATES:
        raise ValueError(f"There is no message called {key!r}.")
    _overrides.pop(key, None)
    client = require_client()
    client.table("bot_settings").upsert({"key": _SETTINGS_KEY, "value": dict(_overrides)}).execute()
    return current()
