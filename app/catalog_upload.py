"""
Catalog retrain pipeline: turn an uploaded sheet (.xlsx/.csv) into the same
shape as catalog_data.json, diff it against the live catalog, and let the
owner review the diff before it goes live.

There's no ML model here — "retrain" means re-deriving keywords/prices/ids
from a fresh sheet using the same conventions the existing 1,200+ entry
catalog already follows (reverse-engineered from catalog_data.json):

  - perfume_id  = slug(brand) + slug(fragrance_name), concatenated directly
                  (e.g. "Afnan" + "9PM Rebel" -> "afnan" + "9pm_rebel").
  - display_name = f"{brand} {fragrance_name}".strip()
  - keywords include 1/2/3-word windows of brand+name and of clone_of (so a
    customer typing the *original* designer perfume name still matches the
    clone that's inspired by it).

One deliberate improvement over a naive tokenizer: single-word keywords are
filtered by BOTH the static app.matcher.GENERIC_STOPWORDS list (generic
English filler words) and a per-upload *corpus* frequency check — a word
like "oud" or "noir" that shows up across dozens of entries in a Middle
Eastern-perfume-heavy catalog would otherwise become a standalone keyword
that matches almost everything (the same false-positive problem
GENERIC_STOPWORDS already exists to prevent for English filler words).
Multi-word phrases aren't filtered this way since a 2-3 word phrase is
inherently specific enough to be safe.

parse_upload() only ever *produces* a candidate version + diff — nothing
here touches the live catalog directly except _activate_version(), reached
only via publish_version()/rollback_version().
"""

import csv
import io
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.db import CATALOG_BUCKET, require_client
from app.matcher import GENERIC_STOPWORDS, normalize_message

logger = logging.getLogger(__name__)


class CatalogParseError(Exception):
    """Raised for problems with a specific upload/version request (bad file, bad id, wrong state)."""


# --- Header recognition -----------------------------------------------------

BRAND_HEADERS = {"brand"}
NAME_HEADERS = {"fragrance name", "perfume name", "name", "product name", "fragrance"}
CLONE_HEADERS = {"clone of", "clone", "inspired by", "original", "inspired"}
FULL_BOTTLE_HEADERS = {"bnib", "full bottle", "full", "fullbottle", "bottle", "full size"}
SIZE_HEADER_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*ml$", re.IGNORECASE)


def _find_column_map(headers: list[str]) -> dict:
    """Map recognized column purposes to their column index."""
    col_map: dict = {"sizes": {}}

    for idx, raw_header in enumerate(headers):
        h = (raw_header or "").strip().lower()
        if not h:
            continue
        if h in BRAND_HEADERS:
            col_map["brand"] = idx
        elif h in NAME_HEADERS:
            col_map["name"] = idx
        elif h in CLONE_HEADERS:
            col_map["clone_of"] = idx
        elif h in FULL_BOTTLE_HEADERS:
            col_map["full_bottle"] = idx
        else:
            m = SIZE_HEADER_RE.match(h)
            if m:
                size_num = m.group(1)
                col_map["sizes"][f"{size_num}ml"] = idx

    if "name" not in col_map:
        raise CatalogParseError(
            "Could not find a 'Fragrance Name' column. Found headers: "
            + ", ".join(h for h in headers if h)
        )
    if not col_map["sizes"] and "full_bottle" not in col_map:
        raise CatalogParseError(
            "Could not find any size/price columns (e.g. '3ml', '10ml', 'BNIB'). Found headers: "
            + ", ".join(h for h in headers if h)
        )

    return col_map


def _read_rows(filename: str, content: bytes) -> tuple[list[str], list[list]]:
    """Parse the raw file into (headers, data_rows), scanning for the header row."""
    ext = filename.lower().rsplit(".", 1)[-1] if filename and "." in filename else ""

    if ext == "csv":
        text = content.decode("utf-8-sig", errors="replace")
        all_rows = [row for row in csv.reader(io.StringIO(text))]
    elif ext in ("xlsx", "xlsm"):
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
        sheet = wb.active
        all_rows = [
            ["" if c is None else str(c) for c in row] for row in sheet.iter_rows(values_only=True)
        ]
    else:
        raise CatalogParseError(
            f"Unsupported file type '.{ext}'. Please upload a .xlsx or .csv export of the sheet "
            "(PDF exports can't be parsed reliably — use File > Download in Google Sheets instead)."
        )

    # The real sheet has promo/shipping text above the actual table (see
    # SUPERSEDED source PDFs), so scan the first few rows for the header
    # rather than assuming row 0 is it.
    header_idx = None
    for i, row in enumerate(all_rows[:20]):
        lowered = {str(c).strip().lower() for c in row if c}
        if lowered & (BRAND_HEADERS | NAME_HEADERS):
            header_idx = i
            break

    if header_idx is None:
        raise CatalogParseError(
            "Could not find a header row containing 'Brand' or 'Fragrance Name' in the first 20 rows."
        )

    headers = all_rows[header_idx]
    data_rows = all_rows[header_idx + 1 :]
    return headers, data_rows


