"""
Reconcile app/catalog_data.json against the source Decant Sheet PDFs.

Answers three questions the bot cannot answer for itself:

  1. Is every product on the sheets actually in the catalog? A product the
     catalog is missing is a customer who gets told "we don't have that".
  2. Does every price in the catalog match the sheet? A wrong price is worse
     than a missing one — it is quoted confidently and has to be honoured.
  3. Does the catalog contain products the sheets no longer list?

WHY POSITIONAL PARSING
----------------------
These PDFs are printed spreadsheets, so plain text extraction interleaves
columns and silently glues a price onto the wrong product. Every word is
read with its coordinates instead: rows are anchored on the price cells and
columns are derived from the header row, so a name that wraps across three
lines still lands in one record with its own prices.

THE THREE SHEETS ARE NOT THE SAME SHAPE
---------------------------------------
Sheets 1 and 2 are decant sheets — Brand | Fragrance Name | Clone Of |
3ml | 5ml | 8ml | 10ml | 20ml | 30ml | BNIB. Sheet 3 is the full-bottle
list — Brand | Perfume Name | Quantity (ML) | Price — which maps onto the
"<n>ml_full" price keys rather than the decant tiers.

Matching back to the catalog is by normalized display name, with a fuzzy
second pass so a near-miss is reported as "probably this, spelled
differently" rather than as both a missing product and an orphan entry.

Run:
    python scripts/audit_catalog_against_sheets.py
    python scripts/audit_catalog_against_sheets.py --json audit.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging

logging.disable(logging.CRITICAL)

from rapidfuzz import fuzz, process  # noqa: E402

from app.catalog import PERFUMES  # noqa: E402
from app.matcher import normalize_message  # noqa: E402

DECANT_SHEETS = [
    "Sovereign Scents - Decant Sheet - Google Sheets.pdf",
    "Sovereign Scents - Decant Sheet - Google Sheets2.pdf",
]
FULL_BOTTLE_SHEET = "Sovereign Scents - Decant Sheet - Google sheets3.pdf"

# Vertical tolerance when gathering a row's words. Rows sit ~6pt apart on the
# decant sheets and a wrapped cell spills ±2pt, so this has to be wide enough
# to catch the wrap and narrow enough not to steal the neighbouring row.
_ROW_BAND = 3.2

_PRICE_RE = re.compile(r"^\d[\d,]*$")


@dataclass
class SheetRow:
    brand: str
    name: str
    clone_of: str
    prices: dict[str, int]
    source: str
    page: int

    @property
    def display_name(self) -> str:
        return f"{self.brand} {self.name}".strip()


@dataclass
class Report:
    sheet_rows: list[SheetRow] = field(default_factory=list)
    missing: list[SheetRow] = field(default_factory=list)
    near_miss: list[tuple[SheetRow, str, float]] = field(default_factory=list)
    price_diffs: list[tuple[SheetRow, str, dict]] = field(default_factory=list)
    unmatched_catalog: list[str] = field(default_factory=list)
    parse_skips: list[str] = field(default_factory=list)


def _words(page) -> list[tuple[float, float, str]]:
    """(x, y, text) for every word on the page, ignoring empties."""
    return [
        (w[0], w[1], w[4].strip())
        for w in page.get_text("words")
        if w[4] and w[4].strip()
    ]


def _find_header(words, labels: list[str]) -> dict[str, float] | None:
    """Locate the header row and return each label's x position."""
    by_y: dict[float, list[tuple[float, str]]] = defaultdict(list)
    for x, y, t in words:
        by_y[round(y)].append((x, t.lower()))

    for y in sorted(by_y):
        texts = {t for _, t in by_y[y]}
        if labels[0].lower() in texts:
            found = {}
            # Header labels can sit on two adjacent baselines (the decant
            # sheets split "Brand / Fragrance Name / Clone Of" from the size
            # columns), so scan a small band rather than one exact line.
            for yy in range(int(y) - 1, int(y) + 4):
                for x, t in by_y.get(yy, []):
                    for label in labels:
                        if t == label.lower() and label not in found:
                            found[label] = x
            if len(found) >= len(labels) - 1:
                return found
    return None


def _column_of(x: float, bounds: list[tuple[str, float, float]]) -> str | None:
    for name, lo, hi in bounds:
        if lo <= x < hi:
            return name
    return None


