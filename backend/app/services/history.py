"""My Tube watch history service (Phase 2).

Dual-history semantics:
- My Tube history (watch_sessions + aggregated watch_history) is fully controlled here.
- `playback_source='external_youtube'` records that the user initiated/opened YouTube
  from My Tube. It NEVER implies confirmed YouTube account watch history.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.db.models import Channel, Video, WatchHistory, WatchSession, now_ms

VALID_SOURCES = {"embedded", "external_youtube"}
VALID_STATUSES = {"active", "completed", "abandoned"}


class HistoryError(ValueError):
    pass


def _clamp_nonneg(value: float | int | None, *, limit: float | None = None) -> float | None:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError) as exc:
        raise HistoryError("invalid numeric value") from exc
    if v != v:  # NaN
        raise HistoryError("invalid numeric value")
    if v < 0:
        v = 0.0
    if limit is not None and limit >= 0 and v > limit:
        # allow slight overrun tolerance for client clocks, but clamp hard beyond 10%
        if v <= limit * 1.1 + 5:
            v = float(limit)
        else:
            v = float(limit)
    return v


def _resolve_video(session: Session, video_id: str) -> tuple[Video, Channel | None]:
    if not video_id or not isinstance(video_id, str):
        raise HistoryError("video_id is required")
    v = session.scalar(select(Video).where(Video.yt_id == video_id))
    if v is None:
        raise HistoryError("video not found")
    ch = session.get(Channel, v.channel_id) if v.channel_id else None
    return v, ch


def _get_or_create_aggregate(session: Session, video: Video, channel: Channel | None, ts: int) -> WatchHistory:
    row = session.get(WatchHistory, video.id)
    if row is None:
        row = WatchHistory(
            video_id=video.id,
            channel_id=channel.id if channel else None,
            watched_at=ts,
            first_watched_at=ts,
            last_watched_at=ts,
            engagement=1.0,
            position_s=0.0,
            completed=0,
            total_watch_s=0.0,
            watch_count=0,
        )
        session.add(row)
        session.flush()
    else:
        if row.first_watched_at is None:
            row.first_watched_at = row.watched_at or ts
        if row.last_watched_at is None:
            row.last_watched_at = row.watched_at or ts
        if row.channel_id is None and channel is not None:
            row.channel_id = channel.id
        if row.total_watch_s is None:
            row.total_watch_s = 0.0
        if row.watch_count is None:
            row.watch_count = 0
    return row


def start_session(
    session: Session,
    *,
    video_id: str,
    playback_source: str = "embedded",
    session_id: str | None = None,
    position_s: float | None = None,
    duration_s: float | None = None,
    meta: dict | None = None,
) -> WatchSession:
    if playback_source not in VALID_SOURCES:
        raise HistoryError("invalid playback_source")
    ts = now_ms()
    video, channel = _resolve_video(session, video_id)
    limit = _clamp_nonneg(duration_s, limit=None)
    if limit is None and video.duration_s:
        limit = float(video.duration_s)
    pos = _clamp_nonneg(position_s, limit=limit) or 0.0

    sid = (session_id or "").strip() or uuid.uuid4().hex[:16]
    existing = session.get(WatchSession, sid)
    if existing is not None:
        # idempotent: if same video/source, return/refresh; if different video, reject
        if existing.video_id != video.id:
            raise HistoryError("session_id already used for a different video")
        existing.updated_at = ts
        if existing.status == "active":
            existing.position_s = max(float(existing.position_s or 0), pos)
        session.flush()
        return existing

    ws = WatchSession(
        id=sid,
        video_id=video.id,
        channel_id=channel.id if channel else None,
        started_at=ts,
        updated_at=ts,
        position_s=pos,
        duration_s=limit,
        watched_s=0.0,
        completed=0,
        playback_source=playback_source,
        status="active",
        external_initiated_at=ts if playback_source == "external_youtube" else None,
        meta_json=json.dumps(meta, separators=(",", ":")) if meta else None,
    )
    session.add(ws)

    agg = _get_or_create_aggregate(session, video, channel, ts)
    agg.watch_count = int(agg.watch_count or 0) + 1
    agg.watched_at = ts
    agg.last_watched_at = ts
    agg.last_session_id = sid
    agg.playback_source = playback_source
    agg.position_s = max(float(agg.position_s or 0), pos)
    if playback_source == "external_youtube":
        # Record initiation only. NOT confirmed YouTube account history.
        agg.external_initiated_at = ts
    session.flush()
    return ws


def record_progress(
    session: Session,
    *,
    session_id: str,
    position_s: float | None = None,
    watched_delta_s: float | None = None,
    duration_s: float | None = None,
    completed: bool | None = None,
) -> WatchSession:
    ws = session.get(WatchSession, (session_id or "").strip())
    if ws is None:
        raise HistoryError("session not found")
    ts = now_ms()
    limit = _clamp_nonneg(duration_s, limit=None)
    if limit is None:
        limit = ws.duration_s
    if limit is not None:
        ws.duration_s = limit

    if position_s is not None:
        ws.position_s = _clamp_nonneg(position_s, limit=ws.duration_s) or 0.0
    if watched_delta_s is not None:
        delta = _clamp_nonneg(watched_delta_s, limit=86400.0) or 0.0  # sanity cap per event
        ws.watched_s = float(ws.watched_s or 0.0) + delta
        if ws.duration_s is not None:
            ws.watched_s = min(float(ws.watched_s), float(ws.duration_s) * 1.1)

    if completed is True:
        # For external_youtube, only accept explicit completion signals; still "My Tube" state.
        ws.completed = 1
        ws.status = "completed"
        ws.ended_at = ws.ended_at or ts

    ws.updated_at = ts

    agg = session.get(WatchHistory, ws.video_id)
    if agg is not None:
        agg.watched_at = ts
        agg.last_watched_at = ts
        agg.position_s = max(float(agg.position_s or 0), float(ws.position_s or 0))
        agg.total_watch_s = max(float(agg.total_watch_s or 0), float(ws.watched_s or 0))
        agg.last_session_id = ws.id
        agg.playback_source = ws.playback_source
        if ws.completed:
            agg.completed = 1
            agg.engagement = max(float(agg.engagement or 1.0), 1.0)
    session.flush()
    return ws


def complete_session(
    session: Session,
    *,
    session_id: str,
    position_s: float | None = None,
    watched_s: float | None = None,
    completed: bool = True,
) -> WatchSession:
    ws = session.get(WatchSession, (session_id or "").strip())
    if ws is None:
        raise HistoryError("session not found")
    ts = now_ms()
    if position_s is not None:
        ws.position_s = _clamp_nonneg(position_s, limit=ws.duration_s) or float(ws.position_s or 0)
    if watched_s is not None:
        w = _clamp_nonneg(watched_s, limit=(float(ws.duration_s) * 1.1) if ws.duration_s else 86400.0)
        if w is not None:
            ws.watched_s = max(float(ws.watched_s or 0), w)
    ws.completed = 1 if completed else 0
    ws.status = "completed" if completed else "abandoned"
    ws.ended_at = ts
    ws.updated_at = ts

    agg = session.get(WatchHistory, ws.video_id)
    if agg is not None:
        agg.watched_at = ts
        agg.last_watched_at = ts
        agg.position_s = max(float(agg.position_s or 0), float(ws.position_s or 0))
        agg.total_watch_s = max(float(agg.total_watch_s or 0), float(ws.watched_s or 0))
        agg.last_session_id = ws.id
        agg.playback_source = ws.playback_source
        if completed:
            agg.completed = 1
            agg.engagement = max(float(agg.engagement or 1.0), 1.2)
    session.flush()
    return ws


def get_history(
    session: Session,
    *,
    limit: int = 100,
    cursor: str | None = None,
    channel_id: str | None = None,
) -> tuple[list[dict], str | None]:
    from app.repositories.library import decode_cursor, encode_cursor
    q = (
        select(WatchHistory, Video, Channel)
        .join(Video, Video.id == WatchHistory.video_id)
        .outerjoin(Channel, Channel.id == Video.channel_id)
    )
    if channel_id:
        ch = session.scalar(select(Channel).where(Channel.yt_id == channel_id))
        if ch:
            q = q.where(WatchHistory.channel_id == ch.id)
        else:
            return [], None
    if cursor:
        try:
            state = decode_cursor(cursor)
            if "before" in state:
                q = q.where(func.coalesce(WatchHistory.last_watched_at, WatchHistory.watched_at) < int(state["before"]))
        except Exception:
            pass
    q = q.order_by(func.coalesce(WatchHistory.last_watched_at, WatchHistory.watched_at).desc()).limit(limit + 1)
    rows = list(session.execute(q).all())
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1][0]
        ts = last.last_watched_at or last.watched_at
        if ts is not None:
            next_cursor = encode_cursor({"before": int(ts)})
    return [_history_row(h, v, c) for h, v, c in rows], next_cursor


def get_video_history(session: Session, video_id: str) -> dict | None:
    v = session.scalar(select(Video).where(Video.yt_id == video_id))
    if v is None:
        return None
    h = session.get(WatchHistory, v.id)
    if h is None:
        return None
    c = session.get(Channel, v.channel_id) if v.channel_id else None
    return _history_row(h, v, c)


def delete_video_history(session: Session, video_id: str) -> bool:
    v = session.scalar(select(Video).where(Video.yt_id == video_id))
    if v is None:
        return False
    h = session.get(WatchHistory, v.id)
    # also remove sessions for this video
    sessions = list(session.scalars(select(WatchSession).where(WatchSession.video_id == v.id)))
    for s in sessions:
        session.delete(s)
    if h is None and not sessions:
        return False
    if h is not None:
        session.delete(h)
    session.flush()
    return True


def clear_all_history(session: Session) -> dict:
    sc = int(session.query(WatchSession).delete(synchronize_session=False) or 0)
    hc = int(session.query(WatchHistory).delete(synchronize_session=False) or 0)
    session.flush()
    return {"sessions_deleted": sc, "history_deleted": hc}


def _history_row(h: WatchHistory, v: Video, c: Channel | None) -> dict:
    return {
        "video_id": v.yt_id,
        "channel_id": c.yt_id if c else None,
        "title": v.title,
        "channel_name": c.name if c else None,
        "first_watched_at": h.first_watched_at or h.watched_at,
        "last_watched_at": h.last_watched_at or h.watched_at,
        "position_s": h.position_s,
        "total_watch_s": h.total_watch_s,
        "completed": bool(h.completed),
        "watch_count": int(h.watch_count or 0),
        "playback_source": h.playback_source,
        "last_session_id": h.last_session_id,
        # Truthful dual-history fields:
        "mytube_recorded": True,
        "external_youtube_initiated_at": h.external_initiated_at,
        "youtube_history_confirmed": False,  # never assumed/verified via supported APIs
    }
