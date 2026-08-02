"""
Owner-only dashboard API — analytics + catalog upload/retrain endpoints.

Every route here requires a valid Supabase-authenticated owner session
(see app/auth.py). Mounted under /api/admin in app/main.py.
"""

import asyncio
import logging

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile

from app.analytics import (
    AnalyticsUnavailable,
    get_ambiguous_queries,
    get_catalog_stats,
    get_overview,
    get_timeseries,
    get_top_perfumes,
    get_unmatched_queries,
)
from app.auth import require_owner
from app.catalog_upload import (
    CatalogParseError,
    CatalogRemovalWarning,
    create_pending_version,
    discard_version,
    get_version_detail,
    list_versions,
    publish_version,
    rollback_version,
)
from app.handoff import admin_get_pause_duration_hours, admin_set_pause_duration_hours

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_owner)])


def _days_param(days: int = Query(default=30, ge=1, le=365)) -> int:
    return days


@router.get("/metrics/overview")
async def metrics_overview(days: int = Depends(_days_param)):
    try:
        return await get_overview(days)
    except AnalyticsUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/metrics/timeseries")
async def metrics_timeseries(days: int = Depends(_days_param)):
    try:
        return await get_timeseries(days)
    except AnalyticsUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/metrics/top-perfumes")
async def metrics_top_perfumes(
    days: int = Depends(_days_param), limit: int = Query(default=15, ge=1, le=100)
):
    try:
        return await get_top_perfumes(days, limit)
    except AnalyticsUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/metrics/unmatched")
async def metrics_unmatched(
    days: int = Depends(_days_param), limit: int = Query(default=50, ge=1, le=200)
):
    try:
        return await get_unmatched_queries(days, limit)
    except AnalyticsUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/metrics/ambiguous")
async def metrics_ambiguous(
    days: int = Depends(_days_param), limit: int = Query(default=50, ge=1, le=200)
):
    try:
        return await get_ambiguous_queries(days, limit)
    except AnalyticsUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/metrics/catalog-stats")
async def metrics_catalog_stats():
    return await get_catalog_stats()


# --- Settings ---------------------------------------------------------------

@router.get("/settings/handoff")
async def get_handoff_settings():
    """Current human-handoff pause duration (see app.handoff) — how long
    the bot stays out of a conversation after the owner personally messages
    that customer directly."""
    try:
        hours = await admin_get_pause_duration_hours()
    except AnalyticsUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"pause_hours": hours}


@router.put("/settings/handoff")
async def set_handoff_settings(pause_hours: float = Body(embed=True)):
    if not (0 < pause_hours <= 720):
        raise HTTPException(status_code=400, detail="pause_hours must be between 0 and 720 (30 days)")
    try:
        hours = await admin_set_pause_duration_hours(pause_hours)
    except AnalyticsUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"pause_hours": hours}


@router.get("/catalog")
async def list_catalog(
    q: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """Search/paginate the live catalog — lets the owner sanity-check what's active."""
    from app.catalog import PERFUMES

    needle = q.lower().strip()
    items = [
        {"perfume_id": pid, **data}
        for pid, data in PERFUMES.items()
        if not needle
        or needle in data.get("display_name", "").lower()
        or needle in pid.lower()
    ]
    items.sort(key=lambda i: i["display_name"])
    total = len(items)
    return {"total": total, "items": items[offset : offset + limit]}


# --- Catalog retrain pipeline ---------------------------------------------

@router.post("/catalog/upload")
async def upload_catalog(file: UploadFile = File(...)):
    """Parse an uploaded sheet and store it as a pending version — does NOT touch the live catalog."""
    content = await file.read()
    try:
        return await asyncio.to_thread(create_pending_version, file.filename, content)
    except CatalogParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except AnalyticsUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/catalog/versions")
async def get_versions(limit: int = Query(default=30, ge=1, le=200)):
    try:
        return await asyncio.to_thread(list_versions, limit)
    except AnalyticsUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/catalog/versions/{version_id}")
async def get_version(version_id: int):
    try:
        return await asyncio.to_thread(get_version_detail, version_id)
    except CatalogParseError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except AnalyticsUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/catalog/versions/{version_id}/publish")
async def publish_catalog_version(
    version_id: int, confirm_removals: bool = Query(default=False)
):
    """
    Make a pending version live — writes catalog_data.json and hot-reloads
    the running bot.

    Answers 409 rather than 400 when the version would delete a large part
    of the catalog: the request is well-formed, it just conflicts with what
    is already there, and the client is expected to show the number and
    re-send with confirm_removals=true. See catalog_upload.MAX_SILENT_REMOVALS.
    """
    try:
        return await asyncio.to_thread(publish_version, version_id, confirm_removals)
    except CatalogRemovalWarning as exc:
        raise HTTPException(
            status_code=409,
            detail={"message": str(exc), "removed": exc.removed, "needs_confirmation": True},
        )
    except CatalogParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except AnalyticsUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/catalog/versions/{version_id}/discard")
async def discard_catalog_version(version_id: int):
    try:
        await asyncio.to_thread(discard_version, version_id)
    except AnalyticsUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"status": "discarded"}


