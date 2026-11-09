"""Primary API surface for the MyTube backend."""

from __future__ import annotations

import json
from datetime import datetime, timezone
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, request_id
from app.config import Settings, get_settings
from app.db.models import BackgroundJob, Channel, Category, Video
from app.repositories.library import (
    list_channels,
    list_videos,
    stats as lib_stats,
    channel_by_yt_id,
    video_by_yt_id,
)
from app.services.importer import import_channels
from app.repositories.user_state import export_state
from app.schemas import (
    ChannelSummary,
    JobStatus,
    HistoryCompleteRequest,
    HistoryItemOut,
    HistoryListOut,
    HistoryProgressRequest,
    HistorySessionOut,
    HistoryStartRequest,
    KnobUpdate,
    LocalStorageState,
    RecommendationItem,
    RecommendationOut,
    RecommendationRequest,
    SearchHitOut,
    SearchResultOut,
    StatBlock,
    UserSettingsOut,
    VideoFilterRequest,
    VideoSummary,
)
from app.services.recommendations import recommend
from app.services.search import search_videos
from app.services import history as history_svc
from app.services.history import HistoryError
from app.services.jobs_service import JobService
from app.services.jobs import ensure_default_handlers_registered, run_job

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["v1"])


@router.get("/health", tags=["system"])
def health() -> dict:
    return {"status": "ok"}


@router.get("/settings", tags=["system"], response_model=UserSettingsOut)
def settings_view() -> UserSettingsOut:
    s = get_settings()
    return UserSettingsOut(
        data_dir=str(s.data_dir),
        host=s.host,
        port=s.port,
        requires_auth=s.requires_auth,
    )


@router.get("/channels", tags=["catalog"])
def channels(
    request: Request,
    *,
    limit: int = Query(50, ge=1, le=500),
    cursor: str | None = None,
    query: str | None = None,
    sort: str = Query("name", pattern="^(name|recent|subscriber_count)$"),
):
    session = get_db(request)
    rows, next_cursor = list_channels(
        session,
        library_id=1,
        limit=limit,
        cursor=cursor,
        query=query,
        sort=sort,
    )
    return {
        "items": [_channel_summary(r) for r in rows],
        "next_cursor": next_cursor,
    }


@router.get("/channels/{channel_id}")
def channel_detail(
    request: Request,
    channel_id: str,
):
    session = get_db(request)
    ch = channel_by_yt_id(session, channel_id)
    if ch is None:
        raise HTTPException(status_code=404, detail="channel not found")
    videos, _cursor = list_videos(
        session,
        channel_id=ch.id,
        limit=50,
        cursor=None,
        available_only=True,
        sort="recent",
    )
    from sqlalchemy import func as _func
    video_count = int(session.scalar(select(_func.count(Video.id)).where(Video.channel_id == ch.id)) or 0)
    return {
        "id": ch.yt_id,
        "name": ch.name,
        "handle": ch.handle,
        "avatar_url": ch.avatar_url,
        "description": ch.description,
        "subscriber_count": ch.subscriber_count,
        "video_count": video_count,
        "videos": [_video_summary(v, ch) for v, _c in videos[:200]],
    }


@router.get("/videos", tags=["catalog"])
def videos(
    request: Request,
    *,
    limit: int = Query(50, ge=1, le=500),
    cursor: str | None = None,
    channel: str | None = None,
    duration_min_s: int | None = Query(None, ge=0),
    duration_max_s: int | None = Query(None, ge=0),
    sort: str = Query("recent", pattern="^(recent|oldest|views|duration)$"),
):
    session = get_db(request)
    rows, next_cursor = list_videos(
        session,
        limit=limit,
        cursor=cursor,
        channel_id=_channel_id_for(session, channel),
        min_duration=duration_min_s,
        max_duration=duration_max_s,
        sort=sort,
    )
    return {
        "items": [_video_summary(v, c) for v, c in rows],
        "next_cursor": next_cursor,
    }


@router.get("/videos/{video_id}")
def video_detail(
    request: Request,
    video_id: str,
):
    session = get_db(request)
    v = video_by_yt_id(session, video_id)
    if v is None:
        raise HTTPException(status_code=404, detail="video not found")
    ch = channel_by_yt_id(session, v.channel_id) if v.channel_id else None
    return _video_summary(v, ch)


