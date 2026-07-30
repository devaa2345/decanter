"""
Groq LLM client — intent judgment, disambiguation, and reply phrasing.

WHAT CHANGED AND WHY
--------------------
Groq used to be the primary matcher: it was handed the top 25 perfumes by
raw n-gram fuzzy score and asked which one the customer meant. That put the
model in an impossible position. With 1,200+ catalog entries the shortlist
was frequently 25 irrelevant products (short unrelated words score high on
string similarity alone), and a small fast model given a plausible-looking
list does not reliably answer "none of these" — so it picked something, and
the customer got a confident price card for a perfume they never mentioned.

app.name_index now decides which perfumes a message could be naming, by
scoring the whole catalog rather than sampling it. By the time this module
is called, the candidate list is short and genuinely relevant. That leaves
Groq the three jobs it is actually good at:

  1. explicit_ask — is the customer asking about this perfume right now, or
     did the name just come up in conversation? A model that sees the whole
     sentence AND the recent chat judges this far better than a word list.
  2. Narrowing — when several real candidates fit ("the EDP one", "the
     second one", a bare series name), pick the one the conversation
     supports.
  3. Phrasing — a short, warm opening and closing around the price card.

It cannot introduce a perfume the index did not find: app.matcher
intersects whatever comes back with the candidate list it sent. Prices are
never generated here at all — app.formatter assembles those from
catalog.py, and the prompt forbids numbers in the model's own text as a
second layer of defense.

RETURN CONTRACT
---------------
  - None means Groq itself could not be asked (no API key, no candidates,
    network/API error). The caller falls back to its own deterministic
    intent gate so an outage degrades manners, not availability.
  - An empty GroqClassification() means Groq ran and judged this not to be
    a request. The caller trusts that and stays silent.

JSON mode, not a text convention: confirmed against llama-3.1-8b-instant
that a "PERFUME_ID: ..." plain-text format is not followed reliably (a real
observed reply was "dior_sauvage_edt: Dior Sauvage EDT"), which the old
regex parser silently discarded. JSON mode is an API guarantee.
"""

import json
import logging
import re
from dataclasses import dataclass, field

from openai import AsyncOpenAI

from app.config import settings

logger = logging.getLogger(__name__)

# How many recent conversation turns to show the model. Enough to resolve
# "and the 5ml?" or "the second one", short enough to keep the prompt cheap
# and the model's attention on the current message.
_HISTORY_TURNS = 6


@dataclass
class GroqClassification:
    """A successful classify_and_phrase call. perfume_ids is empty whenever
    Groq ran fine but judged the message not to be a request — that is a
    real answer, not a failure (see the module docstring for the None case).
    opening/closing are only populated alongside a non-empty perfume_ids,
    since they are useless without one."""

    perfume_ids: list[str] = field(default_factory=list)
    explicit_ask: bool = False
    opening: str | None = None
    closing: str | None = None


def _format_history(history: list[dict] | None) -> str:
    """Render recent turns for the prompt. Bot turns are summarized by which
    perfume cards they showed rather than quoted in full — the price grid is
    long, and what matters for resolving "the second one" is which products
    were on offer, in what order."""
    if not history:
        return ""

    lines: list[str] = []
    for turn in history[-_HISTORY_TURNS:]:
        role = turn.get("role")
        if role == "customer":
            text = (turn.get("text") or "").strip()
            if text:
                lines.append(f"Customer: {text[:200]}")
        elif role == "bot":
            names = turn.get("perfume_names") or []
            if names:
                shown = ", ".join(names[:8])
                lines.append(f"You: (showed price cards for {shown})")
            else:
                text = (turn.get("text") or "").strip()
                if text:
                    lines.append(f"You: {text[:120]}")

    if not lines:
        return ""

    return (
        "\nRECENT CONVERSATION (oldest first, for context only — the "
        "customer's NEW message is below):\n" + "\n".join(lines) + "\n"
    )