# --- Full-bottle (BNIB) free-text price parsing -----------------------------

_SIZE_PRICE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*ml[^\d]{0,15}?(\d[\d,]*)", re.IGNORECASE)


def parse_full_bottle_cell(raw: str) -> tuple[dict[str, int], list[str]]:
    """
    Parse a free-text full-bottle cell (e.g. "100ml - 2800", "50ml/1600,
    100ml/2800") into {"{size}ml_full": price} entries.

    Anything that doesn't match a clear "SIZEml ... PRICE" pattern is left
    out and reported as a warning instead of guessed — this feeds
    customer-facing prices, so silent guesses aren't an option.
    """
    raw = (raw or "").strip()
    if not raw:
        return {}, []

    matches = list(_SIZE_PRICE_RE.finditer(raw))
    if not matches:
        return {}, [f"couldn't read a size+price from full-bottle value {raw!r} — expected e.g. '100ml - 2800'"]

    prices: dict[str, int] = {}
    for m in matches:
        size_str, price_str = m.group(1), m.group(2).replace(",", "")
        size_num = float(size_str)
        size_key = f"{int(size_num)}ml_full" if size_num.is_integer() else f"{size_str}ml_full"
        try:
            prices[size_key] = int(price_str)
        except ValueError:
            continue

    return prices, []


# --- Row parsing --------------------------------------------------------

@dataclass
class ParsedRow:
    brand: str
    name: str
    clone_of: str | None
    prices: dict[str, int] = field(default_factory=dict)


def _parse_rows(data_rows: list[list], col_map: dict) -> tuple[list[ParsedRow], list[str]]:
    parsed: list[ParsedRow] = []
    warnings: list[str] = []

    def cell(row: list, idx: int | None) -> str:
        if idx is None or idx >= len(row) or row[idx] is None:
            return ""
        return str(row[idx]).strip()

    for i, row in enumerate(data_rows):
        row_num = i + 1
        brand = cell(row, col_map.get("brand"))
        name = cell(row, col_map.get("name"))

        if not name and not brand:
            continue  # blank / section-divider row — not an error, just skip

        if not name:
            warnings.append(f"row {row_num}: has brand '{brand}' but no fragrance name — skipped")
            continue

        clone_of = cell(row, col_map.get("clone_of")) or None

        prices: dict[str, int] = {}
        for tier, idx in col_map["sizes"].items():
            raw_val = cell(row, idx)
            if not raw_val:
                continue
            cleaned = raw_val.replace(",", "").replace("₹", "").strip()
            try:
                prices[tier] = int(float(cleaned))
            except ValueError:
                warnings.append(f"row {row_num} ({name}): couldn't read {tier} price {raw_val!r} — skipped that size")

        if "full_bottle" in col_map:
            fb_prices, fb_warnings = parse_full_bottle_cell(cell(row, col_map["full_bottle"]))
            prices.update(fb_prices)
            warnings.extend(f"row {row_num} ({name}): {w}" for w in fb_warnings)

        if not prices:
            warnings.append(f"row {row_num} ({name}): no valid prices for any size — entry skipped")
            continue

        parsed.append(ParsedRow(brand=brand, name=name, clone_of=clone_of, prices=prices))

    return parsed, warnings


# --- ID + keyword generation ---------------------------------------------

def _slugify(text: str) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def _make_unique_id(brand: str, name: str, used_ids: set[str]) -> str:
    base = _slugify(brand) + _slugify(name) or "perfume"
    candidate = base
    n = 2
    while candidate in used_ids:
        candidate = f"{base}_{n}"
        n += 1
    return candidate


def _ngrams(words: list[str], n: int) -> list[str]:
    return [" ".join(words[i : i + n]) for i in range(len(words) - n + 1)]


