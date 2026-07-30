"""
Sovereign Scents — Perfume Catalog

Prices and keywords are loaded from catalog_data.json (same directory).
To update prices by hand: edit catalog_data.json directly.
To add a new perfume by hand: add a new entry to catalog_data.json following the existing shape.

Prefer the dashboard's catalog upload/retrain feature (app/catalog_upload.py)
for bulk updates — it regenerates catalog_data.json from an uploaded sheet
and hot-reloads it via reload_catalog(), no redeploy needed.

Each perfume entry has:
  - keywords: list of lowercase trigger words/phrases for matching
  - display_name: shown in the WhatsApp reply card header
  - brand: shown in the dashboard (optional — only present on entries produced by a catalog upload)
  - prices: dict mapping size tier (e.g. "3ml", "5ml", "100ml_full") to price in INR
  - clone_of: what original perfume this is inspired by (for reference only)
"""

import json
from pathlib import Path


CATALOG_PATH = Path(__file__).parent / "catalog_data.json"


def _load() -> dict[str, dict]:
    with open(CATALOG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


PERFUMES: dict[str, dict] = _load()


def reload_catalog() -> None:
    """
    Re-read catalog_data.json from disk and hot-swap PERFUMES in place.

    Mutates the existing dict (clear + update) rather than rebinding the
    module attribute, so every `from app.catalog import PERFUMES` reference
    elsewhere (matcher.py, formatter.py, groq_client.py) sees the update
    immediately without re-importing anything.
    """
    fresh = _load()
    PERFUMES.clear()
    PERFUMES.update(fresh)

    # The matcher's index is derived from PERFUMES, so it has to be rebuilt
    # too — and it cannot detect this on its own, since a publish that only
    # corrects prices or spellings leaves the entry count unchanged.
    # Imported here rather than at module scope: app.name_index imports
    # PERFUMES from this module.
    from app.name_index import build_index

    build_index()


# Shipping rates by region (INR) — the single source of truth for both the
# text SHIPPING_CARD below and the per-region grand totals shown when a
# customer names one specific ml size (see app.formatter's requested_ml
# handling): keeping one dict means the two can never drift apart.
SHIPPING_RATES: dict[str, int] = {
    "Delhi NCR": 65,
    "Rest of India": 80,
    "J&K, NE, Lakshadweep & Andaman": 100,
}

# Shipping rates — appended to every price card reply
SHIPPING_CARD = (
    # No leading emoji / "|" separator here on purpose — this card, sent
    # together with 🌸 in the price header, got rejected outright by Chat
    # Mitra's send API ("Text contains invalid characters"). Not isolated
    # further (avoided burning more real send attempts); "-" is confirmed
    # safe (it's already in the successfully-sent fallback message). One
    # region per line reads more cleanly than the old single-line layout.
    "Prepaid only, no COD\n"
    + "\n".join(f"₹{rate} - {region}" for region, rate in SHIPPING_RATES.items())
)
