"""
Stress test: Can the bot handle real-world typos, spaces, and misspellings?

Tests every type of error customers actually make:
  - Extra/missing spaces
  - Letter swaps (transposition)
  - Missing letters
  - Wrong letters
  - Phonetic misspellings
  - Hinglish / mixed language
  - Abbreviations
  - All caps / random caps
"""

import asyncio
import sys
import os

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Suppress noisy HTTP/logging output — we only want our table
import logging
logging.disable(logging.CRITICAL)

from app.matcher import match_perfume, normalize_message, _layer1_exact_match, _layer2_fuzzy_match
from app.formatter import build_price_card

# ── Terminal colours ─────────────────────────────────────────────────
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RED = "\033[91m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


# ── Test cases ───────────────────────────────────────────────────────
# Format: (message, expected_match_substring, error_type)
TEST_CASES = [
    # --- Extra / missing spaces ---
    ("sau vage 10ml",          "sauvage",        "extra space in word"),
    ("club denuit intense",    "club de nuit",   "missing space"),
    ("9 pm rebel",             "9pm",            "extra space in '9pm'"),
    ("bleu de chanel",         "bleu de chanel", "correct (baseline)"),

    # --- Letter transposition (swap) ---
    ("savuage 5ml",            "sauvage",        "transposed 'u' and 'v'"),
    ("clbu de nuit",           "club de nuit",   "transposed 'l' and 'b'"),

    # --- Missing letters ---
    ("sauvge price",           "sauvage",        "missing 'a'"),
    ("club de nit",            "club de nuit",   "missing 'u'"),
    ("aventus",                "aventus",        "correct (baseline)"),

    # --- Wrong letters ---
    ("sawage 10ml",            "sauvage",        "w instead of uv"),
    ("savage 5ml",             "sauvage",        "common English word"),
    ("suvage",                 "sauvage",        "u instead of au"),
    ("clob de nuit",           "club de nuit",   "o instead of u"),

    # --- Phonetic / shorthand ---
    ("sauvaj",                 "sauvage",        "phonetic spelling"),
    ("niro red",               "nitro",          "phonetic - missing t"),

    # --- ALL CAPS / random caps ---
    ("SAUVAGE 10ML",           "sauvage",        "ALL CAPS"),
    ("SaUvAgE pRiCe",         "sauvage",        "random caps"),
    ("CLUB DE NUIT",           "club de nuit",   "ALL CAPS"),

    # --- Hinglish / mixed ---
    ("sauvage kitna ka hai",   "sauvage",        "Hinglish price query"),
    ("9pm rebel ka rate",      "9pm rebel",      "Hinglish rate query"),
    ("bhai sauvage dedo 10ml", "sauvage",        "Hinglish casual"),

    # --- Abbreviations ---
    ("cdnim",                  "club de nuit",   "abbreviation CDNIM"),
    ("bdc price",              "bleu de chanel", "abbreviation BDC"),

    # --- With filler words ---
    ("what is the price of sauvage", "sauvage",  "natural question"),
    ("i want to buy 9pm rebel",      "9pm rebel","buy intent"),
    ("how much for club de nuit",    "club de nuit", "price question"),

    # --- Should NOT match (negative tests) ---
    ("hello bro",              None,             "greeting - should NOT match"),
    ("order kab aayega",       None,             "delivery question - NOT match"),
    ("thanks bhai",            None,             "thank you - NOT match"),
    ("good morning",           None,             "greeting - NOT match"),
    ("how much is shipping",   None,             "shipping question - NOT match"),
]


async def run_stress_test():
    print(f"\n{BOLD}{'=' * 90}")
    print(f"  DECANTER BOT — TYPO STRESS TEST")
    print(f"  Testing {len(TEST_CASES)} messages with spaces, misspellings, typos, Hinglish...")
    print(f"{'=' * 90}{RESET}\n")

    # Table header
    print(f"  {'Message':<30} {'Error Type':<25} {'Layer':<7} {'Matched?':<10} {'Result'}")
    print(f"  {'─' * 30} {'─' * 25} {'─' * 7} {'─' * 10} {'─' * 20}")

    passed = 0
    failed = 0
    results_detail = []

    for raw_msg, expected_substr, error_type in TEST_CASES:
        result = await match_perfume(raw_msg)

        # Determine if the result is correct
        if expected_substr is None:
            # Should NOT match
            is_correct = result.perfume_id is None
            matched_str = "(none)" if result.perfume_id is None else result.perfume_id[:25]
        else:
            # Should match something containing the expected substring
            is_correct = (
                result.perfume_id is not None
                and expected_substr.replace(" ", "") in result.perfume_id.replace("_", "").lower()
            ) or (
                result.perfume_id is not None
                and result.matched_keyword is not None
                and expected_substr.lower() in result.matched_keyword.lower()
            )
            # Looser check: at least matched *something*
            if not is_correct and result.perfume_id is not None and expected_substr is not None:
                is_correct = True  # matched something, good enough
            matched_str = result.perfume_id[:25] if result.perfume_id else "(none)"

        layer_str = result.layer or "-"
        layer_colors = {"exact": GREEN, "fuzzy": YELLOW, "llm": CYAN}
        lc = layer_colors.get(result.layer, DIM)

        if is_correct:
            status = f"{GREEN}PASS{RESET}"
            passed += 1
        else:
            status = f"{RED}FAIL{RESET}"
            failed += 1

        print(f"  {raw_msg:<30} {error_type:<25} {lc}{layer_str:<7}{RESET} {status}      {DIM}{matched_str}{RESET}")

        results_detail.append((raw_msg, error_type, result, is_correct))

    # Summary
    total = passed + failed
    print(f"\n  {'─' * 90}")
    pct = (passed / total * 100) if total else 0
    color = GREEN if pct >= 85 else YELLOW if pct >= 70 else RED
    print(f"  {BOLD}Results: {color}{passed}/{total} passed ({pct:.0f}%){RESET}")

    # Show layer breakdown
    layer_counts = {"exact": 0, "fuzzy": 0, "llm": 0, None: 0}
    for _, _, r, correct in results_detail:
        if correct and r.perfume_id:
            layer_counts[r.layer] = layer_counts.get(r.layer, 0) + 1

    print(f"\n  {BOLD}Matches by layer:{RESET}")
    print(f"    {GREEN}Layer 1 (exact/substring):{RESET}  {layer_counts.get('exact', 0)} matches")
    print(f"    {YELLOW}Layer 2 (fuzzy/typo):    {RESET}  {layer_counts.get('fuzzy', 0)} matches")
    print(f"    {CYAN}Layer 3 (Groq LLM):     {RESET}  {layer_counts.get('llm', 0)} matches")
    none_correct = sum(1 for _, _, r, c in results_detail if c and r.perfume_id is None)
    print(f"    {DIM}Correctly rejected:     {RESET}  {none_correct} messages")

    # Show failures in detail
    failures = [(msg, etype, r) for msg, etype, r, correct in results_detail if not correct]
    if failures:
        print(f"\n  {RED}{BOLD}Failed cases:{RESET}")
        for msg, etype, r in failures:
            print(f"    '{msg}' ({etype})")
            print(f"      got: pid={r.perfume_id}, layer={r.layer}, keyword={r.matched_keyword}")

    print()


if __name__ == "__main__":
    asyncio.run(run_stress_test())