# Minimum width of an empty vertical strip for it to count as a column
# separator rather than ordinary inter-word spacing. It has to be per-sheet
# because the two layouts have very different geometry: the decant sheets
# are dense, with real columns only 8-17pt apart, while the full-bottle
# sheet is sparse, with ~100pt between columns and its prices right-aligned
# 9pt clear of their own header. One threshold cannot serve both — 6pt on
# the full-bottle sheet severed every price from the "Price" column and
# parsed zero rows.
_DECANT_COLUMN_GAP = 6
_FULL_BOTTLE_COLUMN_GAP = 30


def _detect_columns(
    doc, header: dict[str, float], min_gap: float
) -> list[tuple[str, float, float]]:
    """
    Work out where each column actually starts and ends, from the layout
    rather than from the header text.

    Header positions alone are not enough and produced real corruption:
    "Clone Of" is printed further right than the values beneath it, so
    boundaries taken from the label put "Creed" (a clone name) inside the
    fragrance column and parsed "Afnan 9PM Rebel / Creed Aventus Absolu" as
    a product called "9PM Rebel Creed". Every price on that row then
    belonged to the wrong thing.

    Columns of a printed spreadsheet are separated by strips no word ever
    occupies. Those strips are found across the whole document, cut at their
    midpoints, and each resulting band is named by whichever header label
    falls inside it. Bands no label falls into are merged leftwards — they
    are wrapped continuations, not columns of their own.
    """
    occupied: set[int] = set()
    for page in doc:
        for x, _y, _t in _words(page):
            occupied.add(int(x))

    if not occupied:
        return []

    ordered = sorted(occupied)
    cuts = [
        (a + b) / 2
        for a, b in zip(ordered, ordered[1:])
        if b - a >= min_gap
    ]

    # Guarantee that every header label ends up in a band of its own. Data
    # gaps alone are not enough: on the full-bottle sheet a few long perfume
    # names reach far enough right that no page-wide gap separates them from
    # the Quantity column, which silently merged the two and dropped every
    # row for want of a quantity. Where two labels share a band, the widest
    # empty strip between them is promoted to a boundary.
    label_xs = sorted(header.values())
    for left, right in zip(label_xs, label_xs[1:]):
        if any(left < c < right for c in cuts):
            continue
        between = [x for x in ordered if left < x < right]
        widest, best = 0.0, (left + right) / 2
        for a, b in zip([left, *between], [*between, right]):
            if b - a > widest:
                widest, best = b - a, (a + b) / 2
        cuts.append(best)

    cuts.sort()
    edges = [ordered[0] - 2, *cuts, ordered[-1] + 8]

    bands: list[tuple[str | None, float, float]] = []
    for lo, hi in zip(edges, edges[1:]):
        label = next((n for n, x in header.items() if lo <= x < hi), None)
        bands.append((label, lo, hi))

    # Anything beyond the last named column is margin commentary
    # ("Designer prices have been slashed to the MAX!"), not a column — it
    # must be dropped rather than merged into the rightmost price, which is
    # how sheet notes ended up parsed as 30ml prices.
    last_named = max(
        (i for i, (label, _, _) in enumerate(bands) if label is not None), default=-1
    )

    merged: list[tuple[str, float, float]] = []
    for label, lo, hi in bands[: last_named + 1]:
        if label is None:
            # A nameless interior band is a wrapped continuation of the
            # column on its left; before the first column it is margin noise.
            if merged:
                name, plo, _ = merged[-1]
                merged[-1] = (name, plo, hi)
            continue
        merged.append((label, lo, hi))

    return merged


def _to_price(text: str) -> int | None:
    text = text.replace(",", "").strip()
    return int(text) if _PRICE_RE.match(text) and text.isdigit() else None


