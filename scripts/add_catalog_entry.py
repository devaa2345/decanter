"""
Add or update a single perfume in app/catalog_data.json by hand.

For the one-off case the dashboard upload flow is too heavy for: a customer
asks for something, it turns out not to be in the sheet, and you want the
bot answering for it now rather than after the next full re-upload.

The entry is built with exactly the conventions app/catalog_upload.py uses
for a bulk upload — same id slug, same display_name, same keyword
generation — so a later re-upload of the real sheet produces an identical
entry and shows no spurious diff.

IMPORTANT: this edits the local catalog only. Add the row to the actual
Google Sheet too, or the next dashboard publish will drop it again (see
app/catalog_upload.py — publishing overwrites catalog_data.json wholesale).

Run:
    python scripts/add_catalog_entry.py --brand Dior --name "Fahrenheit EDT" \
        --price 3ml=340 --price 5ml=520 --price 8ml=800 --price 10ml=990
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.catalog import CATALOG_PATH  # noqa: E402
from app.catalog_upload import (  # noqa: E402
    ParsedRow,
    _corpus_stopwords,
    _make_unique_id,
    _slugify,
    generate_keywords,
)
from app.matcher import normalize_message  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brand", required=True)
    parser.add_argument("--name", required=True, help="fragrance name, without the brand")
    parser.add_argument("--clone-of", default=None, help="designer original this is inspired by")
    parser.add_argument(
        "--price",
        action="append",
        default=[],
        required=True,
        metavar="SIZE=AMOUNT",
        help="repeatable, e.g. --price 3ml=340 --price 100ml_full=6500",
    )
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    prices: dict[str, int] = {}
    for item in args.price:
        size, _, amount = item.partition("=")
        if not amount.strip().isdigit():
            parser.error(f"bad --price {item!r} — expected SIZE=AMOUNT, e.g. 3ml=340")
        prices[size.strip()] = int(amount)

    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))

    display_name = f"{args.brand} {args.name}".strip()
    norm = normalize_message(display_name)

    existing_id = next(
        (pid for pid, v in catalog.items() if normalize_message(v.get("display_name", "")) == norm),
        None,
    )
    pid = existing_id or _make_unique_id(args.brand, args.name, set(catalog))

    # Keyword generation needs the corpus-frequency stopword set the bulk
    # pipeline derives from a whole upload — rebuild it from the live catalog
    # so a hand-added entry gets the same treatment as a bulk-added one.
    rows = [
        ParsedRow(
            brand="",
            name=v.get("display_name", ""),
            clone_of=v.get("clone_of"),
            prices=v.get("prices", {}),
        )
        for v in catalog.values()
    ]
    corpus_stopwords = _corpus_stopwords(rows)

    catalog[pid] = {
        "keywords": generate_keywords(args.brand, args.name, args.clone_of, corpus_stopwords),
        "display_name": display_name,
        "brand": args.brand or None,
        "prices": prices,
        "clone_of": args.clone_of,
    }

    CATALOG_PATH.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    action = "Updated" if existing_id else "Added"
    print(f"{action} {pid!r}")
    print(f"  display_name : {display_name}")
    print(f"  slug         : {_slugify(args.brand)}+{_slugify(args.name)}")
    print(f"  prices       : {prices}")
    print(f"  clone_of     : {args.clone_of}")
    print(f"  catalog now has {len(catalog)} entries")
    print("\nRemember to add this row to the Google Sheet too — the next")
    print("dashboard publish overwrites catalog_data.json wholesale.")


if __name__ == "__main__":
    main()
