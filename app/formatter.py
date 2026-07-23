"""
WhatsApp reply text formatter.

Builds the price card + shipping card from catalog data.

No asterisks anywhere in outbound text: WhatsApp itself renders *word* as
bold, but Chat Mitra's send API rejects the literal "*" character outright
("Text contains invalid characters") — confirmed in production, isolated
via direct API tests (a lone "*" alone was enough to trigger it). Plain
text only.
"""

from app.catalog import PERFUMES, SHIPPING_CARD


# --- Fixed reply messages ---

# Full catalog sheet, sent to anyone who hasn't named a specific perfume yet.
#
# Deliberately the plain base link, no ?gid=/#gid= tab-selector — confirmed
# in production that Chat Mitra's send API rejects a "#" fragment in the
# message body outright ("Text contains invalid characters"). This opens
# the whole workbook rather than a specific tab, which is an acceptable
# tradeoff for the link actually being deliverable.
CATALOG_SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/118f-ZLbawdsi38eDv9TqV7pq-yxjaHPdunruCwuerb8"
)

FALLBACK_MESSAGE = (
    f"📋 Here's our full catalog - all perfumes, decants & prices:\n{CATALOG_SHEET_URL}\n\n"
    "Please tell me the name of the perfume/decant and what quantity (ml) you'd like, "
    "and I'll get you the price! 🙂"
)

AMBIGUOUS_MESSAGE = (
    "I found more than one perfume in your message - "
    "which one are you asking about? 🙂"
)

NON_TEXT_MESSAGE = "Please type your question and I'll help with pricing 🙂"

ORDER_CONFIRMATION_MESSAGE = "Thank you for confirming your order! We will contact you shortly!!"

WILL_CONTACT_LINE = "We'll contact you shortly!"

# Safety cap on how many candidates an ambiguous-match card lists — the
# known cases (the "9pm" family, a catalog keyword collision) are always a
# handful, but this guards against ever dumping an unreadably long list.
_MAX_AMBIGUOUS_CANDIDATES = 8


def _format_price(price: int) -> str:
    """Format price with ₹ symbol and comma separators."""
    return f"₹{price:,}"


def _price_range(prices: dict) -> str:
    """Compact 'lowest - highest' price span across every size tier."""
    values = list(prices.values())
    if not values:
        return "price on request"
    lo, hi = min(values), max(values)
    return _format_price(lo) if lo == hi else f"{_format_price(lo)} - {_format_price(hi)}"


def build_ambiguous_card(perfume_ids: list[str] | None) -> str:
    """
    List every genuinely ambiguous candidate (name + price range) instead of
    just asking "which one?" with no information to go on. Falls back to
    the plain AMBIGUOUS_MESSAGE if no candidate list was supplied (should
    only happen for a matcher path that hasn't been taught to populate
    matched_perfume_ids yet — never a hard failure).
    """
    if not perfume_ids:
        return AMBIGUOUS_MESSAGE

    shown = perfume_ids[:_MAX_AMBIGUOUS_CANDIDATES]
    lines = ["I found more than one perfume in your message:", ""]

    for i, pid in enumerate(shown, start=1):
        perfume = PERFUMES.get(pid)
        if not perfume:
            continue
        lines.append(f"{i}. {perfume['display_name']} - {_price_range(perfume.get('prices', {}))}")

    remaining = len(perfume_ids) - len(shown)
    if remaining > 0:
        lines.append(f"...and {remaining} more")

    lines.append("")
    lines.append("Please reply with the exact name of the one you'd like!")

    return "\n".join(lines)


def build_price_card(
    perfume_id: str,
    opening: str | None = None,
    closing: str | None = None,
) -> str:
    """
    Build the full reply message for a matched perfume.

    Returns the price card + shipping card as a single string.
    Always shows all available size tiers (never just one size).

    opening/closing (from Groq's classify_and_phrase — see app.matcher)
    wrap the card with short, natural, personalized phrasing when
    available. The name header, price grid, and shipping card underneath
    are always the exact same deterministic text either way — Groq never
    touches a price, only what surrounds it. Without them (Groq
    unavailable, or the free fallback matcher resolved it instead), this
    renders with the same plain header/closing it always has.
    """
    perfume = PERFUMES.get(perfume_id)
    if not perfume:
        return FALLBACK_MESSAGE

    prices = perfume["prices"]
    display_name = perfume["display_name"]

    # Build the card header — no asterisks (see module docstring) and no
    # leading emoji (🌸 was one of the things suspected before "*" was
    # isolated as the actual cause; left plain since only 📋/🙂 are
    # individually confirmed safe and neither fits this context). Uppercase
    # name + a plain "-" divider (confirmed safe) fakes emphasis without
    # any markdown Chat Mitra might reject.
    divider = "-" * min(max(len(display_name), 12), 32)
    lines = []
    if opening:
        lines.append(opening)
        lines.append("")
    lines.append(display_name.upper())
    lines.append(divider)

    # Define the standard decant tiers and full bottle tier
    decant_sizes = ["3ml", "5ml", "8ml", "10ml", "20ml", "30ml"]
    full_sizes = [k for k in prices if "full" in k or (k.endswith("ml") and k not in decant_sizes)]

    # Build 2-column layout for decant sizes
    # Left column: 3ml, 5ml, 8ml  |  Right column: 10ml, 20ml, 30ml
    left = []
    right = []
    for size in decant_sizes[:3]:  # 3ml, 5ml, 8ml
        if size in prices:
            left.append(f"{size}  {_format_price(prices[size])}")
    for size in decant_sizes[3:]:  # 10ml, 20ml, 30ml
        if size in prices:
            right.append(f"{size} {_format_price(prices[size])}")

    # Pair up left and right columns
    max_rows = max(len(left), len(right))
    for i in range(max_rows):
        l_part = left[i] if i < len(left) else ""
        r_part = right[i] if i < len(right) else ""
        if l_part and r_part:
            # Pad left part to align columns
            lines.append(f"{l_part:<16}{r_part}")
        elif l_part:
            lines.append(l_part)
        elif r_part:
            lines.append(f"{'':16}{r_part}")

    # Add full bottle prices
    for size_key in sorted(full_sizes):
        if size_key in prices:
            # Extract ML number for display
            ml_num = size_key.replace("ml_full", "").replace("ml", "")
            if ml_num.isdigit():
                lines.append(f"Full {ml_num}ml  {_format_price(prices[size_key])}")
            else:
                lines.append(f"Full bottle  {_format_price(prices[size_key])}")

    # Closing divider + shipping card + contact-you line (Groq's closing
    # phrasing if we have it, else the plain deterministic default)
    lines.append(divider)
    lines.append(SHIPPING_CARD)
    lines.append("")
    lines.append(closing or WILL_CONTACT_LINE)

    return "\n".join(lines)