def _build_system_prompt(
    candidates: dict[str, dict], history: list[dict] | None = None
) -> str:
    perfume_list = "\n".join(
        f"- {pid}: {data['display_name']}" for pid, data in candidates.items()
    )

    return f"""You are the WhatsApp assistant for Sovereign Scents, a perfume decant business in India. A customer messaged you.

Our search has already found the perfumes below as the closest matches to what the customer typed (it handles misspellings, so the spelling may not match exactly). Your job is NOT to search — it is to judge intent and pick.

CANDIDATES (id: name):
{perfume_list}
{_format_history(history)}
Respond ONLY with a JSON object in exactly this shape, nothing else:
{{"perfume_ids": ["<id from the list above>", ...], "explicit_ask": true/false, "opening": "<one short, friendly line>", "closing": "<one short, friendly line>"}}

RULES:
- perfume_ids lists every candidate the customer is asking about right now. Usually exactly one.
- The candidates come from a misspelling-tolerant search. If the customer's text is a plausible misspelling of a candidate's name, that IS a match — do not reject it for not matching letter for letter.
- PICK ONE when the message says which. If the customer's words single out one candidate — they named the concentration ("EDT", "EDP", "parfum"), the variant, or the brand — return ONLY that one. Do not add the others as alternatives. Returning extra candidates means the customer gets several price cards for a question they already answered themselves.
- If the customer clearly names 2+ different perfumes ("sauvage and eros price"), include all of them — never answer only part of the question.
- Only if several candidates fit EQUALLY, and nothing in the message or the conversation above picks between them, include all the plausible ones rather than guessing.
- Use the conversation above to resolve references: "the second one", "the EDP", "that one", "and the 5ml?" refer to what was already shown. The candidate list is in the order it was shown, so "the second one" means the second candidate listed above.
- Use an empty list [] only if none of the candidates is what the customer means. Never invent an id that is not in the list above.
- explicit_ask is true only if the customer is asking about price, availability, or buying right now. This is a real decision, not a formality — nothing else will catch a mistake here.
  * A bare product name, with or without a size, IS an explicit ask — it is the most common way customers ask ("sauvage", "sauvage 10ml", "kaaf"). Do not mark it false for lacking a question mark or the word "price".
  * A MISSPELLED bare product name is still a bare product name. "9pm rebl", "kaff", "sauvge 10ml" are people typing fast on a phone, and they are asking exactly as much as someone who spelled it correctly. Never set explicit_ask false because the spelling looks off.
  * "I want to confirm X", "I'll take X", "book/order/send me X" IS an explicit ask.
  * A follow-up to a price card you just showed ("and the 5ml?", "how much for the second one", "the EDP one") IS an explicit ask.
  * A perfume merely MENTIONED in passing is NOT an explicit ask. If the sentence is reporting, recalling, or commenting — "the owner told me X is really nice", "my friend uses X", "I bought X last month", "X is good but I don't want it" — set this false even though the name is right there and the sentence sounds positive. Someone telling you about a perfume has not asked you for anything.
  * A thank-you, an acknowledgement ("ok", "thanks bhai"), or a question about delivery or an existing order is NOT an explicit ask, even right after you showed a price card.
  * If perfume_ids is empty, this must be false.
- If perfume_ids is non-empty AND explicit_ask is true, you are CONFIDENT — the opening must sound definitive and matter-of-fact ("Sauvage is one of our favourites!"), never hedge or ask for more details ("which one did you mean?"). That contradicts having just picked it.
- opening and closing must NEVER contain a price, number, size, or currency symbol — that data comes from us, not you. Including a number is a mistake.
- Under 15 words each. Sound like a real person texting, not a script. Hinglish is fine if the customer wrote that way. At most one emoji total.
- Never use the asterisk character (*) anywhere.
- If perfume_ids is empty or explicit_ask is false, still write a brief natural opening, and set closing to an empty string."""


def _parse_response(text: str) -> tuple[list[str], bool, str | None, str | None]:
    """Parse the JSON response. Any failure (invalid JSON, wrong shape,
    non-string fields) returns ([], False, None, None) rather than raising.
    A missing or non-boolean explicit_ask defaults to False (fail closed) —
    an unconfirmed request is exactly the "just mentioned it" case the field
    exists to catch, so defaulting to True would reopen the bug."""
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


# Anything that means "money" in a line the model wrote. Prices come from
# catalog.py via app.formatter and from nowhere else — a number the model
# invented, sitting one line above the real price grid, is how a customer
# ends up quoted something we never charged.
_PRICE_MARKERS = re.compile(r"[₹$€£]|\brs\.?\b|\brupees?\b|\binr\b", re.IGNORECASE)


def _without_prices(line: str | None) -> str | None:
    """
    Drop an opening/closing line that mentions money at all.

    The prompt forbids this explicitly, and the model still does it —
    observed live: a follow-up about a 5ml decant came back with the opening
    "Afnan 9PM Rebel ka 5ml price hai ₹", cut off mid-number by the token
    limit, immediately above the real price card.

    Dropped rather than repaired: app.formatter falls back to its own
    deterministic header, which is always correct, so there is nothing to
    gain by trying to salvage the model's phrasing. Matches currency symbols
    and words, not bare digits — plenty of real product names contain
    numbers ("9PM Rebel", "212 VIP", "Baccarat Rouge 540") and an opening
    naming one of those is exactly what this feature is for.
    """
    if line and _PRICE_MARKERS.search(line):
        logger.warning("Discarded Groq phrasing containing a price: %r", line)
        return None
    return line


