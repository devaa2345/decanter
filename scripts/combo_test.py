"""
Large mixed-perfume order tester — exactness, not just recall.

Customers do not send one name. They send six, in a numbered list, with two
written out in full, three shortened to whatever they call them, and one
misspelled. Every other harness here asks "was the right product found?".
This one asks the harder question the shop actually cares about:

    IS WHAT CAME BACK EXACTLY WHAT WAS ASKED FOR?

A card for a product the customer never named is its own kind of wrong. It
buries the ones they did name, it reads as the bot guessing, and on a
six-item order it turns a quote into a negotiation. So a message scores as
correct only when the returned set EQUALS the intended set — nothing
missing, nothing extra.

HOW THE MESSAGES ARE BUILT
--------------------------
Each product is written the way a different customer would write it:

    full      the catalog name as-is
    short     the name minus its brand ("rebel")
    acronym   the short name the index generated for it ("cdnim")
    typo      one letter dropped from the longest word

...then the mentions are strung together in one of several real layouts
(comma list, numbered list, one per line, "X and Y", Hinglish filler),
with sizes and prices sprinkled in the way real orders carry them.

Only products whose chosen form resolves to THEM ALONE are used. Otherwise
the test would be measuring catalog ambiguity — "9pm" fits four products,
and a message containing it cannot have one right answer — instead of
measuring whether combining names breaks anything. Each candidate form is
verified on its own first; combinations are built only from ones that pass.

Deterministic (the LLM is off) and seeded, so a failure can be reproduced.

Usage:
    python scripts/combo_test.py                    # 300 messages, 2-8 each
    python scripts/combo_test.py --messages 1000
    python scripts/combo_test.py --size 10          # bigger orders
    python scripts/combo_test.py --seed 7 --show 30
"""

import argparse
import random
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.catalog import PERFUMES  # noqa: E402
from app import name_index  # noqa: E402
from scripts.product_sweep import _strip_brand, _typo, _words  # noqa: E402

# Layouts real orders arrive in. {items} is the joined mention list; the
# joiner is chosen per layout so the separator and the wrapper agree.
_LAYOUTS = [
    ("comma list", ", ", "{items}"),
    ("comma list + price ask", ", ", "{items} price please"),
    ("and-joined", " and ", "{items}"),
    ("newline list", "\n", "{items}"),
    ("dashed list", "\n- ", "- {items}"),
    ("hinglish", ", ", "bhai {items} ka rate kya hai"),
    ("greeting + list", ", ", "hi, i want {items}"),
    ("availability", ", ", "do you have {items} in stock"),
]

# Sizes get written next to names constantly. They must not change which
# products come back (they no longer change the card either — every card
# shows the full grid), so they belong in these messages as noise.
_SIZES = ["", "", "", " 3ml", " 5ml", " 10ml"]


def forms_for(pid: str, data: dict, acronyms: dict[str, list[str]]) -> dict[str, str]:
    """Every way this product might be written in an order."""
    name = data["display_name"]
    words = _words(name)
    if not words:
        return {}

    out = {"full": " ".join(words)}

    short = _strip_brand(name, data.get("brand"))
    if short and short != words:
        out["short"] = " ".join(short)

    for acronym in acronyms.get(pid, ()):
        out["acronym"] = acronym

    typo = _typo(words)
    if typo != words:
        out["typo"] = " ".join(typo)

    return out


def unambiguous_forms(acronyms: dict[str, list[str]]) -> dict[str, dict[str, str]]:
    """
    pid -> {form: query} for the forms that resolve to that product ALONE.

    Verified one at a time before any combining happens. A form that pulls
    in a family on its own would make every message containing it
    unanswerable-by-construction, and counting that as a failure of
    combination would be measuring the wrong thing entirely.
    """
    keep: dict[str, dict[str, str]] = {}
    for pid, data in PERFUMES.items():
        good = {}
        for form, query in forms_for(pid, data, acronyms).items():
            results = name_index.search(query)
            if [s.perfume_id for s in results] == [pid]:
                good[form] = query
        if good:
            keep[pid] = good
    return keep


def build_message(
    rng: random.Random, picks: list[tuple[str, str, str]]
) -> tuple[str, str]:
    """(layout name, message text) for these (pid, form, query) mentions."""
    label, joiner, template = rng.choice(_LAYOUTS)
    mentions = [q + rng.choice(_SIZES) for _pid, _form, q in picks]
    if label == "numbered":
        mentions = [f"{i}. {m}" for i, m in enumerate(mentions, 1)]
    return label, template.format(items=joiner.join(mentions))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--messages", type=int, default=300)
    parser.add_argument("--size", type=int, default=8, help="max products per message")
    parser.add_argument("--min-size", type=int, default=2)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--show", type=int, default=15)
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    name_index.build_index()

    acronyms: dict[str, list[str]] = {}
    for short, expansion in name_index._acronyms.items():
        for pid, data in PERFUMES.items():
            if " ".join(name_index.tokenize(data["display_name"])).endswith(expansion):
                acronyms.setdefault(pid, []).append(short)

    print("=" * 96)
    print("  COMPLEX COMBINATION TEST — building the pool of unambiguous forms...")
    pool = unambiguous_forms(acronyms)
    by_form = Counter(f for forms in pool.values() for f in forms)
    print(f"  {len(pool)} of {len(PERFUMES)} products usable   {dict(by_form)}")
    print("=" * 96)

    rng = random.Random(args.seed)
    pids = sorted(pool)

    exact = 0
    missing_total = extra_total = 0
    by_count: dict[int, Counter] = {}
    failures = []

    for _ in range(args.messages):
        n = rng.randint(args.min_size, args.size)
        chosen = rng.sample(pids, min(n, len(pids)))
        picks = [(pid, *rng.choice(sorted(pool[pid].items()))) for pid in chosen]
        label, message = build_message(rng, picks)

        want = {pid for pid, _f, _q in picks}
        got = {s.perfume_id for s in name_index.search(message, limit=60)}

        missing = want - got
        extra = got - want
        missing_total += len(missing)
        extra_total += len(extra)

        bucket = by_count.setdefault(len(chosen), Counter())
        bucket["messages"] += 1
        if not missing and not extra:
            exact += 1
            bucket["exact"] += 1
        else:
            failures.append((label, message, missing, extra, picks))

    print()
    print(f"  {'products':>9}{'messages':>10}{'exact':>8}{'exact %':>10}")
    print("  " + "-" * 38)
    for count in sorted(by_count):
        c = by_count[count]
        print(
            f"  {count:>9}{c['messages']:>10}{c['exact']:>8}"
            f"{c['exact'] / c['messages'] * 100:>9.1f}%"
        )
    print("  " + "-" * 38)
    print(
        f"  {'ALL':>9}{args.messages:>10}{exact:>8}"
        f"{exact / max(args.messages, 1) * 100:>9.1f}%"
    )
    print()
    print(f"  Products missed   : {missing_total}")
    print(f"  Products invented : {extra_total}")

    if failures and args.show:
        print()
        print(f"  Failures ({len(failures)} total, showing {min(args.show, len(failures))}):")
        for label, message, missing, extra, picks in failures[: args.show]:
            print(f"\n    [{label}] {message!r}")
            print(f"      asked for: {[PERFUMES[p]['display_name'] for p, _f, _q in picks]}")
            if missing:
                print(f"      MISSING:   {[PERFUMES[p]['display_name'] for p in missing]}")
            if extra:
                print(f"      EXTRA:     {[PERFUMES[p]['display_name'] for p in extra]}")

    print()
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