def parse_decant_sheet(path: str, report: Report) -> list[SheetRow]:
    import fitz

    size_labels = ["3ml", "5ml", "8ml", "10ml", "20ml", "30ml"]
    rows: list[SheetRow] = []
    doc = fitz.open(path)

    header = None
    for page in doc:
        header = _find_header(
            _words(page), ["Brand", "Fragrance", "Clone"] + size_labels + ["BNIB"]
        )
        if header:
            break
    if not header:
        report.parse_skips.append(f"{path}: could not locate a header row")
        doc.close()
        return rows

    bounds = _detect_columns(doc, header, _DECANT_COLUMN_GAP)
    if not bounds:
        report.parse_skips.append(f"{path}: could not detect columns")
        doc.close()
        return rows

    # Everything past the last named column is a sheet note in the margin
    # ("Designer prices have been slashed to the MAX!", "PLEASE CHECK"), not
    # data — _column_of drops it, but bounding the words first keeps the
    # row anchoring from being dragged around by it.
    right_edge = max(hi for _, _, hi in bounds)
    first_price_x = min(lo for label, lo, _ in bounds if label in size_labels)

    for pno, page in enumerate(doc, 1):
        words = [(x, y, t) for x, y, t in _words(page) if x < right_edge]

        # Anchor rows on price cells: every real product row has at least one.
        anchors = sorted(
            {round(y, 1) for x, y, t in words if x >= first_price_x and _to_price(t)}
        )

        for anchor in anchors:
            cells: dict[str, list[tuple[float, float, str]]] = defaultdict(list)
            for x, y, t in words:
                if abs(y - anchor) > _ROW_BAND:
                    continue
                col = _column_of(x, bounds)
                if col:
                    cells[col].append((y, x, t))

            def text_of(col: str) -> str:
                # Sorted by line first, then left-to-right. A cell wrapping
                # over three lines ("Chanel Allure Homme Edition / Blanche")
                # reads as nonsense if sorted by x alone.
                return " ".join(t for _, _, t in sorted(cells.get(col, [])))

            prices: dict[str, int] = {}
            for size in size_labels:
                value = _to_price(text_of(size))
                if value:
                    prices[size] = value

            name = text_of("Fragrance")
            if not name or not prices:
                continue

            rows.append(
                SheetRow(
                    brand=text_of("Brand"),
                    name=name,
                    clone_of=text_of("Clone"),
                    prices=prices,
                    source=os.path.basename(path),
                    page=pno,
                )
            )

    doc.close()
    return rows


def parse_full_bottle_sheet(path: str, report: Report) -> list[SheetRow]:
    import fitz

    rows: list[SheetRow] = []
    doc = fitz.open(path)

    header = None
    for page in doc:
        header = _find_header(_words(page), ["Brand", "Perfume", "Quantity", "Price"])
        if header:
            break
    if not header:
        report.parse_skips.append(f"{path}: could not locate a header row")
        doc.close()
        return rows

    bounds = _detect_columns(doc, header, _FULL_BOTTLE_COLUMN_GAP)
    if not bounds:
        report.parse_skips.append(f"{path}: could not detect columns")
        doc.close()
        return rows

    for pno, page in enumerate(doc, 1):
        words = _words(page)
        anchors = sorted(
            {round(y, 1) for x, y, t in words if x >= min(lo for label, lo, _ in bounds if label == "Price") and _to_price(t)}
        )

        for anchor in anchors:
            cells: dict[str, list[tuple[float, float, str]]] = defaultdict(list)
            for x, y, t in words:
                if abs(y - anchor) > 6:
                    continue
                col = _column_of(x, bounds)
                if col:
                    cells[col].append((y, x, t))

            def text_of(col: str) -> str:
                return " ".join(t for _, _, t in sorted(cells.get(col, [])))

            price = _to_price(text_of("Price"))
            qty = _to_price(text_of("Quantity"))
            name = text_of("Perfume")
            if not name or not price or not qty:
                continue

            rows.append(
                SheetRow(
                    brand=text_of("Brand"),
                    name=name,
                    clone_of="",
                    prices={f"{qty}ml_full": price},
                    source=os.path.basename(path),
                    page=pno,
                )
            )

    doc.close()
    return rows


# --- Reconciliation ---------------------------------------------------------

def _catalog_groups() -> dict[str, list[str]]:
    """
    Normalized display name -> every catalog id carrying it.

    A list, not a single id, because the catalog genuinely contains repeats
    of the same product — and they do not always agree on price, which is
    the single most important thing this audit surfaces.
    """
    groups: dict[str, list[str]] = defaultdict(list)
    for pid, data in PERFUMES.items():
        groups[normalize_message(data["display_name"])].append(pid)
    return groups


