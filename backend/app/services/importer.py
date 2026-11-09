"""Import service: validates payloads, bulk upserts channels+videos, classifies."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from time import perf_counter

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Category, Channel, Video, now_ms
from app.domain.categories import ChannelText, classify
from app.domain.weights import metadata_hash as compute_hash, _md_hash
import json
import logging
from app.repositories.library import (
    channel_id_map,
    get_or_create_library,
    seed_categories,
)

logger = logging.getLogger(__name__)

CHANNEL_CHUNK = 250
VIDEO_CHUNK = 1000


@dataclass
class ChannelRecord:
    id: str
    name: str
    url: str | None = None
    handle: str | None = None
    avatar_url: str | None = None
    description: str | None = None
    subscriber_count: int | None = None
    videos: list[dict] = field(default_factory=list)


@dataclass
class ImportResult:
    channels_read: int = 0
    channels_upserted: int = 0
    videos_buffered: int = 0
    videos_upserted: int = 0
    videos_before: int = 0
    videos_after: int = 0
    duration_max_s: int = 0
    view_count_max: int = 0
    upsert_duration_s: float = 0.0


def _int_or_none(v):
    if v is None:
        return None
    try:
        n = int(v)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _str_or_none(v):
    if v is None:
        return None
    return str(v).strip() or None


def _parse_channel(raw: dict) -> ChannelRecord:
    videos: list[dict] = []
    for v in raw.get("videos") or []:
        if not isinstance(v, dict):
            continue
        vid = _str_or_none(v.get("id") or v.get("videoId") or v.get("video_id") or v.get("ytid"))
        if not vid:
            continue
        videos.append(
            {
                "id": vid,
                "title": str(v.get("title") or "").strip() or f"Untitled {vid[:8]}",
                "duration_s": _int_or_none(v.get("durationSeconds") or v.get("lengthSeconds") or v.get("duration") or v.get("duration_seconds")),
                "view_count": _int_or_none(v.get("viewCount") or v.get("views") or v.get("viewCountText") or v.get("view_count")),
                "published_at": _int_or_none(v.get("publishedAt") or v.get("publishDate") or v.get("published") or v.get("publishedAt")),
                "description": str(v.get("description") or "").strip() or None,
                "thumbnail_url": _str_or_none(v.get("thumbnail") or v.get("thumbnail_url") or v.get("thumbnails") or v.get("defaultThumbnail") or v.get("default_thumbnail")),
            }
        )
    return ChannelRecord(
        id=_str_or_none(raw.get("id") or raw.get("channelId") or raw.get("cid") or raw.get("channel_id")) or "",
        name=str(raw.get("name") or raw.get("title") or raw.get("displayTitle") or raw.get("titleText") or "").strip() or "",
        url=_str_or_none(raw.get("url") or raw.get("link") or raw.get("channel_url")),
        handle=_str_or_none(raw.get("handle") or raw.get("channelHandle") or raw.get("@")),
        avatar_url=_str_or_none(raw.get("avatar") or raw.get("avatar_url") or raw.get("thumbnails") or raw.get("defaultThumbnail") or raw.get("default_thumbnail_url") or raw.get("thumbnail")),
        description=str(raw.get("description") or raw.get("channelDescription") or "").strip() or None,
        subscriber_count=_int_or_none(raw.get("subscriberCount") or raw.get("subscriber_count") or raw.get("subscriberCountText")),
        videos=videos,
    )


@contextmanager
def _session(session_factory_or_session: sessionmaker[Session] | Session):
    if isinstance(session_factory_or_session, Session):
        session = session_factory_or_session
        should_close = False
    else:
        session = session_factory_or_session()
        should_close = True
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        if should_close:
            session.close()


def import_channels(
    session_factory: Session | sessionmaker[Session],
    payloads: Sequence[dict],
    *,
    library_name: str = "main",
    source_path: str | None = None,
) -> ImportResult:
    """Validate and bulk-insert channels/videos, then classify and commit in one session."""
    start = perf_counter()
    result = ImportResult(channels_read=len(payloads))
    with _session(session_factory) as session:
        library = get_or_create_library(session, library_name, source_path)

        existing_ids = set(
            row[0] for row in session.execute(select(Channel.yt_id).where(Channel.library_id == library.id))
        )
        channels: list[ChannelRecord] = []
        for raw in payloads:
            ch = _parse_channel(raw)
            if not ch.id:
                continue
            if ch.id in existing_ids and not ch.videos:
                continue
            channels.append(ch)

        channels_upserted = 0
        videos_to_insert: list[dict] = []
        for offset in range(0, len(channels), CHANNEL_CHUNK):
            batch = channels[offset : offset + CHANNEL_CHUNK]
            rows = [
                {
                    "library_id": library.id,
                    "yt_id": c.id,
                    "name": c.name,
                    "url": c.url,
                    "handle": c.handle,
                    "avatar_url": c.avatar_url,
                    "description": c.description,
                    "subscriber_count": c.subscriber_count,
                    "first_seen_at": now_ms(),
                    "last_seen_at": now_ms(),
                }
                for c in batch
            ]
            stmt = sqlite_insert(Channel).values(rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=[Channel.yt_id],
                set_={
                    "name": stmt.excluded.name,
                    "url": stmt.excluded.url,
                    "handle": stmt.excluded.handle,
                    "avatar_url": stmt.excluded.avatar_url,
                    "description": stmt.excluded.description,
                    "subscriber_count": stmt.excluded.subscriber_count,
                    "last_seen_at": stmt.excluded.last_seen_at,
                },
            )
            session.execute(stmt)
            channels_upserted += len(rows)
            session.flush()
            channel_map = {
                row[0]: row[1]
                for row in session.execute(
                    select(Channel.yt_id, Channel.id).where(Channel.yt_id.in_([c.id for c in batch]))
                ).all()
            }
            for c in batch:
                cid = channel_map.get(c.id)
                if cid is None:
                    continue
                for v in c.videos:
                    videos_to_insert.append(
                        {
                            "channel_id": cid,
                            "yt_id": v["id"],
                            "title": v["title"],
                            "duration_s": v["duration_s"],
                            "view_count": v["view_count"],
                            "published_at": v["published_at"],
                            "description": v["description"],
                            "thumbnail_url": v["thumbnail_url"],
                            "metadata_hash": compute_hash(
                                v["title"],
                                v["duration_s"],
                                v["view_count"],
                                v["published_at"],
                                cid,
                            ),
                            "first_seen_at": now_ms(),
                            "last_seen_at": now_ms(),
                            "is_available": 1,
                        }
                    )
                    if len(videos_to_insert) >= VIDEO_CHUNK:
                        _flush_video_chunk(session, videos_to_insert)
                        videos_to_insert = []
        if videos_to_insert:
            _flush_video_chunk(session, videos_to_insert)

        videos_before = int(session.scalars(select(func.count(Video.id))).first() or 0)
        videos_after = int(session.scalars(select(func.count(Video.id))).first() or 0)
        dur_rows = session.execute(select(func.max(Video.duration_s))).fetchall()
        dur_max = dur_rows[0][0] if dur_rows else 0
        view_rows = session.execute(select(func.max(Video.view_count))).fetchall()
        view_max = view_rows[0][0] if view_rows else 0

        result.videos_before = videos_before
        result.videos_after = videos_after
        result.videos_buffered = int(session.scalars(select(func.count(Video.id))).first() or 0)
        result.videos_upserted = videos_after - videos_before if videos_before else videos_after
        result.duration_max_s = int(dur_max or 0)
        result.view_count_max = int(view_max or 0)

        texts = [
            ChannelText(
                yt_id=c.id,
                name=c.name,
                description=c.description or "",
                titles=[v["title"] for v in c.videos[:30]],
            )
            for c in channels
        ]
        categories = classify(texts)
        slug_to_id = {
            row[0]: row[1]
            for row in session.execute(
                select(Category.slug, Category.id).where(Category.library_id == library.id)
            ).all()
        }
        for c in channels:
            cid = channel_map.get(c.id)
            if cid is None:
                continue
            slug = categories.get(c.id, "unclassified")
            sid = slug_to_id.get(slug)
            if sid:
                session.execute(
                    Channel.__table__.update()
                    .where(Channel.id == cid)
                    .values(category_id=sid)
                )
        session.flush()
        result.upsert_duration_s = perf_counter() - start
        result.channels_upserted = channels_upserted
        return result


def _flush_video_chunk(session: Session, videos: list[dict]) -> None:
    stmt = sqlite_insert(Video).values(videos)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Video.yt_id],
        set_={
            "channel_id": stmt.excluded.channel_id,
            "title": stmt.excluded.title,
            "duration_s": stmt.excluded.duration_s,
            "view_count": stmt.excluded.view_count,
            "published_at": func.coalesce(stmt.excluded.published_at, Video.published_at),
            "description": func.coalesce(stmt.excluded.description, Video.description),
            "thumbnail_url": stmt.excluded.thumbnail_url,
            "metadata_hash": stmt.excluded.metadata_hash,
            "last_seen_at": stmt.excluded.last_seen_at,
        },
    )
    session.execute(stmt)
