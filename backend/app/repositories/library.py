"""Library persistence: channels, videos, categories, statistics, cursors."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.db.models import Category, Channel, Library, Video, now_ms

CHUNK = 2000


@dataclass(frozen=True)
class ChannelRow:
    yt_id: str
    name: str
    url: str | None = None
    handle: str | None = None
    avatar_url: str | None = None
    description: str | None = None
    subscriber_count: int | None = None


@dataclass(frozen=True)
class VideoRow:
    yt_id: str
    channel_id: int
    title: str
    duration_s: int | None
    view_count: int | None
    metadata_hash: str
    published_at: int | None = None
    description: str | None = None
    thumbnail_url: str | None = None


# ------------------------------------------------------------------ cursors


def encode_cursor(values: dict) -> str:
    raw = json.dumps(values, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> dict:
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(padded.encode()))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("invalid cursor") from exc


# ------------------------------------------------------------------ library


def get_or_create_library(
    session: Session, name: str = "main", source_path: str | None = None
) -> Library:
    library = session.scalar(select(Library).where(Library.name == name))
    if library:
        if source_path and library.source_path != source_path:
            library.source_path = source_path
            library.updated_at = now_ms()
        return library
    library = Library(name=name, source_path=source_path, created_at=now_ms(), updated_at=now_ms())
    session.add(library)
    session.flush()
    return library


def seed_categories(session: Session, library_id: int, catalog: dict[str, str]) -> dict[str, int]:
    existing = {
        category.slug: category.id
        for category in session.scalars(select(Category).where(Category.library_id == library_id))
    }
    for position, (slug, name) in enumerate(catalog.items()):
        if slug in existing:
            continue
        category = Category(library_id=library_id, slug=slug, name=name, position=position)
        session.add(category)
        session.flush()
        existing[slug] = category.id
    return existing


def category_map(session: Session, library_id: int) -> dict[str, int]:
    return {
        category.slug: category.id
        for category in session.scalars(select(Category).where(Category.library_id == library_id))
    }


# ------------------------------------------------------------------ writes


def upsert_channels(
    session: Session, library_id: int, rows: Sequence[ChannelRow], *, timestamp: int | None = None
) -> int:
    stamp = timestamp or now_ms()
    for start in range(0, len(rows), CHUNK):
        payload = [
            {
                "library_id": library_id,
                "yt_id": row.yt_id,
                "name": row.name,
                "url": row.url,
                "handle": row.handle,
                "avatar_url": row.avatar_url,
                "description": row.description,
                "subscriber_count": row.subscriber_count,
                "first_seen_at": stamp,
                "last_seen_at": stamp,
            }
            for row in rows[start : start + CHUNK]
        ]
        statement = sqlite_insert(Channel).values(payload)
        statement = statement.on_conflict_do_update(
            index_elements=[Channel.yt_id],
            set_={
                "name": statement.excluded.name,
                "url": statement.excluded.url,
                "handle": statement.excluded.handle,
                "avatar_url": statement.excluded.avatar_url,
                "description": statement.excluded.description,
                "subscriber_count": statement.excluded.subscriber_count,
                "last_seen_at": statement.excluded.last_seen_at,
            },
        )
        session.execute(statement)
    session.flush()
    return len(rows)


def channel_id_map(session: Session, library_id: int, yt_ids: Iterable[str] | None = None):
    statement = select(Channel.yt_id, Channel.id).where(Channel.library_id == library_id)
    if yt_ids is not None:
        statement = statement.where(Channel.yt_id.in_(list(yt_ids)))
    return {row[0]: row[1] for row in session.execute(statement)}


def upsert_videos(
    session: Session, rows: Sequence[VideoRow], *, timestamp: int | None = None
) -> tuple[int, int]:
    """Insert or refresh videos. Returns (seen, before_count) for new/updated maths."""
    stamp = timestamp or now_ms()
    before = int(session.scalar(select(func.count(Video.id))) or 0)
    for start in range(0, len(rows), CHUNK):
        payload = [
            {
                "channel_id": row.channel_id,
                "yt_id": row.yt_id,
                "title": row.title,
                "duration_s": row.duration_s,
                "view_count": row.view_count,
                "published_at": row.published_at,
                "thumbnail_url": row.thumbnail_url,
                "description": row.description,
                "metadata_hash": row.metadata_hash,
                "first_seen_at": stamp,
                "last_seen_at": stamp,
            }
            for row in rows[start : start + CHUNK]
        ]
        statement = sqlite_insert(Video).values(payload)
        statement = statement.on_conflict_do_update(
            index_elements=[Video.yt_id],
            set_={
                "channel_id": statement.excluded.channel_id,
                "title": statement.excluded.title,
                "duration_s": statement.excluded.duration_s,
                "view_count": statement.excluded.view_count,
                "published_at": func.coalesce(
                    statement.excluded.published_at, Video.published_at
                ),
                "thumbnail_url": func.coalesce(
                    statement.excluded.thumbnail_url, Video.thumbnail_url
                ),
                "description": func.coalesce(statement.excluded.description, Video.description),
                "metadata_hash": statement.excluded.metadata_hash,
                "last_seen_at": statement.excluded.last_seen_at,
                "is_available": 1,
            },
            # Never rewrite an unchanged row: it would churn the FTS index and page cache.
            where=Video.metadata_hash != statement.excluded.metadata_hash,
        )
        session.execute(statement)
    session.flush()
    after = int(session.scalar(select(func.count(Video.id))) or 0)
    return len(rows), before


def set_channel_categories(session: Session, mapping: dict[str, int]) -> int:
    updated = 0
    for yt_id, category_id in mapping.items():
        updated += int(
            session.query(Channel)
            .filter(Channel.yt_id == yt_id, Channel.category_id != category_id)
            .update({Channel.category_id: category_id}, synchronize_session=False)
            or 0
        )
    return updated


def mark_channel_seen(session: Session, channel_id: int, timestamp: int | None = None) -> None:
    session.query(Channel).filter(Channel.id == channel_id).update(
        {Channel.last_seen_at: timestamp or now_ms()}, synchronize_session=False
    )


# ------------------------------------------------------------------ reads


def channel_by_yt_id(session: Session, yt_id: str) -> Channel | None:
    return session.scalar(select(Channel).where(Channel.yt_id == yt_id))


def video_by_yt_id(session: Session, yt_id: str) -> Video | None:
    return session.scalar(select(Video).where(Video.yt_id == yt_id))


def list_channels(
    session: Session,
    *,
    library_id: int,
    limit: int = 50,
    cursor: str | None = None,
    query: str | None = None,
    category_id: int | None = None,
    sort: str = "name",
) -> tuple[list[Channel], str | None]:
    statement = select(Channel).where(Channel.library_id == library_id)
    if query:
        statement = statement.where(Channel.name.ilike(f"%{query}%"))
    if category_id is not None:
        statement = statement.where(Channel.category_id == category_id)
    if cursor:
        state = decode_cursor(cursor)
        if sort == "name":
            statement = statement.where(
                (Channel.name > state["k"])
                | ((Channel.name == state["k"]) & (Channel.id > state["id"]))
            )
        else:
            statement = statement.where(Channel.id > state["id"])
    if sort == "name":
        statement = statement.order_by(Channel.name, Channel.id).limit(limit + 1)
    else:
        statement = statement.order_by(Channel.id).limit(limit + 1)
    rows = list(session.scalars(statement))
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = encode_cursor(
            {"k": last.name, "id": last.id} if sort == "name" else {"id": last.id}
        )
    return rows, next_cursor


def list_videos(
    session: Session,
    *,
    limit: int = 50,
    cursor: str | None = None,
    channel_id: int | None = None,
    category_id: int | None = None,
    min_duration: int | None = None,
    max_duration: int | None = None,
    available_only: bool = True,
    sort: str = "recent",
) -> tuple[list[tuple[Video, Channel]], str | None]:
    statement = select(Video, Channel).join(Channel, Channel.id == Video.channel_id)
    if channel_id is not None:
        statement = statement.where(Video.channel_id == channel_id)
    if category_id is not None:
        statement = statement.where(Channel.category_id == category_id)
    if min_duration is not None:
        statement = statement.where(Video.duration_s >= min_duration)
    if max_duration is not None:
        statement = statement.where(Video.duration_s <= max_duration)
    if available_only:
        statement = statement.where(Video.is_available == 1)
    if cursor:
        state = decode_cursor(cursor)
        statement = statement.where(Video.id < state["id"])
    statement = statement.order_by(Video.id.desc()).limit(limit + 1)
    rows = list(session.execute(statement).all())
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_cursor({"id": rows[-1][0].id})
    return rows, next_cursor


def stats(session: Session, library_id: int) -> dict:
    videos = int(session.scalar(select(func.count(Video.id))) or 0)
    channels = int(session.scalar(select(func.count(Channel.id))) or 0)
    duration = int(session.scalar(select(func.coalesce(func.sum(Video.duration_s), 0))) or 0)
    unavailable = int(
        session.scalar(select(func.count(Video.id)).where(Video.is_available == 0)) or 0
    )
    categories = int(
        session.scalar(select(func.count(Category.id)).where(Category.library_id == library_id))
        or 0
    )
    return {
        "channels": channels,
        "videos": videos,
        "hours": round(duration / 3600, 1),
        "categories": categories,
        "videos_unavailable": unavailable,
    }


def failed_channel_count(session: Session) -> int:
    from app.db.models import ChannelSyncState

    return int(
        session.scalar(
            select(func.count(ChannelSyncState.channel_id)).where(
                ChannelSyncState.status.in_(("failed", "partial", "throttled"))
            )
        )
        or 0
    )