def find_duplicate_conflicts() -> list[tuple[str, list[str]]]:
    """
    Products the catalog lists more than once with DIFFERENT prices.

    Which price a customer is quoted then depends on which copy the matcher
    happens to rank first, so the same question can be answered two
    different ways on two different days.
    """
    conflicts = []
    for _name, pids in sorted(_catalog_groups().items()):
        if len(pids) < 2:
            continue
        distinct = {json.dumps(PERFUMES[p].get("prices", {}), sort_keys=True) for p in pids}
        if len(distinct) > 1:
            conflicts.append((PERFUMES[pids[0]]["display_name"], pids))
    return conflicts


def reconcile(rows: list[SheetRow], report: Report) -> None:
    groups = _catalog_groups()
    known_names = list(groups)
    seen_pids: set[str] = set()

    for row in rows:
        key = normalize_message(row.display_name)
        pids = groups.get(key)

        if not pids:
            match = process.extractOne(key, known_names, scorer=fuzz.token_sort_ratio)
            if match and match[1] >= 90:
                pids = groups[match[0]]
                report.near_miss.append((row, PERFUMES[pids[0]]["display_name"], match[1]))
            else:
                report.missing.append(row)
                continue

        seen_pids.update(pids)

        # Every entry sharing the name is checked, not just one — a duplicate
        # carrying wrong prices is exactly what must not slip through.
        for pid in pids:
            catalog_prices = PERFUMES[pid].get("prices", {})
            diff = {
                size: {"sheet": amount, "catalog": catalog_prices.get(size)}
                for size, amount in row.prices.items()
                if catalog_prices.get(size) != amount
            }
            if diff:
                report.price_diffs.append((row, pid, diff))

    report.unmatched_catalog = sorted(set(PERFUMES) - seen_pids)


def _money(value) -> str:
    return "—" if value is None else f"₹{value}"


def _size_order(size: str) -> int:
    digits = "".join(c for c in size if c.isdigit())
    return int(digits) if digits else 0


