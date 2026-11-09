"""Intelligent chapters and transcript navigation (Feature 7).

Chapters are generated only from a transcript whose timestamps are trustworthy. If
timing is unavailable the service says so and returns no chapters rather than
inventing offsets. Every generated chapter is labelled `authored_by:
"auto_generated"`; creator-provided rows keep `authored_by: "creator"` and are never
overwritten. User edits are stored as `authored_by: "user"`.

Boundary detection is deterministic: adjacent windows are compared with TF-IDF
cosine similarity, with a small boost for explicit transition phrases. No model is
required.
"""

from __future__ import annotations

import json
import math
import re
import secrets
from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Channel,
    TimestampBookmark,
    TranscriptSegment,
    Video,
    VideoChapter,
    now_ms,
)
from app.services.knowledge.text import (
    STOPWORDS,
    excerpt,
    normalize_text,
    sentences_with_timing,
    title_terms,
    words,
)
from app.services.knowledge.transcripts import get_transcript

TRANSITION_RE = re.compile(
    r"^(?:so|now|next|then|first|second|third|finally|okay|alright|let'?s|"
    r"in this (?:section|part|chapter)|to summarize|in summary|conclusion|"
    r"before we|moving on|another (?:thing|example))",
    re.IGNORECASE,
)
DEFINITION_RE = re.compile(r"\b(?:is|are|refers to|means|denotes|describes)\b", re.IGNORECASE)
CODE_RE = re.compile(r"(```|\bdef\b|\bclass\b|\bfunction\b|\bconst\b|\breturn\b|\{|\}\s*$)", re.M)


def _window_vectors(segments: list[TranscriptSegment]) -> list[tuple[float, float, str, list[int]]]:
    """Group segments into ~30s windows with TF-IDF vectors."""
    windows: list[tuple[float, float, str, list[int]]] = []
    start: float | None = None
    end: float | None = None
    parts: list[str] = []
    ids: list[int] = []
    for seg in segments:
        seg_start = float(seg.start_s or 0)
        seg_end = float(seg.end_s or seg_start)
        if start is None:
            start = seg_start
        if parts and seg_start - (end or seg_start) > 8:
            windows.append((start, end or seg_start, " ".join(parts), ids))
            start, end, parts, ids = seg_start, seg_end, [], []
        parts.append(seg.text)
        ids.append(seg.id)
        end = seg_end
        if end - start >= 30:
            windows.append((start, end, " ".join(parts), ids))
            start, end, parts, ids = None, None, [], []
    if parts and start is not None:
        windows.append((start, end or start, " ".join(parts), ids))
    return windows


def _tfidf(vectors: list[Counter[str]]) -> list[dict[str, float]]:
    df = Counter()
    for counts in vectors:
        for term in counts:
            df[term] += 1
    n = max(1, len(vectors))
    out = []
    for counts in vectors:
        vec = {}
        for term, count in counts.items():
            idf = math.log((1 + n) / (1 + df[term])) + 1.0
            vec[term] = (1.0 + math.log(count)) * idf
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        out.append({k: v / norm for k, v in vec.items()})
    return out


def _cos(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(k, 0.0) for k, v in a.items())


def _chapter_title(text: str, *, index: int, total: int, duration_s: float | None) -> str:
    if index == 0:
        return "Introduction"
    if index == total - 1 and re.search(r"\b(conclusion|summary|wrap up|outro|thanks)\b", text, re.I):
        return "Conclusion"
    if CODE_RE.search(text):
        return "Code demonstration"
    if re.search(r"\bfor example|\be\.g\.|\bfor instance", text, re.I):
        return "Example"
    match = re.search(r"\b([A-Za-z][A-Za-z0-9 \-]{2,40})\s+(?:is|are|refers to|means)\b", text)
    if match:
        subject = match.group(1).strip()
        if subject.lower() not in STOPWORDS:
            return f"Definition: {subject}"
    terms = title_terms(text, limit=3)
    if terms:
        return " · ".join(terms)
    return f"Chapter {index + 1}"


