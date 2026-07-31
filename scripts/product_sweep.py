"""
Per-product detection sweep over the WHOLE catalog.

Every other harness here samples: benchmark_matcher.py generates random
typos, real_queries_test.py replays 20 real messages. Neither can tell you
that product #877 of 1,354 is unreachable — and a product nobody can name is
worth exactly as much as one that isn't in the catalog at all.

This walks every product and asks for it several different ways, the ways a
real customer does:

  exact       the full catalog name, as written on the sheet
  no_brand    the name minus its brand prefix ("rebel", not "Afnan 9PM Rebel")
  no_noise    minus packaging words (EDP / EDT / Parfum / For Men ...)
  last_two    the final two words
  last_word   the final word alone
  acronym     the short name the index itself generated for this product
              (name_index._acronyms) — if the bot claims to answer "cdnim",
              "cdnim" had better come back with the right product
  typo        one letter deleted from the longest word

The bar is different per form, on purpose:

  * The full name must come back FIRST and ALONE. Anything else is the bot
    padding a precise request with products the customer never named.
  * A short form only has to come back AT ALL. "blue" is genuinely several
    products and showing that family is the right answer — but the one the
    customer meant has to be in there.
  * A short form made of nothing but ordinary English ("blue", "leather",
    "for men") is SUPPOSED to come back empty — one such word names no
    product, and answering it would mean guessing. Those are skipped rather
    than scored, so the numbers measure detection instead of rewarding the
    bot for being reckless.
  * Stripping the concentration off a name ("Dior Sauvage EDP" -> "dior
    sauvage") asks a question that genuinely has several right answers, so
    the EDT and the Parfum coming back too is correct, not noise.

Deterministic only: the LLM is disabled so this measures the index itself,
which is what actually has to be right. Groq can reorder candidates but it
never invents one the index failed to find.

Usage:
    python scripts/product_sweep.py                 # full sweep, summary
    python scripts/product_sweep.py --limit 200     # first 200 products
    python scripts/product_sweep.py --form exact    # one form only
    python scripts/product_sweep.py --show 40       # list N failures
    python scripts/product_sweep.py --csv out.csv   # every failure, to file
"""

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.catalog import PERFUMES  # noqa: E402
from app import name_index  # noqa: E402

# Packaging / concentration words that appear on the sheet but that nobody
# says out loud. Stripped for the "no_noise" form. Kept separate from
# name_index._NOISE_TOKENS deliberately — this is about how people TALK,
# and it is used to build queries, not to match them.
# NOT stripped, though they look like they belong here: "elixir",
# "intense", "absolu", "noir". They read as concentration words but the
# catalog uses them to tell real products apart — Lattafa Asad and Lattafa
# Asad Elixir are different perfumes at different prices — so removing them
# would build a query for a product other than the one being tested.
_NOISE = {
    "edp", "edt", "edc", "parfum", "perfume", "extrait",
    "eau", "de", "pour", "homme", "femme",
    "for", "men", "man", "women", "woman", "unisex", "spray",
    "ml", "the", "and",
}

FORMS = ("exact", "no_brand", "no_noise", "last_two", "last_word", "acronym", "typo")


def _family(form: str) -> str:
    """Query forms are reported by family, so the per-product acronym forms
    ("acronym:cdnim") roll up into one row."""
    return form.split(":", 1)[0]


# Short name -> the products it could have been built from. Filled in main()
# once the index is built. name_index only keeps an acronym when it points at
# exactly one product, so this is a 1:1 map in practice — but it is built by
# expansion text, so a duplicate display name lands both rows here rather
# than failing one of them for being the other.
_ACRONYMS_BY_PID: dict[str, list[tuple[str, str]]] = {}


def _words(name: str) -> list[str]:
    return name_index.tokenize(name)


def _strip_brand(name: str, brand: str | None) -> list[str]:
    """The name with its brand prefix removed. Uses the `brand` column when
    the entry has one; falls back to dropping the first word, which is what
    a customer effectively does either way."""
    words = _words(name)
    if brand:
        bwords = _words(brand)
        if len(words) > len(bwords) and words[: len(bwords)] == bwords:
            return words[len(bwords):]
    return words[1:] if len(words) > 1 else words


def _typo(words: list[str]) -> list[str]:
    """Delete one letter from the longest word — the single most common real
    misspelling, and the one that breaks a naive prefix match."""
    out = list(words)
    i = max(range(len(out)), key=lambda j: len(out[j]))
    w = out[i]
    if len(w) >= 5:
        cut = len(w) // 2
        out[i] = w[:cut] + w[cut + 1:]
    return out


def build_queries(pid: str, data: dict) -> dict[str, str]:
    """The set of ways to ask for one product. A form is omitted when it
    would be degenerate for this name (e.g. last_two on a two-word name is
    just the name again) rather than counted as a free pass."""
    name = data["display_name"]
    words = _words(name)
    if not words:
        return {}

    queries: dict[str, str] = {"exact": " ".join(words)}

    no_brand = _strip_brand(name, data.get("brand"))
    if no_brand and no_brand != words:
        queries["no_brand"] = " ".join(no_brand)

    no_noise = [w for w in words if w not in _NOISE]
    if no_noise and no_noise != words:
        queries["no_noise"] = " ".join(no_noise)

    meaningful = no_noise or words
    if len(meaningful) >= 3:
        queries["last_two"] = " ".join(meaningful[-2:])
    if len(meaningful) >= 2:
        queries["last_word"] = meaningful[-1]

    for short, expansion in _ACRONYMS_BY_PID.get(pid, ()):
        queries[f"acronym:{short}"] = short

    typo = _typo(words)
    if typo != words:
        queries["typo"] = " ".join(typo)

    return queries


