"""
Groq LLM client — primary perfume classification + reply phrasing.

Groq now runs FIRST for every message (see app.matcher.match_perfume), not
as a last resort — one combined call both identifies which candidate
perfume(s) (if any) the customer means AND writes short, natural opening/
closing phrasing for the reply. Two separate calls (classify, then phrase)
would double latency and failure surface for no benefit, so this is
deliberately one call with a structured JSON response.

perfume_ids is a LIST, not a single id: confirmed in production that a
customer message can clearly name multiple distinct perfumes at once (e.g.
"sauvage and eros price"), and the old single-id shape had no way to return
more than one, silently dropping every match but the first. Most messages
still resolve to exactly one id — this is the same shape either way.

JSON mode (response_format={"type": "json_object"}), not a free-text label
format: confirmed directly against llama-3.1-8b-instant that a plain-text
"PERFUME_ID: ..." convention isn't followed reliably — a real observed
response was "dior_sauvage_edt: Dior Sauvage EDT" instead of the requested
"PERFUME_ID: dior_sauvage_edt", silently discarded by the old regex parser
and falling through to the free matcher every time. JSON mode is a real API
guarantee of well-formed output, not a hope that a small/fast model follows
a text convention.

Prices are NEVER part of what the LLM generates — see app.formatter, which
assembles the actual price grid deterministically from catalog.py and only
wraps it with this module's opening/closing lines. The system prompt below
also explicitly forbids the model from including any number/price/size in
its own text, as a second layer of defense.
"""

import json
import logging
from dataclasses import dataclass, field

from openai import AsyncOpenAI

from app.config import settings

logger = logging.getLogger(__name__)

_MODEL = "llama-3.1-8b-instant"


@dataclass
class GroqClassification:
    """Result of classify_and_phrase. perfume_ids is empty whenever Groq
    wasn't confident about any candidate — opening/closing are only ever
    populated alongside a non-empty perfume_ids, since they're useless
    without one (the caller falls through to the free matcher instead)."""

    perfume_ids: list[str] = field(default_factory=list)
    opening: str | None = None
    closing: str | None = None


def _build_system_prompt(candidates: dict[str, dict]) -> str:
    perfume_list = "\n".join(
        f"- {pid}: {data['display_name']}" for pid, data in candidates.items()
    )

    return f"""You are the WhatsApp assistant for Sovereign Scents, a perfume decant business in India. A customer messaged you. Your job has two parts:

1. IDENTIFY which perfume(s) (if any) from the candidate list they mean.
2. WRITE a short, warm, natural opening and closing line for the reply — NOT the price details, those come from our verified price list separately.

CANDIDATES (id: name):
{perfume_list}

Respond ONLY with a JSON object in exactly this shape, nothing else:
{{"perfume_ids": ["<id from the list above>", ...], "opening": "<one short, friendly line>", "closing": "<one short, friendly line>"}}

RULES:
- perfume_ids lists every id from the list above that the customer is clearly asking about. Most messages name exactly ONE perfume, so this is usually a single-item list.
- If the customer's message clearly names 2+ different perfumes (e.g. "sauvage and eros price", "bleu de chanel 5ml and versace eros 10ml"), include all of them — never drop one to only answer part of the question.
- If the message names something too vague to tell which ONE specific candidate they mean (e.g. just a product line/series name with 2+ real variants in the candidate list), include all of the genuinely plausible candidates rather than guessing a single one.
- Use an empty list [] if nothing in the candidate list clearly matches. Never include an id that isn't in the candidate list above.
- If perfume_ids is non-empty, you are CONFIDENT — opening must sound definitive and matter-of-fact ("Sauvage is one of our favorites!"), never hedge or ask the customer for more details ("which one did you mean?", "can you tell me more?") — that contradicts having just picked it.
- opening and closing must NEVER contain a price, number, size, or currency symbol — that data comes from us, not you. Including a number is a mistake.
- Under 15 words each. Sound like a real person texting, not a script. Hinglish is fine if the customer wrote that way. At most one emoji total.
- Never use the asterisk character (*) anywhere.
- If perfume_ids is empty, still write a brief natural opening acknowledging you're unsure, and set closing to an empty string."""


def _parse_response(text: str) -> tuple[list[str], str | None, str | None]:
    """Parse the JSON response. Any failure (invalid JSON, wrong shape,
    non-string fields) returns ([], None, None) rather than raising —
    treated by the caller exactly like "no confident match"."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return [], None, None

    if not isinstance(data, dict):
        return [], None, None

    raw_pids = data.get("perfume_ids")
    opening = data.get("opening")
    closing = data.get("closing")

    pids: list[str] = []
    if isinstance(raw_pids, list):
        for pid in raw_pids:
            if isinstance(pid, str) and pid.strip():
                normalized = pid.strip().lower()
                if normalized not in pids:
                    pids.append(normalized)

    opening = opening.strip() if isinstance(opening, str) and opening.strip() else None
    closing = closing.strip() if isinstance(closing, str) and closing.strip() else None

    return pids, opening, closing


async def classify_and_phrase(message: str, candidates: dict[str, dict]) -> GroqClassification:
    """
    Ask Groq which candidate perfume(s) the message refers to, and for short
    opening/closing phrasing to wrap around the (separately, deterministically
    assembled) price card(s).

    Never raises — any failure (missing key, API error, malformed/
    unparseable response, no valid ids among the offered candidates) returns
    an empty GroqClassification, which the caller (app.matcher) treats as
    "fall through to the free exact/fuzzy matcher" rather than a hard failure.
    """
    if not settings.GROQ_API_KEY or not candidates:
        return GroqClassification()

    try:
        client = AsyncOpenAI(
            api_key=settings.GROQ_API_KEY,
            base_url="https://api.groq.com/openai/v1",
        )

        response = await client.chat.completions.create(
            model=_MODEL,
            temperature=0.4,
            max_tokens=200,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _build_system_prompt(candidates)},
                {"role": "user", "content": message},
            ],
        )

        raw = response.choices[0].message.content or ""
        logger.info("Groq classify_and_phrase response: %r", raw)

        pids, opening, closing = _parse_response(raw)

        valid_pids = [pid for pid in pids if pid in candidates]
        if not valid_pids:
            return GroqClassification()

        # Safety net: a confident match should never hedge ("which one did
        # you mean?", "can you give me a hint?") — the prompt rule above
        # asks for this, but confirmed directly that llama-3.1-8b-instant
        # doesn't always comply (a real observed case: matched a specific
        # perfume_id but still wrote "We have several options, can you give
        # me a hint?"). A question mark right next to a confident price
        # card reads as contradictory, so discard rather than ship it — the
        # deterministic plain header in app.formatter takes over instead.
        if opening and "?" in opening:
            opening = None

        return GroqClassification(perfume_ids=valid_pids, opening=opening, closing=closing)

    except Exception:
        logger.exception("Groq classify_and_phrase call failed")
        return GroqClassification()
