"""
Owner-only dashboard API — analytics + catalog upload/retrain endpoints.

Every route here requires a valid Supabase-authenticated owner session
(see app/auth.py). Mounted under /api/admin in app/main.py.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile

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
    create_pending_version,
    discard_version,
    get_version_detail,
    list_versions,
    publish_version,
    rollback_version,
)

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
async def publish_catalog_version(version_id: int):
    """Make a pending version live — writes catalog_data.json and hot-reloads the running bot."""
    try:
        return await asyncio.to_thread(publish_version, version_id)
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
