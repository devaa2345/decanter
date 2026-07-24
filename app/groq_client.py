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

explicit_ask distinguishes "customer is asking about this perfume right
now" from "customer merely said its name" — confirmed in production that a
perfume name surfacing mid-conversation (recalling a past purchase, saying
what a friend or the shop owner wears, chatting about something else that
happens to name a perfume) was still auto-firing a price card, which reads
as the bot barging into a human conversation. An LLM that sees the whole
message is far better positioned to judge this than a keyword list, so it's
asked for directly as part of the same structured response rather than
bolted on afterward. classify_and_phrase collapses the result to an empty
GroqClassification whenever explicit_ask isn't true.

classify_and_phrase returns None vs. an empty GroqClassification() for two
DELIBERATELY different situations — app.matcher treats them differently:
  - None means Groq itself was unreachable (no API key, no candidates worth
    asking about, network/API error) — the caller falls through to the
    free deterministic exact/fuzzy matcher so the bot doesn't go silent on
    an outage.
  - An empty GroqClassification() means Groq successfully ran and made a
    considered judgment that this isn't a confident, explicit ask — the
    caller TRUSTS that and does NOT fall through to the deterministic
    matcher. This distinction was added after a real production bug: the
    old code treated "Groq said no" and "Groq is down" identically, always
    falling through to keyword matching either way — and that keyword
    matcher has no concept of context, so it readily over-matched on
    incidental word overlap (confirmed live: "I want to confirm kaaf only"
    additionally matched an unrelated "Not Only Intense" product purely
    because "only" happens to be a standalone keyword for it). Once Groq
    has actually looked at the message, its judgment is more precise than
    a keyword re-check could ever be, so it should be final.

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
    """Result of a successful classify_and_phrase call (see that function's
    None return for the "Groq was unreachable" case instead). perfume_ids is
    empty whenever Groq ran fine but wasn't confident about any candidate,
    or wasn't confident this was an explicit ask (see explicit_ask) — that
    is trusted as a real "no" by the caller, not a failure. opening/closing
    are only ever populated alongside a non-empty perfume_ids, since
    they're useless without one."""

    perfume_ids: list[str] = field(default_factory=list)
    explicit_ask: bool = False
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
{{"perfume_ids": ["<id from the list above>", ...], "explicit_ask": true/false, "opening": "<one short, friendly line>", "closing": "<one short, friendly line>"}}

RULES:
- perfume_ids lists every id from the list above that the customer is clearly asking about. Most messages name exactly ONE perfume, so this is usually a single-item list.
- If the customer's message clearly names 2+ different perfumes (e.g. "sauvage and eros price", "bleu de chanel 5ml and versace eros 10ml"), include all of them — never drop one to only answer part of the question.
- If the message names something too vague to tell which ONE specific candidate they mean (e.g. just a product line/series name with 2+ real variants in the candidate list), include all of the genuinely plausible candidates rather than guessing a single one.
- Use an empty list [] if nothing in the candidate list clearly matches. Never include an id that isn't in the candidate list above.
- explicit_ask is true only if the customer is actually asking about price, availability, or buying a perfume right now. False if the name is only mentioned in passing — a past purchase, what a friend/the shop owner wears, chit-chat that happens to name it — without actually asking. This is a real decision, not a formality — get it right, since nothing else will catch a mistake here.
  * A bare product name alone, with or without a size, IS an explicit ask — that is the single most common way a customer asks ("sauvage", "sauvage 10ml", "kaaf"). Do not mark this false just because there's no question mark or the word "price".
  * "I want to confirm X", "I'll take X", "book/order/send me X" IS an explicit ask — confirming or ordering is asking, not mentioning.
  * "X is nice but I don't want it", "my friend uses X", "the owner said X is good" is NOT an explicit ask — a name said in passing or a decline, not a request.
  * If perfume_ids is empty, this must be false.
- If perfume_ids is non-empty AND explicit_ask is true, you are CONFIDENT — opening must sound definitive and matter-of-fact ("Sauvage is one of our favorites!"), never hedge or ask the customer for more details ("which one did you mean?", "can you tell me more?") — that contradicts having just picked it.
- opening and closing must NEVER contain a price, number, size, or currency symbol — that data comes from us, not you. Including a number is a mistake.
- Under 15 words each. Sound like a real person texting, not a script. Hinglish is fine if the customer wrote that way. At most one emoji total.
- Never use the asterisk character (*) anywhere.
- If perfume_ids is empty or explicit_ask is false, still write a brief natural opening, and set closing to an empty string."""


def _parse_response(text: str) -> tuple[list[str], bool, str | None, str | None]:
    """Parse the JSON response. Any failure (invalid JSON, wrong shape,
    non-string fields) returns ([], False, None, None) rather than raising —
    treated by the caller exactly like "no confident match". A missing or
    non-boolean explicit_ask defaults to False (fail closed) rather than
    True — an unconfirmed request is exactly the "just mentioned it" case
    this field exists to catch, so treating it as an ask by default would
    reopen the bug."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return [], False, None, None

    if not isinstance(data, dict):
        return [], False, None, None

    raw_pids = data.get("perfume_ids")
    explicit_ask = data.get("explicit_ask") is True
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

    return pids, explicit_ask, opening, closing


async def classify_and_phrase(
    message: str, candidates: dict[str, dict]
) -> GroqClassification | None:
    """
    Ask Groq which candidate perfume(s) the message refers to, and for short
    opening/closing phrasing to wrap around the (separately, deterministically
    assembled) price card(s).

    Returns None only when Groq itself couldn't be asked at all (missing key,
    no candidates, API/network error) — see the module docstring for why the
    caller (app.matcher) treats that differently from a successful call that
    confidently found nothing, which returns an empty GroqClassification()
    instead. Never raises.
    """
    if not settings.GROQ_API_KEY or not candidates:
        return None

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

    except Exception:
        logger.exception("Groq classify_and_phrase call failed")
        return None

    pids, explicit_ask, opening, closing = _parse_response(raw)

    valid_pids = [pid for pid in pids if pid in candidates]
    if not valid_pids or not explicit_ask:
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

    return GroqClassification(
        perfume_ids=valid_pids, explicit_ask=True, opening=opening, closing=closing
    )
