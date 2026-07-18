"""
Bot analytics: logs every inbound WhatsApp query + match outcome to Supabase,
and provides the aggregate queries that power the dashboard.

Everything here is best-effort on the write side — a Supabase hiccup must
never break a customer-facing reply, so log_message_event() never raises.
The read-side (dashboard) functions raise AnalyticsUnavailable when Supabase
isn't configured, which the API layer turns into a clear error — a dashboard
chart failing to load is a very different failure than a customer not
getting a price, so it's allowed to be louder.

Aggregation is done in Python after fetching recent rows rather than via
SQL views/RPCs — this bot serves a single WhatsApp number, so event volume
stays small (low thousands at most) and straightforward client-side grouping
is far simpler to read, test, and change than maintaining Postgres functions.
"""

import asyncio
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

from app.db import SupabaseUnavailable, get_client, require_client

logger = logging.getLogger(__name__)


# --- Write side -----------------------------------------------------------

def _insert_message_event(client, record: dict) -> None:
    client.table("message_events").insert(record).execute()


async def log_message_event(
    *,
    message_id: str,
    sender: str,
    message_text: str,
    perfume_id: str | None,
    layer: str | None,
    confidence: float | None,
    ambiguous: bool,
    reply_sent: bool,
) -> None:
    """Best-effort insert of one inbound-message record. Never raises."""
    client = get_client()
    if client is None:
        return

    record = {
        "message_id": message_id,
        "sender": sender,
        "message_text": (message_text or "")[:2000],
        "perfume_id": perfume_id,
        "layer": layer,
        "confidence": confidence,
        "ambiguous": ambiguous,
        "reply_sent": reply_sent,
    }

    try:
        await asyncio.to_thread(_insert_message_event, client, record)
    except Exception:
        logger.exception("Failed to log message event to Supabase")


# --- Read side (dashboard queries) -----------------------------------------

# Re-exported for callers that only import from app.analytics.
AnalyticsUnavailable = SupabaseUnavailable
_require_client = require_client


