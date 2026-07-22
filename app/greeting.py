"""
Greeting / catalog-request detector.

Called only after the perfume matcher has already failed to find a match
(see app/main.py) — its job is to distinguish "customer wants to see what
we sell" (a greeting, or an explicit ask for the catalog) from everything
else (random chit-chat, delivery questions, thank-yous), so the bot only
spends a reply — and a Chat Mitra conversation credit — on messages worth
answering. Deterministic and free, same style as app.order_confirmation.
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

    if any(phrase in normalized for phrase in _CATALOG_PHRASES):
        return True

    return False
