"""
WhatsApp reply text formatter.

Builds the price card + shipping card from catalog data.

No asterisks anywhere in outbound text: WhatsApp itself renders *word* as
bold, but Chat Mitra's send API rejects the literal "*" character outright
("Text contains invalid characters") — confirmed in production, isolated
via direct API tests (a lone "*" alone was enough to trigger it). Plain
text only.
"""

import re

from app.catalog import PERFUMES, SHIPPING_CARD, SHIPPING_RATES


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


def build_not_in_stock_message(perfume_name: str | None = None) -> str:
    """
    Reply for a customer who clearly named a perfume that isn't in the
    catalog at all.

    The alternative — what this replaced — was total silence, which reads as
    being ignored mid-conversation. Answering costs a Chat Mitra credit, but
    a customer who has just typed a specific product name is a real buyer
    worth the credit, and the catalog link gives them somewhere to go.

    The name is echoed back so the reply doesn't read as a generic bounce,
    but it is stripped to plain letters, digits and spaces first: it arrives
    from the LLM, and nothing that came from a model should reach a customer
    unfiltered. Falls back to "that one" if nothing usable survives.
    """
    cleaned = re.sub(r"[^A-Za-z0-9 ]", " ", perfume_name or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()[:40]
    subject = cleaned if len(cleaned) >= 3 else "that one"

    return (
        f"Sorry, we don't have {subject} in our list right now 🙂\n\n"
        "Here's our full catalog - you might find something similar:\n"
        f"{CATALOG_SHEET_URL}\n\n"
        "Tell me any name from the sheet and I'll send you the price!"
    )

ORDER_CONFIRMATION_MESSAGE = "Thank you for confirming your order! We will contact you shortly!!"

WILL_CONTACT_LINE = "We'll contact you shortly!"

# Sent once per sender, on their very first-ever inbound message regardless
# of what it says (see app.analytics.is_first_contact / app.main's
# first-contact branch). Any later greeting from that same sender still
# gets FALLBACK_MESSAGE as usual — this only fires once, ever.
#
# Adapted from the owner's pasted template: no asterisks (bold markers -
# confirmed to make Chat Mitra's send API reject the whole message), no
# "#" fragment in the sheet URL (also confirmed to get rejected - reuses
# CATALOG_SHEET_URL, the same fragment-free link FALLBACK_MESSAGE uses),
# the markdown [text](url) link replaced with a plain URL (WhatsApp
# doesn't render markdown links anyway), and "-" in place of "•" bullets
# (untested character - "-" is already confirmed safe throughout the rest
# of this file).
WELCOME_MESSAGE = (
    "Hey! 👋\n\n"
    "Thank you for reaching out to Sovereign Scents!\n\n"
    "Here is our Decant Catalog Sheet of 1000+ Perfumes:\n"
    f"🔗 {CATALOG_SHEET_URL}\n\n"
    "The sheet has 4 tabs:\n"
    "1. Men's / Unisex\n"
    "2. Women's\n"
    "3. Decant Pictures\n"
    "4. Customer Reviews\n\n"
    "📦 Order Info\n"
    "- No Minimum Order Value\n"
    "- 🚚 ₹80 flat shipping below ₹1000\n"
    "- 🎁 Free surface shipping on ₹1000+ (3+ decants)\n"
    "- ✈️ Bluedart Air ₹35 (above ₹1000)\n"
    "- 🔥 Flat 5% off above ₹2000 (Minimum 6 decants up to 10ml)\n"
    "- 📦 Dispatch within 48 hours\n\n"
    "📍 Ships from Noida\n\n"
    "⭐ Why Choose Sovereign Scents?\n"
    "- All decants come with metal atomizers (no extra cost)\n"
    "- Premium labelled decants\n"
    "- Carefully packed & handled\n"
    "- Spare atomizers included\n"
    "- Free tester strips with all orders\n\n"
    "Please check the sheet and let us know what you'd like - all prices are listed there.\n\n"
    "Our Community Link: https://chat.whatsapp.com/DYFroG1QrLC54DZUrBaD61\n\n"
    "Looking forward to serving you! 🤝\n\n"
    "Himanshu\n"
    "Sovereign Scents\n"
    "WhatsApp - 8826709398\n"
    "Best time to reach out - After 9PM"
)

# Safety cap on how many perfumes a multi-perfume reply shows full cards
# for — the known cases (the "9pm" family, a catalog keyword collision, a
# customer naming a few products at once) are always a handful, but this
# guards against ever dumping an unreadably long message.
_MAX_MULTI_CANDIDATES = 8


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


# Standard decant tiers — anything else in a perfume's prices dict (e.g.
# "100ml_full", or the odd bare "40ml" bottle) is treated as a full-bottle
# size instead. Shared by _build_card_block's full grid and by
# _resolve_size_key/_size_display_label for the single-size reply path.
_DECANT_SIZES = ("3ml", "5ml", "8ml", "10ml", "20ml", "30ml")


def _size_display_label(size_key: str) -> str:
    """Human label for one prices-dict key: decant tiers show as-is
    ("3ml"), full-bottle tiers show as "Full <n>ml" (or "Full bottle" if
    the size can't be parsed as a plain number)."""
    if size_key in _DECANT_SIZES:
        return size_key
    ml_num = size_key.replace("ml_full", "").replace("ml", "")
    return f"Full {ml_num}ml" if ml_num.isdigit() else "Full bottle"


def _resolve_size_key(prices: dict, requested_ml: int) -> str | None:
    """
    Map a bare ml number parsed from the customer's message (see
    app.matcher.extract_requested_size_ml) onto this perfume's actual
    prices-dict key. Catalog sizes are stored either as "Xml" (decant
    tiers) or "Xml_full" (full bottles) — see catalog_data.json's shape —
    so both forms are tried. Returns None if this perfume simply doesn't
    come in that size, so the caller can fall back to showing every size
    it does offer instead.
    """
    for key in (f"{requested_ml}ml", f"{requested_ml}ml_full"):
        if key in prices:
            return key
    return None


def _delivery_total_lines(price: int) -> list[str]:
    """
    Delivery cost per region added to one specific size's price, with the
    grand total shown alongside it — used only when the customer named an
    exact ml size (build_price_card/build_multi_price_card's requested_ml),
    so the price+shipping math is a single visible number per region
    instead of a separate lookup. Same characters as SHIPPING_CARD (₹, "-",
    plain text) — no untested punctuation near Chat Mitra's send API.
    """
    lines = ["Prepaid only, no COD", "Delivery + grand total:"]
    for region, rate in SHIPPING_RATES.items():
        lines.append(f"₹{rate} - {region} - Total {_format_price(price + rate)}")
    return lines


def _build_card_block(perfume: dict, size_key: str | None = None) -> list[str]:
    """
    Name header + price grid lines for one perfume — no opening/closing/
    shipping card, just the self-contained visual block. Shared by
    build_price_card (one perfume) and build_multi_price_card (2+
    perfumes, which shows one of these per perfume but only ONE shared
    shipping card at the end instead of repeating it per item).

    size_key, when given, must be one of this perfume's actual prices-dict
    keys (see _resolve_size_key) — shows ONLY that one size instead of the
    full grid, since the customer named one specific ml ("9pm rebel 3ml")
    and every other tier alongside it would just be noise.

    No asterisks anywhere (see module docstring) and no leading emoji (🌸
    was one of the things suspected before "*" was isolated as the actual
    cause; left plain since only 📋/🙂 are individually confirmed safe and
    neither fits this context). Uppercase name + a plain "-" divider
    (confirmed safe) fakes emphasis without any markdown Chat Mitra might
    reject.
    """
    prices = perfume["prices"]
    display_name = perfume["display_name"]

    divider = "-" * min(max(len(display_name), 12), 32)
    lines = [display_name.upper(), divider]

    if size_key and size_key in prices:
        lines.append(f"{_size_display_label(size_key)}  {_format_price(prices[size_key])}")
        lines.append(divider)
        return lines

    # Define the standard decant tiers and full bottle tier
    decant_sizes = _DECANT_SIZES
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
    for full_key in sorted(full_sizes):
        if full_key in prices:
            lines.append(f"{_size_display_label(full_key)}  {_format_price(prices[full_key])}")

    lines.append(divider)
    return lines


def build_price_card(
    perfume_id: str,
    opening: str | None = None,
    closing: str | None = None,
    requested_ml: int | None = None,
) -> str:
    """
    Build the full reply message for a single matched perfume.

    Returns the price card + shipping card as a single string.
    By default shows all available size tiers. If requested_ml (parsed
    from the customer's message — see app.matcher.extract_requested_size_ml)
    names a size this perfume actually offers, shows ONLY that size instead,
    with delivery cost added per region and the grand total shown alongside
    it (see _delivery_total_lines) rather than the generic shipping card.
    Falls back to the full grid + generic shipping card if this perfume
    doesn't come in the requested size at all, so the customer still sees
    what sizes ARE available instead of a dead end.

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
    size_key = _resolve_size_key(prices, requested_ml) if requested_ml else None

    lines = []
    if opening:
        lines.append(opening)
        lines.append("")
    lines.extend(_build_card_block(perfume, size_key=size_key))

    if size_key:
        lines.extend(_delivery_total_lines(prices[size_key]))
    else:
        lines.append(SHIPPING_CARD)

    lines.append("")
    lines.append(closing or WILL_CONTACT_LINE)

    return "\n".join(lines)


def build_multi_price_card(
    perfume_ids: list[str] | None,
    opening: str | None = None,
    closing: str | None = None,
    requested_ml: int | None = None,
) -> str:
    """
    Reply for 2+ perfumes found in one message — used both when the
    customer clearly named multiple distinct products (e.g. "sauvage and
    eros price") and when a single mention was ambiguous among close
    variants (e.g. bare "9pm" matching 4 real products). Either way,
    confirmed in production that customers want to actually see the real
    candidates with their prices, not just a random single guess or a
    content-free "which one?" prompt.

    requested_ml (see build_price_card) collapses every candidate that
    actually offers that size to a single compact line each, with ONE
    combined subtotal + one shared delivery/grand-total block at the end —
    never a full size grid per candidate. Confirmed live as a real bug
    ("Rare Carbon 3ml"): the previous all-or-nothing version fell back to
    dumping the FULL size grid for every one of 5 unrelated candidates the
    moment even a single one didn't offer the requested size, which is far
    worse than just noting that one candidate separately and still
    answering cleanly for the rest. A candidate that doesn't come in the
    requested size gets a brief one-line note (with the sizes it DOES come
    in) instead of either its full grid or being silently dropped. Falls
    back to the full comparison grid + one shared shipping card only when
    NONE of the candidates offer the requested size at all — there's
    nothing size-specific to total in that case.

    Capped at _MAX_MULTI_CANDIDATES so a wide keyword collision can't
    produce an unreadably long message. Falls back to AMBIGUOUS_MESSAGE if
    no usable candidate list was supplied.
    """
    if not perfume_ids:
        return AMBIGUOUS_MESSAGE

    shown = perfume_ids[:_MAX_MULTI_CANDIDATES]
    perfumes = [PERFUMES[pid] for pid in shown if pid in PERFUMES]
    if not perfumes:
        return AMBIGUOUS_MESSAGE

    priced: list[tuple[dict, str]] = []
    unavailable: list[dict] = []
    if requested_ml:
        for perfume in perfumes:
            size_key = _resolve_size_key(perfume["prices"], requested_ml)
            if size_key:
                priced.append((perfume, size_key))
            else:
                unavailable.append(perfume)

    lines = []
    if opening:
        lines.append(opening)
        lines.append("")

    if priced:
        item_lines = [
            f"{p['display_name'].upper()} - {_size_display_label(sk)}  {_format_price(p['prices'][sk])}"
            for p, sk in priced
        ]
        for p in unavailable:
            available = ", ".join(_size_display_label(k) for k in p["prices"])
            item_lines.append(
                f"{p['display_name'].upper()} - not available in {requested_ml}ml (has: {available})"
            )

        divider = "-" * min(max(max(len(line) for line in item_lines), 12), 40)
        lines.extend(item_lines)
        lines.append(divider)

        subtotal = sum(p["prices"][sk] for p, sk in priced)
        lines.append(f"Subtotal  {_format_price(subtotal)}")
        lines.append("")
        lines.extend(_delivery_total_lines(subtotal))
    else:
        # No size named, or none of the candidates offer the one that was —
        # the full comparison grid + one shared shipping card, as always.
        for i, perfume in enumerate(perfumes):
            if i > 0:
                lines.append("")
            lines.extend(_build_card_block(perfume))
        lines.append("")
        lines.append(SHIPPING_CARD)

    remaining = len(perfume_ids) - len(shown)
    if remaining > 0:
        lines.append("")
        lines.append(f"...and {remaining} more match your message - tell me the exact name for its price too!")

    lines.append("")
    lines.append(closing or WILL_CONTACT_LINE)

    return "\n".join(lines)
