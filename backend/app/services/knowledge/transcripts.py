"""Transcript ingestion, normalisation and search.

Truthfulness rules:
- A transcript is stored only from text the caller actually supplied. This service
  never fetches captions from the network.
- If the caller supplies no timestamps, `timing_source='none'` and every citation
  reports `timestamp_s: null`. Sequential pseudo-timestamps are never invented.
- Timestamps that are supplied must be monotonic and inside the video duration when
  the duration is known.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from app.db.models import (
    Channel,
    ProcessingProvenance,
    Transcript,
    TranscriptSegment,
    Video,
    now_ms,
)
from app.services.knowledge.text import content_hash, fts_query, normalize_text


class TranscriptError(ValueError):
    pass


@dataclass
class SegmentIn:
    start_s: float | None
    end_s: float | None
    text: str


@dataclass
class TranscriptView:
    transcript: Transcript
    segments: list[TranscriptSegment] = field(default_factory=list)

    @property
    def timing_available(self) -> bool:
        return (self.transcript.timing_source or "none") == "supplied"


def _video(session: Session, video_yt_id: str) -> Video:
    video = session.scalar(select(Video).where(Video.yt_id == video_yt_id))
    if video is None:
        raise TranscriptError(f"video not found: {video_yt_id}")
    return video


def _coerce_segments(raw: Iterable[Any] | None) -> list[SegmentIn]:
    out: list[SegmentIn] = []
    for item in raw or []:
        if hasattr(item, "model_dump"):
            item = item.model_dump()
        if not isinstance(item, dict):
            continue
        body = normalize_text(str(item.get("text") or ""))
        if not body:
            continue
        start = item.get("start_s")
        end = item.get("end_s")
        try:
            start_f = float(start) if start is not None else None
            end_f = float(end) if end is not None else None
        except (TypeError, ValueError) as exc:
            raise TranscriptError("segment timestamps must be numbers") from exc
        if (start_f is None) != (end_f is None):
            raise TranscriptError("segment needs both start_s and end_s, or neither")
        if start_f is not None and (start_f < 0 or end_f < start_f):
            raise TranscriptError("segment timestamps must be non-negative and ordered")
        out.append(SegmentIn(start_s=start_f, end_s=end_f, text=body))
    if out and any(seg.start_s is None for seg in out) and any(seg.start_s is not None for seg in out):
        raise TranscriptError("cannot mix timestamped and untimestamped segments")
    prev = -1.0
    for seg in out:
        if seg.start_s is not None:
            if seg.start_s < prev:
                raise TranscriptError("segment timestamps must be monotonic")
            prev = seg.start_s
    return out


def _fallback_segments(body: str) -> list[SegmentIn]:
    """One untimed segment. No timestamps are invented."""
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", body) if p.strip()]
    if not paragraphs:
        return []
    return [SegmentIn(start_s=None, end_s=None, text=p) for p in paragraphs]


def ingest_transcript(
    session: Session,
    video_yt_id: str,
    *,
    text: str | None = None,
    segments: Iterable[Any] | None = None,
    language: str | None = None,
    source: str = "user_supplied",
    source_kind: str | None = None,
    model_id: str | None = None,
    provenance: dict[str, Any] | None = None,
    force: bool = False,
) -> TranscriptView:
    """Normalise and store a transcript plus segments, idempotently.

    `source` is deliberately free-form but must describe the real origin
    (`creator_captions`, `user_supplied`, `whisper_local`, ...). This function does
    not retrieve anything by itself.
    """
    video = _video(session, video_yt_id)
    segs = _coerce_segments(segments)
    if not segs:
        body = normalize_text(text or "")
        if not body:
            raise TranscriptError("transcript text or segments required")
        segs = _fallback_segments(body)
    body = normalize_text("\n".join(seg.text for seg in segs))
    if len(body) < 20:
        raise TranscriptError("transcript is too short to be useful")
    timing_source = "supplied" if segs and segs[0].start_s is not None else "none"
    if timing_source == "supplied" and video.duration_s:
        if segs[-1].end_s is not None and segs[-1].end_s > float(video.duration_s) + 5:
            raise TranscriptError("segment timestamp exceeds video duration")

    digest = content_hash(body)
    existing = session.scalar(select(Transcript).where(Transcript.video_id == video.id))
    if (
        existing
        and not force
        and existing.content_hash == digest
        and (existing.timing_source or "none") == timing_source
        and int(existing.segment_count or 0) == len(segs)
    ):
        saved = session.scalars(
            select(TranscriptSegment)
            .where(TranscriptSegment.transcript_id == existing.id)
            .order_by(TranscriptSegment.start_s, TranscriptSegment.id)
        ).all()
        return TranscriptView(transcript=existing, segments=list(saved))

    prov = {
        "method": "ingest_transcript",
        "version": 1,
        "source": source,
        "timing_source": timing_source,
        "model_id": model_id,
        "provided_segments": bool(segments),
        **(provenance or {}),
    }
    if existing is None:
        existing = Transcript(
            video_id=video.id,
            language=language,
            source=source,
            text=body,
            fetched_at=now_ms(),
        )
        session.add(existing)
        session.flush()
    else:
        old_ids = [
            int(row[0])
            for row in session.execute(
                select(TranscriptSegment.id).where(TranscriptSegment.transcript_id == existing.id)
            ).all()
        ]
        if old_ids:
            _delete_fts_rows(session, old_ids)
        session.execute(
            TranscriptSegment.__table__.delete().where(
                TranscriptSegment.transcript_id == existing.id
            )
        )
        session.expire_all()

    existing.language = language or existing.language
    existing.source = source
    existing.source_kind = source_kind or source
    existing.text = body
    existing.content_hash = digest
    existing.normalized_hash = digest
    existing.word_count = len(body.split())
    existing.segment_count = len(segs)
    existing.timing_source = timing_source
    existing.provenance_json = json.dumps(prov, sort_keys=True)
    existing.updated_at = now_ms()
    existing.fetched_at = existing.fetched_at or now_ms()
    session.flush()

    rows: list[TranscriptSegment] = []
    for seg in segs:
        row = TranscriptSegment(
            transcript_id=existing.id,
            start_s=float(seg.start_s or 0.0),
            end_s=float(seg.end_s or 0.0),
            text=seg.text,
        )
        session.add(row)
        rows.append(row)
    session.flush()

    title = video.title or ""
    channel_name = _channel_name(session, video)
    for row in rows:
        session.execute(
            sql_text(
                "INSERT INTO transcript_fts(rowid, text, video_title, channel_name) "
                "VALUES (:id, :text, :title, :channel)"
            ),
            {"id": row.id, "text": row.text, "title": title, "channel": channel_name},
        )

    session.add(
        ProcessingProvenance(
            entity_type="transcript",
            entity_id=str(existing.id),
            step="transcript_ingest",
            model_id=model_id or "deterministic-v1",
            content_hash=digest,
            status="ok",
            detail_json=json.dumps({"segments": len(rows), "timing_source": timing_source}),
        )
    )
    session.flush()
    return TranscriptView(transcript=existing, segments=rows)


def _delete_fts_rows(session: Session, segment_ids: list[int]) -> None:
    for chunk_start in range(0, len(segment_ids), 400):
        chunk = segment_ids[chunk_start : chunk_start + 400]
        placeholders = ",".join(f":id{i}" for i in range(len(chunk)))
        session.execute(
            sql_text(f"DELETE FROM transcript_fts WHERE rowid IN ({placeholders})"),
            {f"id{i}": value for i, value in enumerate(chunk)},
        )


def _channel_name(session: Session, video: Video) -> str:
    if not video.channel_id:
        return ""
    ch = session.get(Channel, video.channel_id)
    return ch.name if ch else ""


def get_transcript(session: Session, video_yt_id: str) -> TranscriptView | None:
    video = session.scalar(select(Video).where(Video.yt_id == video_yt_id))
    if video is None:
        return None
    tr = session.scalar(select(Transcript).where(Transcript.video_id == video.id))
    if tr is None:
        return None
    segs = session.scalars(
        select(TranscriptSegment)
        .where(TranscriptSegment.transcript_id == tr.id)
        .order_by(TranscriptSegment.start_s, TranscriptSegment.id)
    ).all()
    return TranscriptView(transcript=tr, segments=list(segs))


def segments_for_video(session: Session, video_id: int) -> list[TranscriptSegment]:
    tr = session.scalar(select(Transcript).where(Transcript.video_id == video_id))
    if tr is None:
        return []
    return list(
        session.scalars(
            select(TranscriptSegment)
            .where(TranscriptSegment.transcript_id == tr.id)
            .order_by(TranscriptSegment.start_s, TranscriptSegment.id)
        ).all()
    )


@dataclass
class TranscriptHit:
    video_id: str
    video_title: str
    channel_name: str
    segment_id: int
    start_s: float | None
    text: str
    score: float


def search_transcript(
    session: Session, video_yt_id: str, query: str, *, limit: int = 20
) -> list[TranscriptHit]:
    video = session.scalar(select(Video).where(Video.yt_id == video_yt_id))
    if video is None or not query.strip():
        return []
    match = fts_query(query)
    if not match:
        return []
    rows = session.execute(
        sql_text(
            """
            SELECT s.id, s.start_s, s.text, t.video_id, t.timing_source,
                   v.yt_id AS video_yt_id, v.title, coalesce(c.name,'') AS channel_name,
                   bm25(transcript_fts) AS rank
              FROM transcript_fts
              JOIN transcript_segments s ON s.id = transcript_fts.rowid
              JOIN transcripts t ON t.id = s.transcript_id
              JOIN videos v ON v.id = t.video_id
              LEFT JOIN channels c ON c.id = v.channel_id
             WHERE transcript_fts MATCH :match AND t.video_id = :vid
             ORDER BY rank
             LIMIT :limit
            """
        ),
        {"match": match, "vid": video.id, "limit": limit},
    ).fetchall()
    return [_hit(row) for row in rows]


def search_library_transcripts(
    session: Session, query: str, *, limit: int = 40
) -> list[TranscriptHit]:
    match = fts_query(query)
    if not match:
        return []
    rows = session.execute(
        sql_text(
            """
            SELECT s.id, s.start_s, s.text, t.video_id,
                   v.yt_id AS video_yt_id, v.title, coalesce(c.name,'') AS channel_name,
                   bm25(transcript_fts) AS rank
              FROM transcript_fts
              JOIN transcript_segments s ON s.id = transcript_fts.rowid
              JOIN transcripts t ON t.id = s.transcript_id
              JOIN videos v ON v.id = t.video_id
              LEFT JOIN channels c ON c.id = v.channel_id
             WHERE transcript_fts MATCH :match
             ORDER BY rank
             LIMIT :limit
            """
        ),
        {"match": match, "limit": limit},
    ).fetchall()
    return [_hit(row) for row in rows]


def _hit(row: Any) -> TranscriptHit:
    keys = row._mapping
    timing = keys.get("timing_source") or "supplied"
    start = keys.get("start_s")
    return TranscriptHit(
        video_id=str(keys.get("video_yt_id") or keys.get("video_id") or ""),
        video_title=str(keys.get("title") or ""),
        channel_name=str(keys.get("channel_name") or ""),
        segment_id=int(keys.get("id") or 0),
        start_s=float(start) if (start is not None and timing == "supplied") else None,
        text=str(keys.get("text") or ""),
        score=float(keys.get("rank") or 0.0),
    )


def transcript_text_for_video(session: Session, video_id: int) -> str:
    tr = session.scalar(select(Transcript).where(Transcript.video_id == video_id))
    return tr.text if tr else ""
