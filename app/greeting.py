"""
Greeting / catalog-request detector.

is_greeting_or_catalog_request is called after the perfume matcher has
already failed to find a match (see app/main.py) — its job is to
distinguish "customer wants to see what we sell" (a greeting, or an
explicit ask for the catalog) from everything else (random chit-chat,
delivery questions, thank-yous), so the bot only spends a reply — and a
Chat Mitra conversation credit — on messages worth answering. Deterministic
and free, same style as app.order_confirmation.

is_catalog_request (the narrower _CATALOG_PHRASES-only check) is instead
called BEFORE the matcher, as an early veto — see app/main.py — because a
catalog phrase like "catalogue" was confirmed in production to sometimes
get hijacked by Groq or the fuzzy matcher into a wrong perfume's price card
instead of the catalog link.
"""

from app.matcher import normalize_message

_GREETING_WORDS = {
    "hi", "hii", "hiii", "hiiii", "hello", "helo", "hlo", "hey", "heya",
    "heyy", "yo", "sup", "namaste", "namaskar",
}

_GREETING_PHRASES = (
    "good morning", "good afternoon", "good evening", "good day",
)

_CATALOG_PHRASES = (
    "catalog", "catalogue", "price list", "pricelist", "rate list",
    "ratelist", "full list", "send catalog", "send list",
    "send me catalog", "send me the list", "what do you have",
    "what all do you have", "what all you have", "show me",
    "show catalogue", "menu", "all perfumes", "all products",
    "your products", "full catalog", "what perfumes", "what brands",
    "list of perfumes", "product list", "what do you sell",
)


def is_catalog_request(text: str) -> bool:
    """
    True if the message explicitly asks to see the catalog/price list —
    the _CATALOG_PHRASES subset only, not bare greeting words.

    Split out from is_greeting_or_catalog_request so app.main can use this
    narrower check as an early veto: a message this specific about wanting
    the catalog (e.g. "catalogue", "send me the catalogue please") should
    never be handed to Groq or the fuzzy matcher, which can otherwise guess
    a wrong perfume instead of recognizing it as a catalog ask — confirmed
    in production for both of those exact examples. Bare greeting words
    ("hi", "hello") are deliberately excluded from this narrower check:
    they're a common opener before naming a (possibly misspelled) perfume
    in the same message, so they still go through the full Groq/fuzzy-
    tolerant pipeline rather than being vetoed this early.
    """
    if not text:
        return False

    normalized = normalize_message(text)
    if not normalized:
        return False

    return any(phrase in normalized for phrase in _CATALOG_PHRASES)


def is_greeting_or_catalog_request(text: str) -> bool:
    """
    True if the message is a greeting or an explicit request to see the
    catalog — the only unmatched-message cases worth replying to.
    """
    if not text:
        return False

    normalized = normalize_message(text)
    if not normalized:
        return False

    words = normalized.split()

    # A greeting as the opener ("hi", "hello there", "hey what's up") —
    # checking just the first word keeps this robust to whatever follows.
    if words and words[0] in _GREETING_WORDS:
        return True

    if any(phrase in normalized for phrase in _GREETING_PHRASES):
        return True

    return is_catalog_request(text)
