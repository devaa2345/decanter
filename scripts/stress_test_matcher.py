"""
Local stress test for the perfume matching pipeline — no network calls, no
Chat Mitra webhook, no Groq API calls, no credits spent. Pure in-process
calls to Layer 1 (word-boundary exact) and Layer 2 (fuzzy) against every one
of the catalog's ~1200 entries, using each entry's own display name plus a
handful of realistic typo perturbations, to find:

  1. Gaps — a perfume's own full display name matches nothing at all
  2. Typo tolerance — how often a perturbed (typo'd) version still matches
  3. False positives — a query resolves to a completely unrelated perfume
     (zero word overlap with the name that was actually queried)

Deliberately stops at Layer 2: match_perfume() would fall through to Layer 3
(a real Groq API call) for anything Layer 1+2 miss, which costs money and
isn't needed to find catalog/keyword-data bugs — those live in Layer 1/2.

Run:
    python scripts/stress_test_matcher.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.catalog import PERFUMES  # noqa: E402
from app.matcher import _layer1_exact_match, _layer2_fuzzy_match, normalize_message  # noqa: E402


def _words(text: str) -> set:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _perturbations(name: str) -> dict:
    """A handful of realistic typo/query perturbations of a display name."""
    variants = {"plain": name}
    words = name.split()

    if len(words) >= 2:
        variants["no_space"] = words[0] + words[1] + (" " + " ".join(words[2:]) if len(words) > 2 else "")

    bare = name.replace(" ", "")
    if len(bare) > 5:
        mid = len(name) // 2
        variants["missing_letter"] = name[:mid] + name[mid + 1:]

    if len(bare) >= 4:
        mid = len(name) // 2
        if mid + 1 < len(name):
            chars = list(name)
            chars[mid], chars[mid + 1] = chars[mid + 1], chars[mid]
            variants["transposed"] = "".join(chars)

    variants["with_price_suffix"] = f"{name} price"
    variants["with_ml_suffix"] = f"{name} 10ml"

    return variants


def _match(normalized: str):
    r1 = _layer1_exact_match(normalized)
    if r1.perfume_id or r1.ambiguous:
        return r1
    return _layer2_fuzzy_match(normalized)


def main():
    gaps = []
    false_positives = []
    perturbation_stats = {}

    for pid, data in PERFUMES.items():
        display_name = data["display_name"]
        own_words = _words(display_name)

        for variant_name, query in _perturbations(display_name).items():
            normalized = normalize_message(query)
            if not normalized:
                continue

            total, matched = perturbation_stats.get(variant_name, (0, 0))
            perturbation_stats[variant_name] = (total + 1, matched)

            result = _match(normalized)

            if result.ambiguous:
                total, matched = perturbation_stats[variant_name]
                perturbation_stats[variant_name] = (total, matched + 1)
                continue

            if not result.perfume_id:
                if variant_name == "plain":
                    gaps.append((pid, display_name))
                continue

            total, matched = perturbation_stats[variant_name]
            perturbation_stats[variant_name] = (total, matched + 1)

            matched_name = PERFUMES[result.perfume_id]["display_name"]
            matched_words = _words(matched_name)

            if result.perfume_id != pid and not (own_words & matched_words):
                false_positives.append(
                    (pid, display_name, result.perfume_id, matched_name, variant_name, query)
                )

    print(f"Catalog size: {len(PERFUMES)}")
    print()
    print("=== Perturbation match rates (matched OR correctly flagged ambiguous) ===")
    for variant_name, (total, matched) in perturbation_stats.items():
        pct = (matched / total * 100) if total else 0
        print(f"  {variant_name:20} {matched}/{total} ({pct:.1f}%)")

    print()
    print(f"=== Gaps: own full display name matches nothing at all ({len(gaps)}) ===")
    for pid, name in gaps[:50]:
        print(f"  {pid}: {name!r}")
    if len(gaps) > 50:
        print(f"  ... and {len(gaps) - 50} more (truncated)")

    print()
    print(f"=== False positives: zero word overlap with the matched perfume ({len(false_positives)}) ===")
    for pid, name, matched_pid, matched_name, variant, query in false_positives[:50]:
        print(f"  {name!r} [{variant}: {query!r}] -> matched {matched_name!r} ({matched_pid})")
    if len(false_positives) > 50:
        print(f"  ... and {len(false_positives) - 50} more (truncated)")


if __name__ == "__main__":
    main()