@router.post("/catalog/versions/{version_id}/rollback")
async def rollback_catalog_version(version_id: int):
    """Re-activate a previously-published version."""
    try:
        return await asyncio.to_thread(rollback_version, version_id)
    except CatalogParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except AnalyticsUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))


# --- Catalog editing --------------------------------------------------------
#
# Separate from the version/publish pipeline above on purpose: that path is
# for replacing the whole catalog from a sheet and is worth its ceremony,
# while these are for the owner who just got a bottle in and wants it
# sellable now. See app.catalog_edit.


@router.get("/catalog/brands")
async def catalog_brands():
    """Brands already in the catalog, for the add form's autocomplete — so a
    new perfume joins "Ahmed Al Maghribi" instead of founding a misspelling
    of it next door."""
    from app.catalog_edit import DECANT_SIZES, known_brands

    return {"brands": known_brands(), "sizes": DECANT_SIZES}


@router.post("/catalog/check-duplicate")
async def catalog_check_duplicate(
    display_name: str = Body(embed=True), ignore_id: str | None = Body(default=None, embed=True)
):
    """Live check while the owner types, so a duplicate is caught before the
    form is filled in rather than after."""
    from app.catalog_edit import find_duplicate

    hit = find_duplicate(display_name, ignore_id=ignore_id)
    if hit is None:
        return {"duplicate": False}
    return {"duplicate": True, "perfume_id": hit.perfume_id, "display_name": hit.display_name}


@router.post("/catalog/perfume")
async def catalog_add_perfume(payload: dict = Body(...)):
    from app.catalog_edit import CatalogEditError, add_perfume

    try:
        return await asyncio.to_thread(
            add_perfume,
            payload.get("brand", ""),
            payload.get("name", ""),
            payload.get("clone_of"),
            payload.get("prices") or {},
        )
    except CatalogEditError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.put("/catalog/perfume/{perfume_id}")
async def catalog_update_perfume(perfume_id: str, payload: dict = Body(...)):
    from app.catalog_edit import CatalogEditError, update_perfume

    try:
        return await asyncio.to_thread(
            update_perfume,
            perfume_id,
            payload.get("brand"),
            payload.get("name"),
            payload.get("clone_of"),
            payload.get("prices"),
        )
    except CatalogEditError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/catalog/delete")
async def catalog_delete(perfume_ids: list[str] = Body(embed=True)):
    """Bulk delete. POST rather than DELETE so a list of ids travels in the
    body instead of a URL with a length limit."""
    from app.catalog_edit import CatalogEditError, delete_perfumes

    try:
        return await asyncio.to_thread(delete_perfumes, perfume_ids)
    except CatalogEditError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/catalog/add-many")
async def catalog_add_many(entries: list[dict] = Body(embed=True)):
    """Add a reviewed batch — the "add these 43" action behind an upload's
    list of full-bottle-only perfumes. Returns what went in and what was
    skipped, per item."""
    from app.catalog_edit import CatalogEditError, add_many

    try:
        return await asyncio.to_thread(add_many, entries)
    except CatalogEditError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# --- The bot's wording ------------------------------------------------------


@router.get("/messages")
async def get_messages():
    from app import messages

    return {
        "messages": messages.current(),
        "defaults": messages.defaults(),
        "templates": messages.TEMPLATES,
        "confirmed_emoji": sorted(messages.CONFIRMED_EMOJI),
        "max_chars": messages.MAX_MESSAGE_CHARS,
    }


@router.post("/messages/validate")
async def validate_messages(payload: dict = Body(...)):
    """Checked as the owner types. Chat Mitra does not fail loudly on a
    message it dislikes — it returns 2xx and never delivers — so the only
    place a bad edit can be caught is before it is saved."""
    from app import messages

    return messages.validate_all(payload.get("messages") or {})


@router.put("/messages")
async def put_messages(payload: dict = Body(...)):
    from app import messages

    try:
        return {"messages": await asyncio.to_thread(messages.save, payload.get("messages") or {})}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except AnalyticsUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/messages/reset")
async def reset_message(key: str = Body(embed=True)):
    from app import messages

    try:
        return {"messages": await asyncio.to_thread(messages.reset, key)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except AnalyticsUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))


# --- Chat tester ------------------------------------------------------------