@router.get("/search", tags=["search"])
def search(
    request: Request,
    *,
    q: str = Query(..., min_length=1),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = None,
):
    session = get_db(request)
    hits, next_cursor, total = search_videos(session, q, limit=limit, cursor=cursor)
    return SearchResultOut(query=q, hits=hits, next_cursor=next_cursor, count=total)


@router.get("/stats", tags=["catalog"])
def stats(
    request: Request,
):
    session = get_db(request)
    return StatBlock(**lib_stats(session, library_id=1))


@router.get("/categories", tags=["catalog"])
def categories(
    request: Request,
):
    from sqlalchemy import func as _func
    session = get_db(request)
    rows = session.scalars(select(Category).order_by(Category.position)).all()
    out: list[dict] = []
    for cat in rows:
        count = int(
            session.scalar(
                select(_func.count(Video.id))
                .select_from(Video)
                .join(Channel, Channel.id == Video.channel_id)
                .where(Channel.category_id == cat.id, Video.is_available == 1)
            )
            or 0
        )
        out.append({"slug": cat.slug, "name": cat.name, "count": count})
    return {"categories": out}


@router.post("/recommends", tags=["recommendations"], response_model=RecommendationOut)
def recommends(
    request: Request,
    body: RecommendationRequest,
):
    from app.services.recommendations import DefaultExplainer
    session = get_db(request)
    items = recommend(
        session,
        user_id=body.user_id,
        count=body.count,
        filters=body.filters,
        knobs=body.knobs,
        diversity=body.diversity,
        channel_cap=body.channel_cap,
    )
    explainer = DefaultExplainer()
    return RecommendationOut(
        user_id=body.user_id,
        items=[
            RecommendationItem(
                id=i.video.yt_id,
                title=i.video.title,
                duration_s=i.video.duration_s,
                view_count=i.video.view_count,
                thumbnail_url=i.video.thumbnail_url,
                channel=_channel_summary(i.channel) if i.channel else None,
                score=round(i.score, 3),
                reason=explainer.explain(i, i.features),
            )
            for i in items
        ],
        model="baseline",
    )


def _dt(ms):
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc) if ms else None

def _job_out(job: BackgroundJob):
    return JobStatus(
        id=job.id,
        type=job.type,
        status=job.status,
        progress=float(job.progress or 0),
        stage=job.stage,
        result=json.loads(job.result_json) if job.result_json else None,
        error=job.error,
        created_at=_dt(job.created_at),
        started_at=_dt(job.started_at),
    )


@router.post("/jobs/sync", tags=["jobs"])
def job_sync(request: Request, body: dict):
    """Enqueue an incremental sync/import job (Phase 5, local-verified).

    `sync_import`: idempotent upsert from supplied `channels`/`source_path` (no
    network, no undocumented YouTube APIs). `sync_ytdlp`: truthful adapter hook.
    """
    session = get_db(request)
    ensure_default_handlers_registered()
    job_type = body.get("type") or "sync_import"
    if job_type not in ("sync_import", "sync_ytdlp", "noop"):
        raise HTTPException(status_code=400, detail=f"unsupported job type: {job_type}")
    payload = dict(body)
    payload.pop("type", None)
    svc = JobService(session)
    job = svc.enqueue(job_type, payload, priority=int(body.get("priority") or 5), max_attempts=int(body.get("max_attempts") or 3))
    session.commit()
    return _job_out(job)