def generate_chapters(
    session: Session,
    video_yt_id: str,
    *,
    min_chapter_s: int = 45,
    force: bool = True,
) -> dict[str, Any]:
    video = session.scalar(select(Video).where(Video.yt_id == video_yt_id))
    if video is None:
        return {"available": False, "reason": "video not found", "chapters": []}
    view = get_transcript(session, video_yt_id)
    if view is None:
        return {
            "available": False,
            "reason": "No transcript stored. Chapters are not invented from the title.",
            "authored_by": "auto_generated",
            "chapters": [],
        }
    if not view.timing_available:
        return {
            "available": False,
            "reason": "Transcript has no trustworthy timestamps; chapters need real offsets.",
            "authored_by": "auto_generated",
            "chapters": [],
        }
    segments = [s for s in view.segments if (s.end_s or 0) >= (s.start_s or 0)]
    if len(segments) < 2:
        return {
            "available": False,
            "reason": "Not enough timed segments to detect topic transitions.",
            "chapters": [],
        }

    windows = _window_vectors(segments)
    counts = [Counter(w for w in words(text) if w not in STOPWORDS and len(w) > 2) for _s, _e, text, _ids in windows]
    vecs = _tfidf(counts)
    similarities = [_cos(vecs[i], vecs[i + 1]) for i in range(len(vecs) - 1)]
    mean = sum(similarities) / len(similarities) if similarities else 1.0
    threshold = min(0.55, max(0.15, mean * 0.7))

    boundaries = [0]
    for i, text in enumerate([w[2] for w in windows]):
        if i == 0:
            continue
        transition = bool(TRANSITION_RE.match(text.strip()))
        low_similarity = i - 1 < len(similarities) and similarities[i - 1] < threshold
        long_enough = windows[i][0] - windows[boundaries[-1]][0] >= min_chapter_s
        if long_enough and (low_similarity or transition):
            boundaries.append(i)
    if len(boundaries) < 2 and len(windows) > 1:
        # Always offer at least a start and a later chapter when a natural boundary exists.
        boundaries = sorted({0, max(1, len(windows) // 2)} | set(boundaries))
    if len(boundaries) < 2:
        return {
            "available": False,
            "reason": "The transcript is too uniform to split into meaningful chapters.",
            "chapters": [],
        }

    # Remove existing auto-generated rows; never touch creator/user rows.
    if force:
        old = session.scalars(
            select(VideoChapter).where(
                VideoChapter.video_id == video.id, VideoChapter.source == "generated"
            )
        ).all()
        for row in old:
            session.delete(row)
        session.flush()

    chapters_out: list[dict[str, Any]] = []
    for pos, window_index in enumerate(boundaries):
        start_s, _end_s, text, seg_ids = windows[window_index]
        next_start = windows[boundaries[pos + 1]][0] if pos + 1 < len(boundaries) else (video.duration_s or windows[-1][1])
        title = _chapter_title(text, index=pos, total=len(boundaries), duration_s=video.duration_s)
        confidence = 0.55 + (0.2 if pos == 0 else 0.0)
        if window_index < len(similarities):
            confidence += max(0.0, min(0.2, (threshold - similarities[window_index]) * 0.8))
        row = VideoChapter(
            video_id=video.id,
            start_s=int(round(start_s)),
            title=title[:300],
            source="generated",
            confidence=round(min(0.9, confidence), 3),
            position=pos,
            generated_at=now_ms(),
            evidence_json=json.dumps(
                {
                    "segment_ids": seg_ids[:50],
                    "method": "tfidf_boundary",
                    "similarity_to_next": round(similarities[window_index], 4)
                    if window_index < len(similarities)
                    else None,
                    "transition_phrase": bool(TRANSITION_RE.match(text.strip())),
                    "text_excerpt": excerpt(text, "", width=200),
                },
                sort_keys=True,
            ),
        )
        session.add(row)
        session.flush()
        chapters_out.append(_chapter_out(row))
        if next_start is not None and int(round(next_start)) <= int(round(start_s)):
            session.delete(row)
    session.flush()
    return {
        "available": True,
        "authored_by": "auto_generated",
        "label": "Automatically generated — not creator-authored",
        "threshold": round(threshold, 3),
        "chapters": chapters_out,
    }


def _chapter_out(row: VideoChapter) -> dict[str, Any]:
    try:
        evidence = json.loads(row.evidence_json or "{}")
    except json.JSONDecodeError:
        evidence = {}
    return {
        "id": row.id,
        "start_s": row.start_s,
        "title": row.title,
        "authored_by": "creator" if row.source == "creator" else "user" if row.source == "user" else "auto_generated",
        "source": row.source or "creator",
        "confidence": round(float(row.confidence or 0), 3),
        "position": row.position,
        "evidence": evidence,
        "is_creator_chapter": row.source == "creator",
    }


def list_chapters(session: Session, video_yt_id: str) -> dict[str, Any]:
    video = session.scalar(select(Video).where(Video.yt_id == video_yt_id))
    if video is None:
        return {"available": False, "reason": "video not found", "chapters": []}
    rows = session.scalars(
        select(VideoChapter)
        .where(VideoChapter.video_id == video.id)
        .order_by(VideoChapter.start_s, VideoChapter.position)
    ).all()
    creator = [_chapter_out(row) for row in rows if (row.source or "creator") == "creator"]
    generated = [_chapter_out(row) for row in rows if row.source == "generated"]
    user = [_chapter_out(row) for row in rows if row.source == "user"]
    return {
        "available": bool(rows),
        "creator_chapters": creator,
        "generated_chapters": generated,
        "user_chapters": user,
        "chapters": creator + user + generated,
        "truthfulness_note": (
            "creator_chapters come from the source metadata. generated_chapters are "
            "auto-generated from the transcript and are labelled as such."
        ),
    }


def update_chapter(session: Session, chapter_id: int, *, title: str | None = None, start_s: float | None = None) -> dict[str, Any]:
    row = session.get(VideoChapter, chapter_id)
    if row is None:
        raise ValueError("chapter not found")
    if title is not None:
        row.title = normalize_text(title)[:300] or row.title
    if start_s is not None:
        if start_s < 0:
            raise ValueError("start_s must be non-negative")
        row.start_s = int(round(start_s))
    row.source = "user"
    session.flush()
    return _chapter_out(row)


def delete_chapter(session: Session, chapter_id: int) -> dict[str, Any]:
    row = session.get(VideoChapter, chapter_id)
    if row is None:
        raise ValueError("chapter not found")
    if (row.source or "creator") == "creator":
        raise ValueError("creator-authored chapters cannot be deleted; edit or hide them in the UI")
    session.delete(row)
    session.flush()
    return {"deleted": True, "id": chapter_id}


def search_in_transcript(
    session: Session, video_yt_id: str, query: str, *, limit: int = 30
) -> dict[str, Any]:
    from app.services.knowledge.transcripts import search_transcript

    hits = search_transcript(session, video_yt_id, query, limit=limit)
    terms = [term for term in {t.lower() for t in words(query)} if len(term) > 1]
    out = []
    for hit in hits:
        text = hit.text
        spans = []
        low = text.lower()
        for term in terms:
            start = 0
            while True:
                idx = low.find(term, start)
                if idx < 0:
                    break
                spans.append([idx, idx + len(term)])
                start = idx + len(term)
        spans.sort()
        out.append(
            {
                "segment_id": hit.segment_id,
                "start_s": hit.start_s,
                "text": text,
                "highlights": spans,
                "score": round(hit.score, 4),
            }
        )
    return {
        "query": query,
        "timing_available": any(hit.start_s is not None for hit in hits),
        "hits": out,
        "note": "Timestamps are null when the stored transcript has no trustworthy timing.",
    }


# ---------------------------------------------------------------- bookmarks


def add_bookmark(
    session: Session, video_yt_id: str, *, timestamp_s: float, label: str
) -> dict[str, Any]:
    video = session.scalar(select(Video).where(Video.yt_id == video_yt_id))
    if video is None:
        raise ValueError("video not found")
    if timestamp_s < 0:
        raise ValueError("timestamp must be non-negative")
    if video.duration_s and timestamp_s > video.duration_s + 5:
        raise ValueError("timestamp exceeds the video duration")
    row = TimestampBookmark(
        id="bm_" + secrets.token_hex(8),
        video_id=video.id,
        timestamp_s=float(timestamp_s),
        label=normalize_text(label)[:200] or f"Bookmark at {int(timestamp_s)}s",
        created_at=now_ms(),
    )
    session.add(row)
    session.flush()
    return _bookmark_out(row)


def list_bookmarks(session: Session, video_yt_id: str) -> list[dict[str, Any]]:
    video = session.scalar(select(Video).where(Video.yt_id == video_yt_id))
    if video is None:
        return []
    rows = session.scalars(
        select(TimestampBookmark)
        .where(TimestampBookmark.video_id == video.id)
        .order_by(TimestampBookmark.timestamp_s)
    ).all()
    return [_bookmark_out(row) for row in rows]


def _bookmark_out(row: TimestampBookmark) -> dict[str, Any]:
    return {
        "id": row.id,
        "timestamp_s": row.timestamp_s,
        "label": row.label,
        "created_at": row.created_at,
    }


def delete_bookmark(session: Session, bookmark_id: str) -> dict[str, Any]:
    row = session.get(TimestampBookmark, bookmark_id)
    if row is None:
        raise ValueError("bookmark not found")
    session.delete(row)
    session.flush()
    return {"deleted": True, "id": bookmark_id}