def _fetch_events_since(client, since: datetime, limit: int = 50000) -> list[dict]:
    resp = (
        client.table("message_events")
        .select("message_text,perfume_id,layer,ambiguous,reply_sent,created_at")
        .gte("created_at", since.isoformat())
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return resp.data or []


async def get_overview(days: int = 30) -> dict:
    """Summary cards: volume, match rate, catalog size, unmatched rate."""
    client = _require_client()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    events = await asyncio.to_thread(_fetch_events_since, client, since)

    total = len(events)
    matched = sum(1 for e in events if e.get("perfume_id") and not e.get("ambiguous"))
    ambiguous = sum(1 for e in events if e.get("ambiguous"))
    unmatched = total - matched - ambiguous

    from app.catalog import PERFUMES

    return {
        "period_days": days,
        "total_queries": total,
        "matched": matched,
        "ambiguous": ambiguous,
        "unmatched": unmatched,
        "match_rate": round(matched / total * 100, 1) if total else None,
        "unmatched_rate": round(unmatched / total * 100, 1) if total else None,
        "catalog_size": len(PERFUMES),
    }


async def get_timeseries(days: int = 30) -> list[dict]:
    """Daily counts by outcome, oldest first — powers the queries-over-time chart."""
    client = _require_client()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    events = await asyncio.to_thread(_fetch_events_since, client, since)

    buckets: dict[str, Counter] = {}
    for e in events:
        day = (e.get("created_at") or "")[:10]  # ISO date prefix
        if not day:
            continue
        bucket = buckets.setdefault(day, Counter())
        if e.get("ambiguous"):
            bucket["ambiguous"] += 1
        elif e.get("perfume_id"):
            bucket[e.get("layer") or "exact"] += 1
        else:
            bucket["unmatched"] += 1

    return [
        {
            "date": day,
            "exact": c.get("exact", 0),
            "fuzzy": c.get("fuzzy", 0),
            "llm": c.get("llm", 0),
            "ambiguous": c.get("ambiguous", 0),
            "unmatched": c.get("unmatched", 0),
            "total": sum(c.values()),
        }
        for day, c in sorted(buckets.items())
    ]


async def get_top_perfumes(days: int = 30, limit: int = 15) -> list[dict]:
    """Most-queried perfumes in the period, with display names from the live catalog."""
    client = _require_client()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    events = await asyncio.to_thread(_fetch_events_since, client, since)

    counts = Counter(
        e["perfume_id"] for e in events if e.get("perfume_id") and not e.get("ambiguous")
    )

    from app.catalog import PERFUMES

    results = []
    for pid, count in counts.most_common(limit):
        perfume = PERFUMES.get(pid)
        results.append(
            {
                "perfume_id": pid,
                "display_name": perfume["display_name"] if perfume else pid,
                "count": count,
            }
        )
    return results


async def get_unmatched_queries(days: int = 30, limit: int = 50) -> list[dict]:
    """
    Recent unmatched queries, grouped by normalized text — the catalog-gap
    finder: what customers asked that the bot couldn't answer, so the owner
    knows exactly what to add or fix next.
    """
    client = _require_client()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    events = await asyncio.to_thread(_fetch_events_since, client, since)

    unmatched = [e for e in events if not e.get("perfume_id") and not e.get("ambiguous")]

    groups: dict[str, dict] = {}
    for e in unmatched:
        key = (e.get("message_text") or "").strip().lower()
        if not key:
            continue
        g = groups.setdefault(
            key, {"message_text": e["message_text"], "count": 0, "last_seen": e["created_at"]}
        )
        g["count"] += 1
        if e["created_at"] > g["last_seen"]:
            g["last_seen"] = e["created_at"]

    ranked = sorted(groups.values(), key=lambda g: (g["count"], g["last_seen"]), reverse=True)
    return ranked[:limit]


async def get_ambiguous_queries(days: int = 30, limit: int = 50) -> list[dict]:
    """Recent queries that matched 2+ perfumes — where catalog keywords collide."""
    client = _require_client()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    events = await asyncio.to_thread(_fetch_events_since, client, since)

    ambiguous = [e for e in events if e.get("ambiguous")]
    ambiguous.sort(key=lambda e: e["created_at"], reverse=True)
    return [
        {"message_text": e["message_text"], "created_at": e["created_at"]}
        for e in ambiguous[:limit]
    ]


async def get_catalog_stats() -> dict:
    """
    Catalog composition stats — computed straight from the live in-memory
    catalog, no DB round-trip needed.
    """
    from app.catalog import PERFUMES

    total = len(PERFUMES)
    with_clone = sum(1 for p in PERFUMES.values() if p.get("clone_of"))

    tier_stats: dict[str, dict] = {}
    for p in PERFUMES.values():
        for tier, price in (p.get("prices") or {}).items():
            s = tier_stats.setdefault(tier, {"count": 0, "min": price, "max": price, "sum": 0})
            s["count"] += 1
            s["min"] = min(s["min"], price)
            s["max"] = max(s["max"], price)
            s["sum"] += price

    price_tiers = [
        {
            "tier": tier,
            "count": s["count"],
            "min": s["min"],
            "max": s["max"],
            "avg": round(s["sum"] / s["count"]) if s["count"] else 0,
        }
        for tier, s in sorted(tier_stats.items())
    ]

    # "brand" is only present on entries produced by the catalog upload
    # pipeline (app/catalog_upload.py) — older/seed entries won't have it
    # until the catalog is next retrained, so this gracefully returns None
    # rather than guessing a brand out of the display name.
    brand_counts = Counter(p["brand"] for p in PERFUMES.values() if p.get("brand"))
    brand_breakdown = (
        [{"brand": b, "count": c} for b, c in brand_counts.most_common(30)]
        if brand_counts
        else None
    )

    missing_full_bottle = sum(
        1 for p in PERFUMES.values() if not any("full" in k for k in (p.get("prices") or {}))
    )

    return {
        "total_perfumes": total,
        "clones": with_clone,
        "originals": total - with_clone,
        "missing_full_bottle": missing_full_bottle,
        "price_tiers": price_tiers,
        "brand_breakdown": brand_breakdown,
    }
