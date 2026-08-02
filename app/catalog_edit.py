"""
Direct catalog edits from the owner dashboard — add one perfume, change
some, remove some.

Separate from app.catalog_upload, which is the bulk path: parse a whole
sheet, stage it as a version, review a diff, publish. That ceremony is
right for replacing 1,354 products at once and far too heavy for "the owner
just got Hawas Nautilus in and wants it sellable in ten seconds".

WHY THESE WRITE THE FILE DIRECTLY
---------------------------------
catalog_data.json is what the bot actually serves; Supabase holds the
version history. Routing a single-perfume add through the version machinery
would make the dashboard unusable wherever Supabase is not configured —
which, as of this writing, includes production. So every function here
writes the file and hot-reloads the index, then records a version as a
best-effort afterthought. The edit lands either way; the audit trail is a
bonus, never a precondition.

DUPLICATES ARE REFUSED, NOT MERGED
----------------------------------
A perfume whose name already exists is rejected with the id of the one
already there. The shop's sheet has ~90 rows listed twice and 39 of them
disagree about price, and every one of those started as somebody adding a
product that was already in the catalog. Refusing costs one click; a
duplicate costs a customer being quoted the wrong price for months.
"""

import logging
import re
from dataclasses import dataclass

from app.catalog import PERFUMES, reload_catalog
from app.catalog_upload import (
    _corpus_stopwords,
    _make_unique_id,
    _write_catalog_file,
    generate_keywords,
)
from app.matcher import normalize_message

logger = logging.getLogger(__name__)


class CatalogEditError(Exception):
    """A rejected edit — a duplicate name, an unknown id, an unusable price."""


@dataclass
class DuplicateHit:
    perfume_id: str
    display_name: str


# Decant tiers the dashboard offers as fixed choices, in the order the price
# card prints them. Free-form sizes are still accepted (the catalog has
# 55ml, 75ml and 60ml full bottles) — this is what the size dropdown is
# built from, not a whitelist.
DECANT_SIZES = ["3ml", "5ml", "8ml", "10ml", "20ml", "30ml"]

_SIZE_KEY_RE = re.compile(r"^(\d{1,4})(ml)(_full)?$|^ml_full$")


def normalize_size_key(raw: str) -> str:
    """
    A size the way catalog_data.json spells it: "10ml" for a decant,
    "100ml_full" for a bottle, "ml_full" for a bottle whose size nobody
    wrote down (see app.formatter, which prints that as "Full bottle").
    """
    key = (raw or "").strip().lower().replace(" ", "")
    if not key:
        raise CatalogEditError("A size is required (e.g. 10ml, or 100ml_full for a bottle).")
    if key.endswith("full") and not key.endswith("_full"):
        key = key[: -len("full")].rstrip("_") + "_full"
    if not _SIZE_KEY_RE.match(key):
        raise CatalogEditError(
            f"{raw!r} is not a size I recognize. Use e.g. 10ml, or 100ml_full for a full bottle."
        )
    return key


def clean_prices(raw: dict) -> dict[str, int]:
    """Validate a {size: price} map from the dashboard."""
    out: dict[str, int] = {}
    for size, value in (raw or {}).items():
        if value in (None, "", []):
            continue  # an empty box means "we do not sell this size"
        key = normalize_size_key(size)
        try:
            price = int(float(str(value).replace(",", "").replace("₹", "").strip()))
        except (TypeError, ValueError):
            raise CatalogEditError(f"{value!r} is not a price I can read for {size}.")
        if price <= 0:
            raise CatalogEditError(f"The {size} price has to be more than zero.")
        out[key] = price
    if not out:
        raise CatalogEditError("A perfume needs at least one size with a price.")
    return out


def find_duplicate(display_name: str, ignore_id: str | None = None) -> DuplicateHit | None:
    """The perfume already using this name, if any.

    Compared on the normalized name rather than the raw string, so
    "Lattafa  KHAMRAH" does not slip past "Lattafa Khamrah".
    """
    needle = normalize_message(display_name)
    if not needle:
        return None
    for pid, data in PERFUMES.items():
        if pid == ignore_id:
            continue
        if normalize_message(data.get("display_name", "")) == needle:
            return DuplicateHit(perfume_id=pid, display_name=data["display_name"])
    return None