@router.get("/jobs/{job_id}", tags=["jobs"])
def job_status(request: Request, job_id: str):
    session = get_db(request)
    job = session.get(BackgroundJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _job_out(job)


@router.post("/jobs/{job_id}/cancel", tags=["jobs"])
def job_cancel(request: Request, job_id: str):
    session = get_db(request)
    svc = JobService(session)
    job = svc.request_cancel(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    session.commit()
    return _job_out(job)


@router.post("/jobs/{job_id}/retry", tags=["jobs"])
def job_retry(request: Request, job_id: str):
    session = get_db(request)
    svc = JobService(session)
    job = svc.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status not in ("failed", "canceled", "done"):
        raise HTTPException(status_code=400, detail=f"cannot retry job in status {job.status}")
    job.status = "queued"
    job.error = None
    job.worker_id = None
    job.started_at = None
    job.finished_at = None
    job.cancel_requested = 0
    job.attempts = 0
    job.progress = 0.0
    job.stage = "retry_queued"
    session.commit()
    return _job_out(job)


@router.get("/jobs", tags=["jobs"])
def job_list(request: Request, status: str = Query("active"), limit: int = Query(50, ge=1, le=200)):
    session = get_db(request)
    if status == "active":
        rows = session.scalars(select(BackgroundJob).where(BackgroundJob.status.in_(["queued", "running", "restarted"]))).all()
    else:
        rows = session.scalars(select(BackgroundJob).where(BackgroundJob.status == status)).all()
    rows = sorted(rows, key=lambda j: (j.priority, j.created_at), reverse=False)[:limit]
    return {"jobs": [_job_out(j) for j in rows]}


@router.get("/user/state", tags=["user"])
def user_state(
    request: Request,
):
    session = get_db(request)
    return export_state(session)


@router.post("/user/state", tags=["user"])
def user_state_apply(
    request: Request,
    body: LocalStorageState,
):
    """Phase 6: safe localStorage migration/import (best-effort, idempotent mapping).
    Imports only entities that exist in DB; never marks YouTube history as confirmed."""
    session = get_db(request)
    from app.repositories.user_state import import_state
    raw = body.model_dump(by_alias=True, exclude_unset=False)
    counts = import_state(session, raw)
    try:
        session.commit()
    except Exception:
        session.rollback(); raise
    return {"applied": counts, "keys": list(raw.keys())}


def _channel_id_for(session: Session, yt_id: str | None) -> int | None:
    if not yt_id:
        return None
    ch = channel_by_yt_id(session, yt_id)
    return ch.id if ch else None


def _channel_summary(ch: Channel) -> ChannelSummary:
    return ChannelSummary(
        id=ch.yt_id,
        name=ch.name,
        handle=ch.handle,
        avatar_url=ch.avatar_url,
        subscriber_count=ch.subscriber_count,
        video_count=int(
            ch.video_count if hasattr(ch, "video_count") and ch.video_count else 0
        ),
    )


def _video_summary(v: Video, ch: Channel | None) -> VideoSummary:
    return VideoSummary(
        id=v.yt_id,
        title=v.title,
        duration_s=v.duration_s,
        view_count=v.view_count,
        published_at=v.published_at,
        thumbnail_url=v.thumbnail_url,
        channel=_channel_summary(ch) if ch else None,
    )


def _search_hit(hit, session: Session) -> SearchHitOut:
    ch = session.get(Channel, hit.channel_id) if hit.channel_id else None
    return SearchHitOut(
        id=hit.video_id,
        title=hit.title,
        duration_s=hit.duration_s,
        view_count=hit.view_count,
        published_at=hit.published_at,
        snippet=hit.snippet,
        rank=round(hit.rank, 3),
        channel=_channel_summary(ch) if ch else None,
    )


# ------------------------------------------------------------------ watch history (Phase 2)

def _history_session_out(ws):
    v = None
    try:
        v = ws.video_id
    except Exception:
        pass
    return HistorySessionOut(
        session_id=ws.id,
        video_id=int(ws.video_id),
        channel_id=int(ws.channel_id) if ws.channel_id is not None else None,
        status=ws.status,
        playback_source=ws.playback_source,
        position_s=float(ws.position_s or 0),
        watched_s=float(ws.watched_s or 0),
        completed=bool(ws.completed),
        started_at=int(ws.started_at),
        updated_at=int(ws.updated_at),
        ended_at=int(ws.ended_at) if ws.ended_at is not None else None,
        external_initiated_at=int(ws.external_initiated_at) if ws.external_initiated_at else None,
        youtube_history_confirmed=False,
    )


@router.post("/history/start", tags=["history"], response_model=HistorySessionOut)
def history_start(request: Request, body: HistoryStartRequest):
    session = get_db(request)
    try:
        ws = history_svc.start_session(
            session,
            video_id=body.video_id,
            playback_source=body.playback_source,
            session_id=body.session_id,
            position_s=body.position_s,
            duration_s=body.duration_s,
            meta=body.meta,
        )
        session.commit()
        return _history_session_out(ws)
    except HistoryError as exc:
        session.rollback()
        msg = str(exc)
        code = 404 if "not found" in msg else 400
        raise HTTPException(status_code=code, detail=msg) from exc


@router.post("/history/progress", tags=["history"], response_model=HistorySessionOut)
def history_progress(request: Request, body: HistoryProgressRequest):
    session = get_db(request)
    try:
        ws = history_svc.record_progress(
            session,
            session_id=body.session_id,
            position_s=body.position_s,
            watched_delta_s=body.watched_delta_s,
            duration_s=body.duration_s,
            completed=body.completed,
        )
        session.commit()
        return _history_session_out(ws)
    except HistoryError as exc:
        session.rollback()
        msg = str(exc)
        code = 404 if "not found" in msg else 400
        raise HTTPException(status_code=code, detail=msg) from exc


@router.post("/history/complete", tags=["history"], response_model=HistorySessionOut)
def history_complete(request: Request, body: HistoryCompleteRequest):
    session = get_db(request)
    try:
        ws = history_svc.complete_session(
            session,
            session_id=body.session_id,
            position_s=body.position_s,
            watched_s=body.watched_s,
            completed=body.completed,
        )
        session.commit()
        return _history_session_out(ws)
    except HistoryError as exc:
        session.rollback()
        msg = str(exc)
        code = 404 if "not found" in msg else 400
        raise HTTPException(status_code=code, detail=msg) from exc


@router.get("/history", tags=["history"], response_model=HistoryListOut)
def history_list(
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    cursor: str | None = None,
    channel_id: str | None = None,
):
    session = get_db(request)
    items, next_cursor = history_svc.get_history(session, limit=limit, cursor=cursor, channel_id=channel_id)
    return HistoryListOut(items=[HistoryItemOut(**it) for it in items], next_cursor=next_cursor)


@router.get("/history/{video_id}", tags=["history"])
def history_get(request: Request, video_id: str):
    session = get_db(request)
    item = history_svc.get_video_history(session, video_id)
    if item is None:
        raise HTTPException(status_code=404, detail="history not found")
    return HistoryItemOut(**item)


@router.delete("/history/{video_id}", tags=["history"])
def history_delete_video(request: Request, video_id: str):
    session = get_db(request)
    ok = history_svc.delete_video_history(session, video_id)
    if not ok:
        session.rollback()
        raise HTTPException(status_code=404, detail="history not found")
    session.commit()
    return {"deleted": True, "video_id": video_id}


@router.delete("/history", tags=["history"])
def history_clear(request: Request):
    # Local-first app; destructive action. Keep behavior explicit.
    session = get_db(request)
    result = history_svc.clear_all_history(session)
    session.commit()
    return {"cleared": True, **result}


# ------------------------------------------------------------------ user preferences (Phase 3)

@router.get("/user/preferences", tags=["user"])
def get_preferences(request: Request):
    from app.db.models import UserPreference
    session = get_db(request)
    rows = session.scalars(select(UserPreference)).all()
    prefs = {}
    for r in rows:
        try:
            prefs[r.key] = json.loads(r.value_json) if r.value_json else None
        except Exception:
            prefs[r.key] = r.value_json
    # defaults
    prefs.setdefault("playback_mode", "embedded")  # 'embedded' | 'external_youtube'
    return {"preferences": prefs}


@router.put("/user/preferences", tags=["user"])
def put_preferences(request: Request, body: dict):
    from app.db.models import UserPreference, now_ms
    session = get_db(request)
    prefs = body.get("preferences") if isinstance(body, dict) else None
    if prefs is None and isinstance(body, dict):
        prefs = body
    if not isinstance(prefs, dict):
        raise HTTPException(status_code=400, detail="preferences object required")
    allowed = {"playback_mode"}
    applied = {}
    for k, v in prefs.items():
        if k not in allowed:
            continue
        if k == "playback_mode" and v not in ("embedded", "external_youtube"):
            raise HTTPException(status_code=400, detail="invalid playback_mode")
        row = session.get(UserPreference, k)
        if row is None:
            row = UserPreference(key=k, value_json=json.dumps(v), updated_at=now_ms())
            session.add(row)
        else:
            row.value_json = json.dumps(v)
            row.updated_at = now_ms()
        applied[k] = v
    session.commit()
    return {"preferences": applied}
