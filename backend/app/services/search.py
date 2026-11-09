"""Search service backed by SQLite FTS5.

Uses the same BM25 scoring FTS5 provides via `bm25(video_fts)` rather than a separate
pure-Python BM25 index. Query authoring is defensive: the user query is never interpolated
as raw SQL; it is injected through a single-quoted, escaped FTS5 query string so FTS5 can
parse it safely.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.fts import fts_exists, rebuild_fts
from app.repositories.library import list_videos
from app.schemas import SearchHitOut, VideoFilterRequest, ChannelSummary

logger = logging.getLogger(__name__)

FTS_TABLE = "video_fts"


def _build_fts_query(query: str, *, filters: VideoFilterRequest | None = None) -> str:
    """Build a safe FTS5 query string. Returns SQL-ready quoted literal text (not SQL itself)."""
    if not query.strip():
        return "*"
    q = query.strip()
    q = re.sub(r"\s+", " ", q)
    terms: list[str] = []
    for token in q.split():
        token = token.strip("'\"")
        if not token:
            continue
        if len(token) >= 2 and token.startswith(token[0]) and token.endswith(token[0]):
            token = token[1:-1]
        escaped = token.replace("'", "''").replace('"', '""')
        if any(ch in escaped for ch in "()*:\""):
            terms.append(f'"{escaped}"')
        else:
            terms.append(escaped if len(escaped) > 1 else f'"{escaped}"')
    if not terms:
        return "*"
    body = " ".join(terms)
    if filters:
        if filters.shorts:
            body = f'"{body}"'
    return body


def search_videos(
    session: Session,
    query: str,
    *,
    limit: int = 50,
    cursor: str | None = None,
    filters: VideoFilterRequest | None = None,
) -> tuple[list[SearchHitOut], str | None, int]:
    if not fts_exists(session.bind):
        logger.warning("video_fts missing; rebuild recommended")
        return [], None, 0

    fts_query = _build_fts_query(query, filters=filters)
    offset = 0
    if cursor:
        try:
            offset = int(cursor)
        except (TypeError, ValueError):
            offset = 0

    sql = f"""
        SELECT v.id, v.title, v.duration_s, v.view_count, v.published_at,
               bm25({FTS_TABLE}) AS rank,
               c.id AS channel_id, c.name AS channel_name, c.handle AS channel_handle
          FROM {FTS_TABLE}
          JOIN videos v ON v.id = {FTS_TABLE}.rowid
          JOIN channels c ON c.id = v.channel_id
         WHERE {FTS_TABLE} MATCH '{fts_query.replace(chr(39), chr(39)+chr(39))}'
           AND v.is_available = 1
      ORDER BY rank
         LIMIT :limit
        OFFSET :offset
    """
    rows = session.execute(
        text(sql),
        {"limit": limit + 1, "offset": offset},
    ).fetchall()

    if not rows:
        return [], None, 0

    hits: list[SearchHitOut] = []
    for row in rows[:limit]:
        channel = None
        if row.channel_id:
            channel = ChannelSummary(
                id=str(row.channel_id),
                name=row.channel_name or "",
                handle=row.channel_handle,
            )
        hits.append(
            SearchHitOut(
                id=str(row.id),
                title=row.title or "",
                duration_s=row.duration_s,
                view_count=row.view_count,
                published_at=row.published_at,
                snippet="",
                rank=float(row.rank or 0.0),
                channel=channel,
            )
        )

    next_cursor = None
    if len(rows) > limit:
        hits = hits[:limit]
        next_cursor = str(offset + limit)
    elif offset > 0 and len(rows) == limit:
        next_cursor = str(offset + limit)

    count = int(session.execute(
        text(f"SELECT count(*) FROM {FTS_TABLE} JOIN videos v ON v.id = {FTS_TABLE}.rowid WHERE {FTS_TABLE} MATCH '{fts_query.replace(chr(39), chr(39)+chr(39))}' AND v.is_available = 1"),
    ).scalar() or 0)

    return hits, next_cursor, count


def rebuild_fts_if_needed(session: Session) -> None:
    if not fts_exists(session.bind):
        rebuild_fts(session.bind)