@router.post("/simulate")
async def simulate(payload: dict = Body(...)):
    """
    Run a message through the real matching pipeline and return what the bot
    WOULD reply, without sending anything.

    This is what lets the WhatsApp tester live inside the deployed
    dashboard. The old local console got the same effect by monkey-patching
    app.main's module-level send_reply, which its own docstring warned must
    never happen in a process serving live traffic — one stray import and
    real customers stop getting replies. Nothing is patched here: the
    pipeline is called directly and its result is returned instead of sent.

    Conversation context is per tester session, keyed by `sender`, so
    follow-ups like "and 5ml?" resolve the way they would for a customer.
    """
    from app.conversation import record_bot_reply, record_customer_message, recent_turns
    from app.formatter import (
        FALLBACK_MESSAGE,
        build_multi_price_card,
        build_price_card,
    )
    from app.greeting import is_greeting_or_catalog_request
    from app.matcher import match_perfume

    text = (payload.get("text") or "").strip()
    sender = f"__tester__{(payload.get('sender') or 'default').strip()}"
    use_groq = bool(payload.get("use_groq", True))

    if not text:
        return {"reply": None, "layer": "empty", "matched": []}

    history = await recent_turns(sender)
    record_customer_message(sender, text)

    if use_groq:
        result = await match_perfume(text, history=history)
    else:
        # Groq forced off, so the deterministic index alone decides — which
        # is what the bot falls back to whenever the LLM is unreachable, and
        # therefore worth being able to test on purpose.
        from app.config import settings as _settings

        saved = _settings.GROQ_API_KEY
        _settings.GROQ_API_KEY = ""
        try:
            result = await match_perfume(text, history=history)
        finally:
            _settings.GROQ_API_KEY = saved

    if result.matched_perfume_ids:
        reply = build_multi_price_card(result.matched_perfume_ids, result.opening, result.closing)
    elif result.perfume_id:
        reply = build_price_card(result.perfume_id, result.opening, result.closing)
    elif is_greeting_or_catalog_request(text):
        from app.main import _message

        reply = _message("fallback", FALLBACK_MESSAGE)
    else:
        reply = None

    if reply:
        record_bot_reply(sender, reply, result.matched_perfume_ids or ([result.perfume_id] if result.perfume_id else []))

    from app.catalog import PERFUMES

    matched_ids = result.matched_perfume_ids or ([result.perfume_id] if result.perfume_id else [])
    return {
        "reply": reply,
        "layer": result.layer,
        "confidence": result.confidence,
        "llm_unavailable": result.llm_unavailable,
        "matched": [
            {"perfume_id": pid, "display_name": PERFUMES[pid]["display_name"]}
            for pid in matched_ids
            if pid in PERFUMES
        ],
        "scores": [
            {"display_name": PERFUMES[pid]["display_name"], "score": score}
            for pid, score in (result.scores or [])[:12]
            if pid in PERFUMES
        ],
    }


@router.post("/simulate/reset")
async def simulate_reset(sender: str = Body(default="default", embed=True)):
    """Forget the tester's conversation so the next message starts fresh —
    otherwise "and 5ml?" keeps resolving against the last card."""
    from app.conversation import clear

    clear(f"__tester__{(sender or 'default').strip()}")
    return {"status": "cleared"}


@router.get("/status")
async def bot_status():
    """One call for everything the dashboard header shows: whether the bot's
    integrations are actually working, rather than assumed to be."""
    from app.catalog import PERFUMES
    from app.config import settings as _settings
    from app.main import HANDOFF_STATUS

    return {
        "catalog_size": len(PERFUMES),
        "groq_configured": bool(_settings.GROQ_API_KEY),
        "whatsapp_configured": bool(_settings.CHATMITRA_API_TOKEN),
        "webhook_secret_set": bool(_settings.CHATMITRA_WEBHOOK_SECRET),
        "supabase_configured": bool(_settings.SUPABASE_URL and _settings.SUPABASE_SERVICE_ROLE_KEY),
        "handoff_pause": HANDOFF_STATUS,
    }


@router.post("/catalog/bulk-edit")
async def catalog_bulk_edit(payload: dict = Body(...)):
    """Apply one change to many perfumes — see catalog_edit.bulk_update for
    why this takes operations rather than field values."""
    from app.catalog_edit import CatalogEditError, bulk_update

    try:
        return await asyncio.to_thread(
            bulk_update, payload.get("perfume_ids") or [], payload.get("ops") or {}
        )
    except CatalogEditError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/catalog/card")
async def catalog_card(payload: dict = Body(...)):
    """The price card for the selected perfumes, exactly as a customer would
    receive it, for the console's copy button."""
    from app.catalog_edit import CatalogEditError, card_preview

    try:
        return await asyncio.to_thread(
            card_preview, payload.get("perfume_ids") or [], payload.get("sizes")
        )
    except CatalogEditError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