_UNSTOCKED_PROMPT = """You read WhatsApp messages sent to a perfume decant shop in India and answer ONE question: is this customer naming a specific perfume they want?

Our catalog search has already looked and found nothing matching, so if they ARE naming a perfume, it is one we do not stock.

Respond ONLY with a JSON object in exactly this shape, nothing else:
{"is_perfume": true/false, "name": "<the perfume name as they wrote it, or empty>"}

RULES:
- is_perfume is true only when the message names a specific fragrance the customer wants the price of or wants to buy. "fahrenheit", "do you have tom ford oud wood", "creed silver mountain water 5ml" are all true.
- It is false for anything else, and most messages are anything else: greetings, thanks, delivery and order questions, payment questions, complaints, small talk, one-word acknowledgements, questions about shipping or discounts, or a message asking for the catalog.
- It is false for a person's name, a place, a courier, or a random word. Do not guess that an unfamiliar word must be a perfume — most unfamiliar words are not.
- It is false if the customer is only mentioning a perfume in passing or saying they do NOT want it.
- name is the perfume as the customer typed it, at most a few words. Do not correct their spelling, do not expand it, do not invent a brand they did not write. Empty string when is_perfume is false."""


@dataclass
class UnstockedRequest:
    """A customer naming a perfume the catalog does not have."""

    name: str


async def identify_unstocked_perfume(
    message: str, history: list[dict] | None = None
) -> UnstockedRequest | None:
    """
    Decide whether a message the catalog could not match is nonetheless a
    customer naming a perfume — so the bot can say "we don't stock that,
    here's the catalog" instead of nothing at all.

    Called only for messages that would otherwise get complete silence (see
    app.main), which is what keeps the cost sane: it never runs for anything
    the index already answered, nor for greetings, catalog requests or order
    confirmations, which are handled before it.

    Returns None for "not a perfume" and for every failure mode — an unsure
    answer here means staying quiet, which is exactly the behaviour this is
    an improvement on, so failing that way costs nothing.
    """
    if not settings.GROQ_API_KEY or not message.strip():
        return None

    try:
        client = AsyncOpenAI(
            api_key=settings.GROQ_API_KEY,
            base_url="https://api.groq.com/openai/v1",
            timeout=settings.GROQ_TIMEOUT_SECONDS,
        )
        response = await client.chat.completions.create(
            model=settings.GROQ_MODEL,
            temperature=0.0,
            max_tokens=80,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _UNSTOCKED_PROMPT + _format_history(history)},
                {"role": "user", "content": message},
            ],
        )
        raw = response.choices[0].message.content or ""
        logger.info("Groq identify_unstocked_perfume response: %r", raw)
    except Exception:
        logger.exception("Groq identify_unstocked_perfume call failed")
        return None

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(data, dict) or data.get("is_perfume") is not True:
        return None

    name = data.get("name")
    return UnstockedRequest(name=name.strip() if isinstance(name, str) else "")


async def classify_and_phrase(
    message: str,
    candidates: dict[str, dict],
    history: list[dict] | None = None,
) -> GroqClassification | None:
    """
    Ask Groq which candidate perfume(s) the customer is asking about, and for
    short phrasing to wrap around the separately-assembled price card.

    `candidates` is app.name_index's shortlist — already relevant, so this is
    a judgment call rather than a search. `history` is the recent
    conversation (see app.conversation.recent_turns), used to resolve
    follow-ups and to read intent in context.

    Returns None only when Groq itself could not be asked. Never raises.
    """
    if not settings.GROQ_API_KEY or not candidates:
        return None

    try:
        client = AsyncOpenAI(
            api_key=settings.GROQ_API_KEY,
            base_url="https://api.groq.com/openai/v1",
            timeout=settings.GROQ_TIMEOUT_SECONDS,
        )

        response = await client.chat.completions.create(
            model=settings.GROQ_MODEL,
            # Low but not zero: this is a classification call with a small
            # phrasing task attached, and the classification half should be
            # as close to deterministic as the API allows.
            temperature=0.15,
            max_tokens=250,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _build_system_prompt(candidates, history)},
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

    # Safety net: a confident match must not hedge. The prompt asks for this,
    # but llama-3.1-8b-instant does not always comply — a real observed case
    # matched a specific perfume_id and still wrote "We have several options,
    # can you give me a hint?". A question mark next to a confident price
    # card reads as contradictory, so discard it and let app.formatter's
    # deterministic header take over.
    if opening and "?" in opening:
        opening = None

    opening = _without_prices(opening)
    closing = _without_prices(closing)

    return GroqClassification(
        perfume_ids=valid_pids, explicit_ask=True, opening=opening, closing=closing
    )