def _identifiable(query: str) -> bool:
    """Whether this query names anything at all. A query built entirely out
    of ordinary English and packaging words ("blue", "for men", "le parfum")
    identifies no product — silence is the right answer, so scoring it as a
    detection failure would only measure how reckless the bot is."""
    words = name_index.tokenize(query)
    return any(
        w not in name_index.GENERIC_STOPWORDS and len(w) > 2 for w in words
    )


def _bare_name(pid: str) -> str:
    """A display name with concentration words removed, for comparing a
    product against its own EDT/EDP/Parfum siblings."""
    return " ".join(
        w for w in name_index.tokenize(PERFUMES[pid]["display_name"])
        if w not in _NOISE
    )


def evaluate(pid: str, form: str, query: str) -> tuple[str, list[str]]:
    """
    Run one query. Returns (verdict, returned_pids).

    ok        the product came back, at the standard this form is held to
    extra     it came back first, but so did products nobody asked for
              (exact-name forms only — see the module docstring)
    buried    it came back, but not first (short forms: still ok; exact: not)
    miss      it did not come back at all
    """
    family = _family(form)
    if family != "exact" and not _identifiable(query):
        return "skip", []

    results = name_index.search(query)
    pids = [s.perfume_id for s in results]

    if pid not in pids:
        return "miss", pids

    if family not in ("exact", "no_noise", "typo"):
        return "ok", pids

    # An "extra" is only extra if it is a genuinely different product. Two
    # catalog rows can carry the same display name (the sheet has real
    # duplicates), and returning both of those is right, not noise. So are
    # the concentration siblings of a name asked for without one — which is
    # the whole of what the no_noise form does.
    same = {PERFUMES[pid]["display_name"].strip().lower()}
    if family == "no_noise":
        same = {_bare_name(pid)}
        others = [p for p in pids if _bare_name(p) not in same]
    else:
        others = [
            p for p in pids if PERFUMES[p]["display_name"].strip().lower() not in same
        ]

    if pids[0] != pid and (family != "no_noise" or _bare_name(pids[0]) not in same):
        return "buried", pids
    return ("extra", pids) if others else ("ok", pids)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0, help="only the first N products")
    parser.add_argument("--form", choices=FORMS, help="only this query form")
    parser.add_argument("--show", type=int, default=25, help="how many failures to print")
    parser.add_argument("--csv", type=Path, help="write every failure to this file")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    name_index.build_index()

    items = list(PERFUMES.items())
    if args.limit:
        items = items[: args.limit]

    for short, expansion in name_index._acronyms.items():
        for pid, data in PERFUMES.items():
            if " ".join(name_index.tokenize(data["display_name"])).endswith(expansion):
                _ACRONYMS_BY_PID.setdefault(pid, []).append((short, expansion))

    by_form: dict[str, Counter] = {f: Counter() for f in FORMS}
    failures: list[tuple[str, str, str, str, str]] = []
    unreachable: list[str] = []

    print("=" * 96)
    print(f"  PER-PRODUCT SWEEP — {len(items)} products")
    print("=" * 96)

    for n, (pid, data) in enumerate(items, 1):
        queries = build_queries(pid, data)
        if args.form:
            queries = {k: v for k, v in queries.items() if _family(k) == args.form}
        found_any = False
        for form, query in queries.items():
            verdict, pids = evaluate(pid, form, query)
            by_form[_family(form)][verdict] += 1
            if verdict == "skip":
                continue
            if verdict != "miss":
                found_any = True
            if verdict != "ok":
                top = ", ".join(
                    PERFUMES[p]["display_name"] for p in pids[:4] if p in PERFUMES
                )
                failures.append((data["display_name"], form, query, verdict, top))
        if not found_any and queries and by_form["exact"]:
            unreachable.append(data["display_name"])
        if n % 200 == 0:
            print(f"  ...{n}/{len(items)}")

    print()
    print(f"  {'form':<12}{'scored':>9}{'ok':>9}{'extra':>9}{'buried':>9}{'miss':>9}   {'ok %':>7}")
    print("  " + "-" * 74)
    grand = Counter()
    for form in FORMS:
        c = by_form[form]
        total = sum(c.values()) - c["skip"]
        if not total:
            continue
        grand.update(c)
        print(
            f"  {form:<12}{total:>9}{c['ok']:>9}{c['extra']:>9}"
            f"{c['buried']:>9}{c['miss']:>9}   {c['ok'] / total * 100:>6.1f}%"
        )
    total = sum(grand.values()) - grand["skip"]
    print("  " + "-" * 74)
    print(
        f"  {'ALL':<12}{total:>9}{grand['ok']:>9}{grand['extra']:>9}"
        f"{grand['buried']:>9}{grand['miss']:>9}   {grand['ok'] / max(total, 1) * 100:>6.1f}%"
    )
    print()
    print(f"  Queries too generic to score     : {grand['skip']}")
    print(f"  Products unreachable by ANY form : {len(unreachable)}")
    for name in unreachable[: args.show]:
        print(f"      {name}")

    if failures and args.show:
        print()
        print(f"  Failures ({len(failures)} total, showing {min(args.show, len(failures))}):")
        for name, form, query, verdict, top in failures[: args.show]:
            print(f"    [{verdict:<6}] {form:<9} {name}")
            print(f"               asked: {query!r}")
            print(f"               got:   {top or '(nothing)'}")

    if args.csv:
        with args.csv.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["display_name", "form", "query", "verdict", "returned"])
            w.writerows(failures)
        print(f"\n  Wrote {len(failures)} failures to {args.csv}")

    print()
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