def print_report(report: Report) -> None:
    rows = report.sheet_rows
    decant_rows = [r for r in rows if not any("full" in k for k in r.prices)]
    full_rows = [r for r in rows if any("full" in k for k in r.prices)]
    decant_ids = {id(r) for r in decant_rows}

    print()
    print("=" * 92)
    print("  CATALOG vs SOURCE SHEETS")
    print("=" * 92)

    by_source: dict[str, int] = defaultdict(int)
    for r in rows:
        by_source[r.source] += 1
    print()
    print(f"  Rows read from the sheets: {len(rows)}")
    for src, n in sorted(by_source.items()):
        print(f"    {n:>5}  {src}")
    print(f"  Catalog entries: {len(PERFUMES)}")
    for note in report.parse_skips:
        print(f"  ! {note}")

    groups = _catalog_groups()
    dupes = {n: p for n, p in groups.items() if len(p) > 1}
    conflicts = find_duplicate_conflicts()

    print()
    print("  --- 1. Duplicate catalog entries -------------------------------")
    print(f"  Products listed more than once      : {len(dupes)}")
    print(f"  ...of those, with CONFLICTING prices: {len(conflicts)}")
    print(f"  Redundant entries in total          : {sum(len(p) - 1 for p in dupes.values())}")
    if conflicts:
        print()
        print("  Which price a customer gets depends on which copy ranks first:")
        for name, pids in conflicts[:40]:
            print(f"    {name}")
            for pid in pids:
                print(f"        {pid:<50} {PERFUMES[pid].get('prices', {})}")

    missing_decant = [r for r in report.missing if id(r) in decant_ids]
    missing_full = [r for r in report.missing if id(r) not in decant_ids]
    diffs_decant = [(r, p, d) for r, p, d in report.price_diffs if id(r) in decant_ids]
    diffs_full = [(r, p, d) for r, p, d in report.price_diffs if id(r) not in decant_ids]
    clean_decant = len(decant_rows) - len(missing_decant) - len({id(r) for r, _, _ in diffs_decant})

    print()
    print("  --- 2. Decant sheets vs catalog --------------------------------")
    print(f"  Decant rows on the sheets      : {len(decant_rows)}")
    print(f"  Matched, every price agrees    : {clean_decant}")
    print(f"  PRICE MISMATCH                 : {len(diffs_decant)}")
    print(f"  ON SHEET, MISSING FROM CATALOG : {len(missing_decant)}")

    if diffs_decant:
        print()
        print(f"  Price mismatches (showing {min(len(diffs_decant), 50)} of {len(diffs_decant)}):")
        for row, pid, diff in diffs_decant[:50]:
            print(f"    {row.display_name}   [{row.source[-12:-4]} p{row.page}]  -> {pid}")
            for size, v in sorted(diff.items(), key=lambda kv: _size_order(kv[0])):
                print(f"        {size:>10}  sheet ₹{v['sheet']}   catalog {_money(v['catalog'])}")

    if missing_decant:
        print()
        print(f"  On the decant sheet but not in the catalog ({len(missing_decant)}):")
        for row in missing_decant[:60]:
            sizes = ", ".join(
                f"{k} ₹{v}" for k, v in sorted(row.prices.items(), key=lambda kv: _size_order(kv[0]))
            )
            print(f"    {row.display_name}   [{row.source[-12:-4]} p{row.page}]")
            print(f"        {sizes}")

    print()
    print("  --- 3. Full-bottle sheet vs catalog ----------------------------")
    print("  These are BNIB bottles, a separate list from the decants. A row")
    print("  here with no catalog match usually just means that product is")
    print("  not sold as a decant, which is not an error.")
    print(f"  Full-bottle rows on the sheet  : {len(full_rows)}")
    print(f"  Matched to a catalog entry     : {len(full_rows) - len(missing_full)}")
    print(f"  Full-bottle PRICE MISMATCH     : {len(diffs_full)}")
    print(f"  No decant equivalent           : {len(missing_full)}")
    if diffs_full:
        print()
        print(f"  Full-bottle price mismatches (showing {min(len(diffs_full), 30)} of {len(diffs_full)}):")
        for row, _pid, diff in diffs_full[:30]:
            for size, v in sorted(diff.items()):
                print(
                    f"    {row.display_name[:50]:<50} {size:>11}  "
                    f"sheet ₹{v['sheet']}  catalog {_money(v['catalog'])}"
                )

    print()
    print("  --- 4. Other ---------------------------------------------------")
    print(f"  Name spelled differently on sheet vs catalog: {len(report.near_miss)}")
    print(f"  Catalog entries on no sheet at all         : {len(report.unmatched_catalog)}")
    if report.near_miss:
        print()
        print("  Spelling differences (showing 25):")
        for row, catalog_name, score in report.near_miss[:25]:
            print(f"    sheet: {row.display_name}")
            print(f"    cat:   {catalog_name}   ({score:.0f}% similar)")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", default="", help="write the full report to this file")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    report = Report()
    for path in DECANT_SHEETS:
        if os.path.exists(path):
            report.sheet_rows.extend(parse_decant_sheet(path, report))
        else:
            report.parse_skips.append(f"{path}: file not found")

    if os.path.exists(FULL_BOTTLE_SHEET):
        report.sheet_rows.extend(parse_full_bottle_sheet(FULL_BOTTLE_SHEET, report))
    else:
        report.parse_skips.append(f"{FULL_BOTTLE_SHEET}: file not found")

    reconcile(report.sheet_rows, report)
    print_report(report)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "parsed": len(report.sheet_rows),
                    "duplicate_conflicts": [
                        {"display_name": name,
                         "entries": {p: PERFUMES[p].get("prices", {}) for p in pids}}
                        for name, pids in find_duplicate_conflicts()
                    ],
                    "missing": [
                        {"display_name": r.display_name, "brand": r.brand, "name": r.name,
                         "clone_of": r.clone_of, "prices": r.prices,
                         "source": r.source, "page": r.page}
                        for r in report.missing
                    ],
                    "price_diffs": [
                        {"display_name": r.display_name, "perfume_id": pid, "diff": d,
                         "source": r.source, "page": r.page}
                        for r, pid, d in report.price_diffs
                    ],
                    "near_miss": [
                        {"sheet": r.display_name, "catalog": name, "score": s}
                        for r, name, s in report.near_miss
                    ],
                    "unmatched_catalog": report.unmatched_catalog,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
        print(f"Wrote {args.json}")


if __name__ == "__main__":
    main()
