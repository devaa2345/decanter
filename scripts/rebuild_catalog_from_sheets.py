"""
Rebuild the catalog from the source Decant Sheet PDFs — into a NEW file.

scripts/audit_catalog_against_sheets.py showed that catalog_data.json does
not match the sheets: 110 entries have every price shifted a size tier, 92
entries are redundant duplicates (35 of them disagreeing on price), and
~336 products on the sheets are absent entirely. The cause was reading the
price cells positionally — first number found becomes the 3ml price — so a
blank size cell slides every later price down one tier.

This regenerates the whole catalog by column position instead, using the
parser the audit validated against the raw PDFs.

WHAT WINS WHERE THE SHEETS DISAGREE
-----------------------------------
Sheets 1 and 2 overlap on 66 products and contradict each other on 38 of
them; they are two snapshots, not two tabs. Per the owner's decision, sheet
1 is authoritative and sheet 2 only contributes the products sheet 1 does
not list. Sheet 3 is the full-bottle list and contributes "<n>ml_full"
prices onto products already present, matched by name.

WHAT IT WILL NOT DO
-------------------
It never touches app/catalog_data.json. Output goes to a separate file and
a diff, so every price change is reviewed before anything reaches a
customer. Entries absent from all three sheets are CARRIED OVER rather than
dropped — the hand-added Dior Fahrenheit EDT is one of these — and listed
in the report so they can be confirmed or removed deliberately.

Run:
    python scripts/rebuild_catalog_from_sheets.py
    python scripts/rebuild_catalog_from_sheets.py --out app/catalog_data.rebuilt.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging

logging.disable(logging.CRITICAL)

from rapidfuzz import fuzz, process  # noqa: E402

import audit_catalog_against_sheets as audit  # noqa: E402
from app.catalog import PERFUMES  # noqa: E402
from app.catalog_upload import (  # noqa: E402
    ParsedRow,
    _corpus_stopwords,
    _make_unique_id,
    generate_keywords,
)
from app.matcher import normalize_message  # noqa: E402

DEFAULT_OUT = "app/catalog_data.rebuilt.json"

# How close a full-bottle row's name must be to a decant product's name to be
# treated as the same product. The full-bottle sheet is typed in capitals
# with a concentration suffix ("AFNAN HISTORIC DORIA EDP" for "Afnan
# Historic Doria"), so exact matching finds almost nothing.
_FULL_BOTTLE_MATCH = 90

# Concentration and packaging words stripped from a full-bottle name before
# matching. Without this "AFNAN 9PM REBEL EDP" scores 88 against "Afnan 9PM
# Rebel" and misses the bar — which silently dropped the 100ml price the
# current catalog already had, turning a rebuild into a regression.
# Kept deliberately short. "Elixir", "Pour Homme" and "Noir" look like
# packaging words but are part of real product names, and stripping them
# would merge genuinely different products.
_CONCENTRATION_WORDS = {"edp", "edt", "edc", "parfum", "extrait", "cologne", "spray"}


def _match_key(name: str) -> str:
    """
    A name reduced to what identifies the product: concentration words
    dropped, then all spacing removed.

    Applied to BOTH sides — stripping only the full-bottle side would make
    the two names less alike, not more. Spacing is squashed because the two
    sheets disagree about it constantly ("AFNAN 9AM DIVE EDP" against "Afnan
    9 AM Dive"), and every such disagreement was costing a real full-bottle
    price that the current catalog already had.
    """
    words = [w for w in normalize_message(name).split() if w not in _CONCENTRATION_WORDS]
    return "".join(words)


def _dedupe(rows: list[audit.SheetRow], report: list[str]) -> dict[str, audit.SheetRow]:
    """Collapse repeats within one sheet, keeping the first occurrence."""
    out: dict[str, audit.SheetRow] = {}
    for row in rows:
        key = normalize_message(row.display_name)
        if key in out:
            if out[key].prices != row.prices:
                report.append(
                    f"{row.display_name}: listed twice on {row.source} with different "
                    f"prices (kept p{out[key].page} {out[key].prices}, "
                    f"ignored p{row.page} {row.prices})"
                )
            continue
        out[key] = row
    return out


def collect_rows() -> tuple[dict[str, audit.SheetRow], list[str]]:
    notes: list[str] = []
    parse_report = audit.Report()

    sheet1 = _dedupe(audit.parse_decant_sheet(audit.DECANT_SHEETS[0], parse_report), notes)
    sheet2 = _dedupe(audit.parse_decant_sheet(audit.DECANT_SHEETS[1], parse_report), notes)
    full = audit.parse_full_bottle_sheet(audit.FULL_BOTTLE_SHEET, parse_report)
    notes.extend(parse_report.parse_skips)

    merged = dict(sheet1)
    added_from_2 = 0
    for key, row in sheet2.items():
        if key not in merged:
            merged[key] = row
            added_from_2 += 1
    notes.append(f"sheet 1 supplied {len(sheet1)} products")
    notes.append(f"sheet 2 supplied {added_from_2} products sheet 1 does not list")
    notes.append(
        f"sheet 2 was overruled on {len(sheet2) - added_from_2} products it shares with sheet 1"
    )

    # Full-bottle prices merge onto the product they belong to, compared on
    # concentration-stripped names (see _match_key).
    match_keys = {_match_key(row.display_name): key for key, row in merged.items()}
    candidates = list(match_keys)
    attached = orphan = 0
    for row in full:
        key = normalize_message(row.display_name)
        target = merged.get(key) and key
        if target is None:
            probe = _match_key(row.display_name)
            hit = match_keys.get(probe)
            if hit is None:
                match = process.extractOne(probe, candidates, scorer=fuzz.ratio)
                hit = match_keys[match[0]] if match and match[1] >= _FULL_BOTTLE_MATCH else None
            target = hit
        if target is None:
            orphan += 1
            continue
        merged[target].prices.update(row.prices)
        attached += 1
    notes.append(
        f"full-bottle sheet: {attached} prices attached to a decant product, "
        f"{orphan} full-bottle-only products not carried into the catalog"
    )

    return merged, notes


def build(rows: dict[str, audit.SheetRow]) -> dict[str, dict]:
    parsed = [
        ParsedRow(brand=r.brand, name=r.name, clone_of=r.clone_of or None, prices=r.prices)
        for r in rows.values()
    ]
    corpus_stopwords = _corpus_stopwords(parsed)

    # Reuse the existing id wherever the product already exists, so the
    # analytics history in message_events keeps pointing at the same thing.
    existing_by_name = {
        normalize_message(v["display_name"]): pid for pid, v in PERFUMES.items()
    }

    catalog: dict[str, dict] = {}
    used: set[str] = set()
    for row in rows.values():
        display_name = f"{row.brand} {row.name}".strip()
        key = normalize_message(display_name)
        pid = existing_by_name.get(key)
        if pid is None or pid in used:
            pid = _make_unique_id(row.brand, row.name, used | set(catalog))
        used.add(pid)

        catalog[pid] = {
            "keywords": generate_keywords(
                row.brand, row.name, row.clone_of or None, corpus_stopwords
            ),
            "display_name": display_name,
            "brand": row.brand or None,
            "prices": row.prices,
            "clone_of": row.clone_of or None,
        }

    return catalog


# A leftover entry this similar to a rebuilt product is the same product
# under a mangled name, not a product the sheets forgot.
_SUPERSEDED_MATCH = 88


def preserve_full_bottle_prices(catalog: dict[str, dict]) -> int:
    """
    Keep any "<n>ml_full" price the current catalog has that the rebuild
    did not find, for the same product.

    The full-bottle sheet names products differently enough ("AFNAN MODEST
    UNE POUR HOMME (EDP )" for "Afnan Modest Une") that name matching
    recovers only some of them, and 142 full-bottle prices already in the
    catalog would otherwise vanish. A rebuild is allowed to correct data; it
    is not allowed to lose it. Decant prices are NOT treated this way — the
    sheets are authoritative there, and the whole point is to overwrite them.
    """
    by_name = {normalize_message(v["display_name"]): v for v in catalog.values()}
    restored = 0
    for data in PERFUMES.values():
        target = by_name.get(normalize_message(data["display_name"]))
        if target is None:
            continue
        for size, price in data.get("prices", {}).items():
            if "full" in size and size not in target["prices"]:
                target["prices"][size] = price
                restored += 1
    return restored


def carry_over(catalog: dict[str, dict]) -> tuple[list[str], list[tuple[str, str]]]:
    """
    Decide what to do with catalog entries that appear on none of the sheets.

    Two very different things end up here and they must not share a fate:

      * Genuinely extra products — the hand-added Dior Fahrenheit EDT, and
        anything else added since the sheets were exported. These are kept;
        dropping them silently is the one thing this must never do.
      * Wreckage from the original import — entries whose display name
        merged the product with its clone ("Delicious Bouquet D&G Devotion")
        or lost the name entirely ("Armaf Armaf"). The real product is
        already in the rebuild under its correct name, so keeping these
        would preserve the corruption and re-create the duplicates.

    Told apart by name similarity to the rebuilt catalog.
    """
    present = {normalize_message(v["display_name"]): pid for pid, v in catalog.items()}
    names = list(present)

    kept: list[str] = []
    superseded: list[tuple[str, str]] = []

    for pid, data in PERFUMES.items():
        key = normalize_message(data["display_name"])
        if key in present or pid in catalog:
            continue

        match = process.extractOne(key, names, scorer=fuzz.token_set_ratio)
        if match and match[1] >= _SUPERSEDED_MATCH:
            superseded.append((data["display_name"], catalog[present[match[0]]]["display_name"]))
            continue

        catalog[pid] = data
        kept.append(pid)

    return kept, superseded


def diff(old: dict[str, dict], new: dict[str, dict]) -> dict:
    old_by_name = {normalize_message(v["display_name"]): (p, v) for p, v in old.items()}
    new_by_name = {normalize_message(v["display_name"]): (p, v) for p, v in new.items()}

    added = [new_by_name[k] for k in new_by_name.keys() - old_by_name.keys()]
    removed = [old_by_name[k] for k in old_by_name.keys() - new_by_name.keys()]

    changed = []
    for key in old_by_name.keys() & new_by_name.keys():
        _, o = old_by_name[key]
        _, n = new_by_name[key]
        if o.get("prices") != n.get("prices"):
            changed.append((n["display_name"], o.get("prices", {}), n.get("prices", {})))

    return {"added": added, "removed": removed, "changed": changed}


def _fmt(prices: dict) -> str:
    order = ["3ml", "5ml", "8ml", "10ml", "20ml", "30ml"]
    parts = [f"{s} {prices[s]}" for s in order if s in prices]
    parts += [f"{s} {v}" for s, v in sorted(prices.items()) if "full" in s]
    return "  ".join(parts) or "(none)"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--show", type=int, default=40, help="how many changes to print")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    rows, notes = collect_rows()
    catalog = build(rows)
    restored = preserve_full_bottle_prices(catalog)
    kept, superseded = carry_over(catalog)
    changes = diff(PERFUMES, catalog)

    print()
    print("=" * 92)
    print("  CATALOG REBUILD (preview — app/catalog_data.json is untouched)")
    print("=" * 92)
    print()
    for note in notes:
        print(f"  - {note}")
    print(f"  - {restored} existing full-bottle prices preserved (the sheet names them differently)")

    print()
    print(f"  Current catalog : {len(PERFUMES)} entries")
    print(f"  Rebuilt catalog : {len(catalog)} entries")
    print(f"    products from the sheets      : {len(catalog) - len(kept)}")
    print(f"    carried over (on no sheet)    : {len(kept)}")
    print()
    print(f"  NEW products added   : {len(changes['added'])}")
    print(f"  PRICE CHANGES        : {len(changes['changed'])}")
    print(f"  Entries dropped      : {len(changes['removed'])}")
    print(f"  Superseded wreckage  : {len(superseded)}")
    print("  (superseded = old entries whose real product is in the rebuild under")
    print("   its correct name — merged name+clone, or a lost name like 'Armaf Armaf')")

    if changes["changed"]:
        print()
        print(f"  --- Price changes (showing {min(len(changes['changed']), args.show)}) ---")
        for name, old_p, new_p in sorted(changes["changed"])[: args.show]:
            print(f"    {name}")
            print(f"        was: {_fmt(old_p)}")
            print(f"        now: {_fmt(new_p)}")

    if changes["added"]:
        print()
        print(f"  --- New products (showing {min(len(changes['added']), args.show)}) ---")
        for _pid, data in sorted(changes["added"], key=lambda kv: kv[1]["display_name"])[: args.show]:
            print(f"    {data['display_name']:<52} {_fmt(data['prices'])}")

    if changes["removed"]:
        print()
        print(f"  --- Dropped (showing {min(len(changes['removed']), args.show)}) ---")
        for _pid, data in sorted(changes["removed"], key=lambda kv: kv[1]["display_name"])[: args.show]:
            print(f"    {data['display_name']:<52} {_fmt(data['prices'])}")

    if superseded:
        print()
        print(f"  --- Superseded by a correctly-named entry (showing {min(len(superseded), args.show)} of {len(superseded)}) ---")
        for old_name, new_name in sorted(superseded)[: args.show]:
            print(f"    {old_name[:46]:46} -> {new_name}")

    if kept:
        print()
        print(f"  --- Kept although on no sheet ({len(kept)}) ---")
        print("  Confirm these are still sold, or remove them from the sheet-rebuilt file.")
        for pid in sorted(kept)[: args.show]:
            print(f"    {PERFUMES[pid]['display_name']:<52} {_fmt(PERFUMES[pid]['prices'])}")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2)

    print()
    print(f"  Wrote {args.out}")
    print("  Nothing is live yet. To adopt it:")
    print(f"      cp {args.out} app/catalog_data.json")
    print()


if __name__ == "__main__":
    main()