def known_brands() -> list[str]:
    """Every brand already in the catalog, for the dashboard's autocomplete
    — so a new perfume joins "Ahmed Al Maghribi" rather than founding
    "Ahmed Al Maghrbi" next to it."""
    brands = {
        (data.get("brand") or "").strip()
        for data in PERFUMES.values()
        if (data.get("brand") or "").strip()
    }
    return sorted(brands, key=str.lower)


def _persist(catalog: dict, summary: str) -> None:
    """Write, reload, and record a version if there is somewhere to record
    it. The write and the reload are what matter; the version is history."""
    _write_catalog_file(catalog)
    reload_catalog()

    try:
        from app.name_index import build_index

        build_index()
    except Exception:
        logger.exception("Catalog saved but the name index failed to rebuild")

    try:
        _record_version(catalog, summary)
    except Exception:
        logger.info("Catalog edit not versioned (%s) — the edit itself is saved", summary)


def _record_version(catalog: dict, summary: str) -> None:
    import json
    from datetime import datetime, timezone

    from app.db import CATALOG_BUCKET, get_client

    client = get_client()
    if client is None:
        return

    resp = (
        client.table("catalog_versions")
        .insert(
            {
                "status": "published",
                "is_active": True,
                "source_filename": summary,
                "storage_path": "",
                "perfume_count": len(catalog),
                "added_count": 0,
                "updated_count": 0,
                "removed_count": 0,
                "diff": {"summary": summary},
                "parse_warnings": [],
                "published_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        .execute()
    )
    version_id = resp.data[0]["id"]
    path = f"v{version_id}.json"
    client.storage.from_(CATALOG_BUCKET).upload(
        path,
        json.dumps(catalog, ensure_ascii=False, indent=2).encode("utf-8"),
        {"content-type": "application/json", "upsert": "true"},
    )
    client.table("catalog_versions").update({"is_active": False}).neq("id", version_id).eq(
        "is_active", True
    ).execute()
    client.table("catalog_versions").update({"storage_path": path}).eq("id", version_id).execute()


def _catalog_stopwords() -> set[str]:
    """Words too common across the WHOLE catalog to be a safe standalone
    keyword — "afnan", "chanel", "oud".

    Measured against every product, not against the one being edited. A
    single-row corpus has no frequencies to speak of, so nothing looked
    common and a hand-edited perfume came out with broader keywords than the
    identical perfume would have had via an import. Same product, same
    keywords, whichever door it came through.
    """
    from app.catalog_upload import ParsedRow

    rows = []
    for data in PERFUMES.values():
        brand = (data.get("brand") or "").strip()
        display = data.get("display_name", "")
        bare = display[len(brand):].strip() if brand and display.startswith(brand) else display
        rows.append(ParsedRow(brand=brand, name=bare, clone_of=data.get("clone_of"), prices={}))
    return _corpus_stopwords(rows)


def _entry(brand: str, name: str, clone_of: str | None, prices: dict[str, int]) -> dict:
    display_name = f"{brand} {name}".strip()
    stopwords = _catalog_stopwords()
    return {
        "keywords": generate_keywords(brand, name, clone_of, stopwords),
        "display_name": display_name,
        "brand": brand.strip() or None,
        "prices": prices,
        "clone_of": (clone_of or "").strip() or None,
    }


def add_perfume(brand: str, name: str, clone_of: str | None, prices: dict) -> dict:
    """
    Add one perfume. Refuses a name the catalog already has.

    The keyword list is generated exactly as the bulk importer generates it,
    so a hand-added perfume is matched by the same rules as the other 1,354
    and not by a second, subtly different set.
    """
    name = (name or "").strip()
    brand = (brand or "").strip()
    if not name:
        raise CatalogEditError("A perfume needs a name.")

    display_name = f"{brand} {name}".strip()
    duplicate = find_duplicate(display_name)
    if duplicate:
        raise CatalogEditError(
            f"{duplicate.display_name!r} is already in the catalog — nothing was added. "
            f"Edit that one instead if the prices have changed."
        )

    cleaned = clean_prices(prices)
    catalog = {pid: dict(data) for pid, data in PERFUMES.items()}
    pid = _make_unique_id(brand, name, set(catalog))
    catalog[pid] = _entry(brand, name, clone_of, cleaned)

    _persist(catalog, f"added {display_name}")
    return {"perfume_id": pid, **catalog[pid]}


def update_perfume(
    perfume_id: str,
    brand: str | None = None,
    name: str | None = None,
    clone_of: str | None = None,
    prices: dict | None = None,
) -> dict:
    """Change one perfume. Only the fields supplied are touched."""
    if perfume_id not in PERFUMES:
        raise CatalogEditError(f"No perfume with id {perfume_id!r} — it may have been removed.")

    current = PERFUMES[perfume_id]
    current_brand = (current.get("brand") or "").strip()
    current_name = current.get("display_name", "")
    if current_brand and current_name.startswith(current_brand):
        current_name = current_name[len(current_brand) :].strip()

    new_brand = current_brand if brand is None else brand.strip()
    new_name = current_name if name is None else name.strip()
    if not new_name:
        raise CatalogEditError("A perfume needs a name.")

    display_name = f"{new_brand} {new_name}".strip()
    duplicate = find_duplicate(display_name, ignore_id=perfume_id)
    if duplicate:
        raise CatalogEditError(
            f"{duplicate.display_name!r} already uses that name — the change was not saved."
        )

    new_prices = dict(current.get("prices") or {}) if prices is None else clean_prices(prices)
    new_clone = current.get("clone_of") if clone_of is None else clone_of

    catalog = {pid: dict(data) for pid, data in PERFUMES.items()}
    catalog[perfume_id] = _entry(new_brand, new_name, new_clone, new_prices)

    _persist(catalog, f"edited {display_name}")
    return {"perfume_id": perfume_id, **catalog[perfume_id]}


def delete_perfumes(perfume_ids: list[str]) -> dict:
    """Remove one or many. Unknown ids are reported rather than ignored —
    a bulk delete that quietly did less than it said would be worse than
    one that refuses."""
    ids = [pid for pid in (perfume_ids or []) if pid]
    if not ids:
        raise CatalogEditError("Nothing was selected.")

    missing = [pid for pid in ids if pid not in PERFUMES]
    if missing:
        raise CatalogEditError(
            f"{len(missing)} of these are no longer in the catalog: {', '.join(missing[:5])}"
        )

    removed = [PERFUMES[pid]["display_name"] for pid in ids]
    catalog = {pid: dict(data) for pid, data in PERFUMES.items() if pid not in set(ids)}

    _persist(catalog, f"removed {len(ids)} perfume(s)")
    return {"removed": len(ids), "names": removed}


def add_many(entries: list[dict]) -> dict:
    """
    Add a batch — the "add these 43" action behind the upload report's list
    of full-bottle-only perfumes.

    Returns the same shape as an upload: what went in, what was skipped for
    already existing, and why. Adding 43 products with no account of what
    happened to each is how a catalog quietly gains duplicates.
    """
    catalog = {pid: dict(data) for pid, data in PERFUMES.items()}
    by_name = {normalize_message(d.get("display_name", "")): pid for pid, d in catalog.items()}

    added: list[dict] = []
    skipped: list[dict] = []

    for entry in entries or []:
        brand = (entry.get("brand") or "").strip()
        name = (entry.get("name") or "").strip()
        display_name = f"{brand} {name}".strip() or (entry.get("display_name") or "").strip()
        if not display_name:
            skipped.append({"display_name": "(no name)", "reason": "no name given"})
            continue

        key = normalize_message(display_name)
        if key in by_name:
            skipped.append(
                {
                    "display_name": display_name,
                    "reason": "already in the catalog",
                    "existing_id": by_name[key],
                }
            )
            continue

        try:
            prices = clean_prices(entry.get("prices") or {})
        except CatalogEditError as exc:
            skipped.append({"display_name": display_name, "reason": str(exc)})
            continue

        if not brand and not name:
            brand, name = "", display_name
        pid = _make_unique_id(brand, name, set(catalog))
        catalog[pid] = _entry(brand, name, entry.get("clone_of"), prices)
        by_name[key] = pid
        added.append({"perfume_id": pid, "display_name": catalog[pid]["display_name"]})

    if added:
        _persist(catalog, f"added {len(added)} perfume(s) in bulk")

    return {
        "submitted": len(entries or []),
        "added": len(added),
        "skipped": len(skipped),
        "added_items": added,
        "skipped_items": skipped,
    }


def bulk_update(perfume_ids: list[str], ops: dict) -> dict:
    """
    Apply the same change to many perfumes at once.

    Editing 40 products one dialog at a time is not editing, it is data
    entry — and the changes an owner actually makes in bulk are not "rename
    these", they are "put the 3ml up by ten rupees" or "we stopped doing
    20ml". So this takes operations rather than field values:

        set_price     {"3ml": 170}      exact price for a size, on all of them
        remove_sizes  ["20ml"]          stop selling a size
        adjust_pct    5                 every price up 5% (negative to cut)
        adjust_flat   10                every price up ₹10
        set_brand     "Ahmed Al Maghribi"

    Rounded to whole rupees, because that is what a price card prints, and
    never below ₹1 — a percentage cut applied to a cheap decant can
    otherwise land on zero or negative and be published as a real price.

    A perfume left with no prices at all would be unsellable and invisible,
    so removing every size it has is refused rather than performed.
    """
    ids = [pid for pid in (perfume_ids or []) if pid]
    if not ids:
        raise CatalogEditError("Nothing was selected.")

    missing = [pid for pid in ids if pid not in PERFUMES]
    if missing:
        raise CatalogEditError(
            f"{len(missing)} of these are no longer in the catalog: {', '.join(missing[:5])}"
        )

    set_price = {}
    for size, value in (ops.get("set_price") or {}).items():
        key = normalize_size_key(size)
        try:
            price = int(float(str(value).replace(",", "").replace("₹", "").strip()))
        except (TypeError, ValueError):
            raise CatalogEditError(f"{value!r} is not a price I can read for {size}.")
        if price <= 0:
            raise CatalogEditError(f"The {size} price has to be more than zero.")
        set_price[key] = price

    remove = {normalize_size_key(s) for s in (ops.get("remove_sizes") or [])}
    pct = float(ops.get("adjust_pct") or 0)
    flat = float(ops.get("adjust_flat") or 0)
    brand = ops.get("set_brand")

    if not (set_price or remove or pct or flat or brand is not None):
        raise CatalogEditError("Pick at least one change to apply.")

    catalog = {pid: dict(data) for pid, data in PERFUMES.items()}
    changed: list[dict] = []
    refused: list[dict] = []

    for pid in ids:
        current = catalog[pid]
        prices = dict(current.get("prices") or {})

        if pct or flat:
            for size, price in list(prices.items()):
                prices[size] = max(1, round(price * (1 + pct / 100) + flat))
        prices.update(set_price)
        for size in remove:
            prices.pop(size, None)

        if not prices:
            refused.append(
                {
                    "display_name": current["display_name"],
                    "reason": "that would leave it with no prices at all — it would stop being sellable",
                }
            )
            continue

        current_brand = (current.get("brand") or "").strip()
        bare = current.get("display_name", "")
        if current_brand and bare.startswith(current_brand):
            bare = bare[len(current_brand) :].strip()
        new_brand = current_brand if brand is None else str(brand).strip()

        catalog[pid] = _entry(new_brand, bare, current.get("clone_of"), prices)
        changed.append({"perfume_id": pid, "display_name": catalog[pid]["display_name"]})

    if changed:
        _persist(catalog, f"bulk-edited {len(changed)} perfume(s)")

    return {
        "selected": len(ids),
        "changed": len(changed),
        "refused": len(refused),
        "changed_items": changed,
        "refused_items": refused,
    }


def card_preview(perfume_ids: list[str], sizes: list[str] | None = None) -> dict:
    """
    The price card for these perfumes, exactly as a customer would receive
    it — for the console's copy button.

    Rendered through app.formatter.render_cards rather than rebuilt here, so
    what the owner pastes into a chat is character-for-character what the bot
    sends. A second formatter would drift, and the first anyone would hear of
    it is a customer quoted in a format the shop does not use.

    `sizes` trims the card to the tiers the customer asked about. The bot
    never does this — it always shows the whole grid — but the owner pasting
    a card by hand is answering a specific question and should not have to
    delete six lines each time.
    """
    from app.formatter import render_cards

    ids = [pid for pid in (perfume_ids or []) if pid in PERFUMES]
    if not ids:
        raise CatalogEditError("Select at least one perfume.")

    wanted = {normalize_size_key(s) for s in sizes} if sizes else None
    perfumes = []
    dropped: list[str] = []

    for pid in ids:
        data = PERFUMES[pid]
        if wanted is None:
            perfumes.append(data)
            continue
        trimmed = {k: v for k, v in (data.get("prices") or {}).items() if k in wanted}
        if not trimmed:
            dropped.append(data["display_name"])
            continue
        perfumes.append({**data, "prices": trimmed})

    if not perfumes:
        raise CatalogEditError(
            "None of the selected perfumes come in those sizes, so there is no card to copy."
        )

    return {
        "text": render_cards(perfumes),
        "perfumes": len(perfumes),
        "dropped": dropped,
    }