def _add_phrase_and_grams(keywords: set[str], text: str, corpus_stopwords: set[str]) -> None:
    norm = normalize_message(text)
    if not norm:
        return
    keywords.add(norm)
    words = norm.split()
    for n in (1, 2, 3):
        for gram in _ngrams(words, n):
            if len(gram) < 3:
                continue
            if n == 1 and (gram in GENERIC_STOPWORDS or gram in corpus_stopwords):
                continue
            keywords.add(gram)


def generate_keywords(brand: str, name: str, clone_of: str | None, corpus_stopwords: set[str]) -> list[str]:
    keywords: set[str] = set()
    _add_phrase_and_grams(keywords, f"{brand} {name}", corpus_stopwords)
    _add_phrase_and_grams(keywords, name, corpus_stopwords)
    if clone_of:
        _add_phrase_and_grams(keywords, clone_of, corpus_stopwords)
    return sorted(k for k in keywords if len(k) >= 3)


def _corpus_stopwords(parsed_rows: list[ParsedRow]) -> set[str]:
    """Words that show up in a large share of this upload's entries — too generic to be a safe standalone keyword."""
    freq: Counter = Counter()
    for r in parsed_rows:
        words = set(normalize_message(f"{r.brand} {r.name} {r.clone_of or ''}").split())
        freq.update(words)

    total = len(parsed_rows) or 1
    threshold = max(8, int(total * 0.015))
    return {w for w, c in freq.items() if c > threshold}


# --- Diff + candidate catalog construction --------------------------------

def build_catalog_from_rows(parsed_rows: list[ParsedRow], existing: dict[str, dict]) -> tuple[dict, dict]:
    """Build the candidate catalog dict + a structured diff against `existing`."""
    existing_by_name = {normalize_message(v.get("display_name", "")): pid for pid, v in existing.items()}
    corpus_stopwords = _corpus_stopwords(parsed_rows)

    new_catalog: dict[str, dict] = {}
    used_ids: set[str] = set()
    added: list[dict] = []
    updated: list[dict] = []

    for r in parsed_rows:
        display_name = f"{r.brand} {r.name}".strip()
        norm_name = normalize_message(display_name)
        existing_id = existing_by_name.get(norm_name)
        pid = existing_id or _make_unique_id(r.brand, r.name, used_ids)
        used_ids.add(pid)

        new_catalog[pid] = {
            "keywords": generate_keywords(r.brand, r.name, r.clone_of, corpus_stopwords),
            "display_name": display_name,
            "brand": r.brand or None,
            "prices": r.prices,
            "clone_of": r.clone_of,
        }

        if existing_id:
            old_prices = existing[existing_id].get("prices", {})
            if old_prices != r.prices:
                updated.append(
                    {
                        "perfume_id": pid,
                        "display_name": display_name,
                        "old_prices": old_prices,
                        "new_prices": r.prices,
                    }
                )
        else:
            added.append({"perfume_id": pid, "display_name": display_name, "prices": r.prices})

    removed = [
        {"perfume_id": pid, "display_name": v.get("display_name", pid)}
        for pid, v in existing.items()
        if pid not in used_ids
    ]

    diff = {
        "added": added,
        "updated": updated,
        "removed": removed,
        "added_count": len(added),
        "updated_count": len(updated),
        "removed_count": len(removed),
    }
    return new_catalog, diff


# --- Supabase-backed version storage ---------------------------------------

def _download_version_json(client, storage_path: str) -> dict:
    raw = client.storage.from_(CATALOG_BUCKET).download(storage_path)
    return json.loads(raw.decode("utf-8"))


def _get_active_catalog(client) -> dict:
    """The catalog to diff a new upload against: Supabase's active version if one exists, else the live in-memory catalog."""
    resp = client.table("catalog_versions").select("storage_path").eq("is_active", True).limit(1).execute()
    if resp.data:
        return _download_version_json(client, resp.data[0]["storage_path"])

    from app.catalog import PERFUMES

    return dict(PERFUMES)


def _get_version(client, version_id: int) -> dict | None:
    resp = client.table("catalog_versions").select("*").eq("id", version_id).limit(1).execute()
    return resp.data[0] if resp.data else None


def _write_catalog_file(catalog: dict) -> None:
    from app.catalog import CATALOG_PATH

    CATALOG_PATH.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")


