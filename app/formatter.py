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


def _format_price(price: int) -> str:
    """Format price with ₹ symbol and comma separators."""
    return f"₹{price:,}"


def build_price_card(perfume_id: str) -> str:
    """
    Build the full reply message for a matched perfume.

    Returns the price card + shipping card as a single string.
    Always shows all available size tiers (never just one size).
    """
    perfume = PERFUMES.get(perfume_id)
    if not perfume:
        return FALLBACK_MESSAGE

    prices = perfume["prices"]
    display_name = perfume["display_name"]

    # Build the card header — no asterisks (see module docstring) and no
    # leading emoji (🌸 was one of the things suspected before "*" was
    # isolated as the actual cause; left plain since only 📋/🙂 are
    # individually confirmed safe and neither fits this context).
    lines = [display_name]

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

    # Add blank line + shipping card + contact-you closing line
    lines.append("")
    lines.append(SHIPPING_CARD)
    lines.append("")
    lines.append(WILL_CONTACT_LINE)

    return "\n".join(lines)
