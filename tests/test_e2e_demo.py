"""
End-to-end demo: misspelled customer messages → full 3-layer pipeline → WhatsApp reply.

Simulates two real-world messages with typos:
  1. "suvage 10ml"   (misspelling of "sauvage")
  2. "9 pm 20ml"     (extra space in "9pm" — breaks exact + fuzzy, caught by LLM)

Shows which layer caught it, the confidence score, and the exact
WhatsApp reply text the customer would receive.
"""

import asyncio
import logging
import sys
import os

# Fix Windows console encoding for emoji/Unicode output
sys.stdout.reconfigure(encoding="utf-8")

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Enable logging so we see Groq LLM responses
logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")

from app.matcher import match_perfume, normalize_message, _layer1_exact_match, _layer2_fuzzy_match
from app.formatter import build_price_card, FALLBACK_MESSAGE, AMBIGUOUS_MESSAGE


# ── Colours for terminal output ─────────────────────────────────────
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
SEPARATOR = f"{DIM}{'=' * 70}{RESET}"
THIN_SEP = f"{DIM}{'─' * 70}{RESET}"


def layer_color(layer: str | None) -> str:
    """Map layer name to a terminal colour."""
    return {"exact": GREEN, "fuzzy": YELLOW, "llm": CYAN}.get(layer or "", RED)


async def demo_single_message(raw_message: str):
    """Run one message through every layer and print a detailed trace."""
    print(f"\n{SEPARATOR}")
    print(f"  {BOLD}Customer sends:{RESET}  \"{raw_message}\"")
    print(SEPARATOR)

    normalized = normalize_message(raw_message)
    print(f"\n   Normalized text:  \"{normalized}\"")
    print(f"{THIN_SEP}")

    # ── Layer 1: Exact / substring ───────────────────────────────────
    r1 = _layer1_exact_match(normalized)
    if r1.perfume_id:
        c = layer_color("exact")
        print(f"   {c}[PASS] Layer 1 (exact/substring):{RESET}")
        print(f"         keyword '{r1.matched_keyword}' found in message")
        print(f"         => {r1.perfume_id}  (confidence: {r1.confidence}%)")
    else:
        print(f"   {RED}[MISS] Layer 1 (exact/substring):{RESET}  no keyword matched")

    # ── Layer 2: Fuzzy ───────────────────────────────────────────────
    r2 = _layer2_fuzzy_match(normalized)
    if r2.perfume_id:
        c = layer_color("fuzzy")
        print(f"   {c}[PASS] Layer 2 (fuzzy match):{RESET}")
        print(f"         '{normalized}' ~ '{r2.matched_keyword}'")
        print(f"         => {r2.perfume_id}  (similarity: {r2.confidence:.1f}%)")
    else:
        print(f"   {RED}[MISS] Layer 2 (fuzzy match):{RESET}  no close keyword found")

    # ── Full pipeline (Layer 3 only fires if L1+L2 missed) ───────────
    print(f"{THIN_SEP}")
    final = await match_perfume(raw_message)

    if final.layer == "llm":
        c = layer_color("llm")
        print(f"   {c}[PASS] Layer 3 (Groq LLM):{RESET}")
        print(f"         LLM classified '{raw_message}' => {final.perfume_id}")
        print(f"         (confidence: {final.confidence}%)")
    elif final.layer in ("exact", "fuzzy"):
        layer_num = "1" if final.layer == "exact" else "2"
        print(f"   {DIM}[SKIP] Layer 3 (Groq LLM):  already matched at Layer {layer_num}{RESET}")
    else:
        print(f"   {RED}[MISS] Layer 3 (Groq LLM):{RESET}  LLM could not identify perfume")

    # ── Result summary ───────────────────────────────────────────────
    print(f"\n{THIN_SEP}")
    if final.ambiguous:
        print(f"   {YELLOW}RESULT: AMBIGUOUS — multiple perfumes detected{RESET}")
        reply = AMBIGUOUS_MESSAGE
    elif final.perfume_id:
        lc = layer_color(final.layer)
        print(f"   {lc}{BOLD}RESULT:  MATCHED!{RESET}")
        print(f"     Perfume ID : {final.perfume_id}")
        print(f"     Via Layer  : {final.layer}")
        print(f"     Confidence : {final.confidence:.1f}%")
        print(f"     Keyword    : {final.matched_keyword or '(LLM classified)'}")
        reply = build_price_card(final.perfume_id)
    else:
        print(f"   {RED}{BOLD}RESULT:  NO MATCH — fallback reply{RESET}")
        reply = FALLBACK_MESSAGE

    # ── WhatsApp reply preview ───────────────────────────────────────
    print(f"\n   {BOLD}{CYAN}WhatsApp reply the customer would receive:{RESET}")
    box_width = 54
    print(f"   {DIM}{'.' * box_width}{RESET}")
    for line in reply.split("\n"):
        padded = line.ljust(box_width - 4)
        print(f"   {DIM}:{RESET} {padded} {DIM}:{RESET}")
    print(f"   {DIM}{'.' * box_width}{RESET}")


async def main():
    print(f"\n{BOLD}{'#' * 70}")
    print(f"#  DECANTER BOT — 3-LAYER MATCHING PIPELINE DEMO")
    print(f"#  Testing misspelled perfume names through all layers")
    print(f"{'#' * 70}{RESET}")

    test_messages = [
        "suvage 10ml",       # misspelling of "sauvage" — Layer 2 catches it
        "9 pm 20ml",         # extra space in "9pm" — only Layer 3 (LLM) can resolve
    ]

    for msg in test_messages:
        await demo_single_message(msg)

    print(f"\n{SEPARATOR}")
    print(f"  {BOLD}{GREEN}Demo complete — both messages processed{RESET}")
    print(f"{SEPARATOR}\n")


if __name__ == "__main__":
    asyncio.run(main())