def create_pending_version(filename: str, content: bytes) -> dict:
    """Parse an uploaded sheet, diff it against the active catalog, and store it as a pending version for review."""
    client = require_client()

    active = _get_active_catalog(client)
    headers, data_rows = _read_rows(filename, content)
    col_map = _find_column_map(headers)
    parsed_rows, row_warnings = _parse_rows(data_rows, col_map)

    if not parsed_rows:
        raise CatalogParseError("No usable rows found in the uploaded sheet.")

    new_catalog, diff = build_catalog_from_rows(parsed_rows, active)

    insert_resp = (
        client.table("catalog_versions")
        .insert(
            {
                "status": "pending",
                "source_filename": filename,
                "storage_path": "",
                "perfume_count": len(new_catalog),
                "added_count": diff["added_count"],
                "updated_count": diff["updated_count"],
                "removed_count": diff["removed_count"],
                "diff": diff,
                "parse_warnings": row_warnings,
            }
        )
        .execute()
    )
    version = insert_resp.data[0]
    version_id = version["id"]
    storage_path = f"v{version_id}.json"

    blob = json.dumps(new_catalog, ensure_ascii=False, indent=2).encode("utf-8")
    client.storage.from_(CATALOG_BUCKET).upload(
        storage_path, blob, {"content-type": "application/json", "upsert": "true"}
    )
    client.table("catalog_versions").update({"storage_path": storage_path}).eq("id", version_id).execute()
    version["storage_path"] = storage_path

    return version


def list_versions(limit: int = 30) -> list[dict]:
    client = require_client()
    resp = (
        client.table("catalog_versions")
        .select(
            "id,status,is_active,source_filename,perfume_count,added_count,"
            "updated_count,removed_count,parse_warnings,created_at,published_at"
        )
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return resp.data or []


def get_version_detail(version_id: int) -> dict:
    client = require_client()
    version = _get_version(client, version_id)
    if version is None:
        raise CatalogParseError(f"Version {version_id} not found")
    return version


def _activate_version(client, version_id: int) -> dict:
    version = _get_version(client, version_id)
    if version is None:
        raise CatalogParseError(f"Version {version_id} not found")

    catalog = _download_version_json(client, version["storage_path"])
    _write_catalog_file(catalog)

    from app.catalog import reload_catalog

    reload_catalog()

    now = datetime.now(timezone.utc).isoformat()
    client.table("catalog_versions").update({"is_active": False}).eq("is_active", True).execute()
    client.table("catalog_versions").update(
        {"status": "published", "is_active": True, "published_at": now}
    ).eq("id", version_id).execute()

    version["status"] = "published"
    version["is_active"] = True
    version["published_at"] = now
    return version


def publish_version(version_id: int) -> dict:
    """Make a pending version live: writes catalog_data.json and hot-reloads the running bot."""
    client = require_client()
    version = _get_version(client, version_id)
    if version is None:
        raise CatalogParseError(f"Version {version_id} not found")
    if version["status"] != "pending":
        raise CatalogParseError(f"Version {version_id} is '{version['status']}', not pending — nothing to publish")
    return _activate_version(client, version_id)


def rollback_version(version_id: int) -> dict:
    """Re-activate a previously-published version (any version, active or not)."""
    client = require_client()
    version = _get_version(client, version_id)
    if version is None:
        raise CatalogParseError(f"Version {version_id} not found")
    if version["status"] != "published":
        raise CatalogParseError(f"Version {version_id} was never published — nothing to roll back to")
    return _activate_version(client, version_id)


def discard_version(version_id: int) -> None:
    """Reject a pending version without ever making it live."""
    client = require_client()
    client.table("catalog_versions").update({"status": "discarded"}).eq("id", version_id).eq(
        "status", "pending"
    ).execute()


def sync_active_catalog_to_disk() -> bool:
    """
    Best-effort startup hook: pull whatever is active in Supabase down to
    catalog_data.json and hot-load it, so a redeploy picks up the latest
    published catalog instead of whatever was baked into the deploy image.

    No-ops (returns False) if Supabase isn't configured or has no active
    version yet — the bundled catalog_data.json keeps working either way.
    """
    try:
        client = require_client()
    except Exception:
        return False

    resp = client.table("catalog_versions").select("storage_path").eq("is_active", True).limit(1).execute()
    if not resp.data:
        return False

    catalog = _download_version_json(client, resp.data[0]["storage_path"])
    _write_catalog_file(catalog)

    from app.catalog import reload_catalog

    reload_catalog()
    return True
