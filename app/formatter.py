"""
WhatsApp reply text formatter.

Builds the price card + shipping card from catalog data.
All formatting uses WhatsApp-compatible plain text (bold via *asterisks*).
"""

from app.catalog import PERFUMES, SHIPPING_CARD


# --- Fixed reply messages ---

FALLBACK_MESSAGE = "Which perfume are you asking about? 🙂"

AMBIGUOUS_MESSAGE = (
    "I found more than one perfume in your message — "
    "which one are you asking about? 🙂"
)

NON_TEXT_MESSAGE = "Please type your question and I'll help with pricing 🙂"


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

    # Build the card header
    lines = [f"🌸 *{display_name}*"]

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

    # Add blank line + shipping card
    lines.append("")
    lines.append(SHIPPING_CARD)

    return "\n".join(lines)
