"""
Order-confirmation message detector.

When a customer taps "confirm order" on the Sovereign Scents website, it
opens WhatsApp with a pre-filled templated message — order number, line
items, total, and a link back to the admin order page. That's a system-
generated acknowledgment, not a price question, and must never reach the
perfume matcher: the line items are real perfume names and would otherwise
happily match and get quoted a price card back instead of an order ack.

Detection deliberately does NOT use app.matcher.normalize_message, which
strips punctuation — the "#", "/", and "." here are exactly what makes the
order-link and order-number signals reliable.
"""

import re

_ORDER_LINK_RE = re.compile(r"sovereignscents\.in/admin/order/", re.IGNORECASE)
_CONFIRM_PHRASE_RE = re.compile(r"confirm\s+my\s+order", re.IGNORECASE)
_ORDER_NUMBER_RE = re.compile(r"order\s*#\s*ss\d+", re.IGNORECASE)


def is_order_confirmation(text: str) -> bool:
    """
    True if the message is the auto-generated "confirm my order" template
    from the website, not a free-form customer question.

    The order-link URL alone is treated as conclusive — it's not something
    a customer would organically type. Without it (e.g. the link format
    changes later), both the confirmation phrase AND an order number are
    required, so a vague "confirm my order" with no specifics still falls
    through to the normal flow — there'd be nothing to actually confirm.
    """
    if not text:
        return False

    if _ORDER_LINK_RE.search(text):
        return True

    return bool(_CONFIRM_PHRASE_RE.search(text) and _ORDER_NUMBER_RE.search(text))
