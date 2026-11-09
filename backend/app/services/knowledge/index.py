"""Embedding index: incremental, versioned, dimension-checked.

Vectors are stored with `model_id`; rows from a different model are never compared
against the active query vector. Re-indexing is driven by a content hash, so
unchanged text is skipped. All of this runs locally on CPU.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Concept,
    EmbeddingVector,
    KnowledgeExtract,
    ProcessingProvenance,
    Transcript,
    TranscriptSegment,
    Video,
    VideoConcept,
    now_ms,
)
from app.services.ai.embeddings import configured_engine, cosine, pack_vector, unpack_vector
from app.services.knowledge.text import content_hash, normalize_text


def _engine():
    return configured_engine()


def _upsert(
    session: Session,
    *,
    owner_type: str,
    owner_id: int,
    text: str,
    engine,
    force: bool = False,
) -> bool:
    digest = content_hash(f"{owner_type}:{owner_id}:{text}")
    existing = session.scalar(
        select(EmbeddingVector).where(
            EmbeddingVector.owner_type == owner_type,
            EmbeddingVector.owner_id == owner_id,
            EmbeddingVector.model_id == engine.model_id,
        )
    )
    if existing is not None and not force and existing.content_hash == digest:
        return False
    vec = engine.embed(text)
    if not vec or not any(vec):
        return False
    if existing is None:
        session.add(
            EmbeddingVector(
                owner_type=owner_type,
                owner_id=owner_id,
                model_id=engine.model_id,
                dim=len(vec),
                vector=pack_vector(vec),
                norm=1.0,
                content_hash=digest,
            )
        )
    else:
        if existing.dim != len(vec):
            # Dimension changed: replace rather than mix incompatible vectors.
            existing.dim = len(vec)
        existing.vector = pack_vector(vec)
        existing.content_hash = digest
        existing.created_at = now_ms()
    return True


def index_video(
    session: Session,
    video: Video,
    *,
    include_segments: bool = True,
    force: bool = False,
    engine=None,
) -> dict[str, int]:
    engine = engine or _engine()
    transcript = session.scalar(select(Transcript).where(Transcript.video_id == video.id))
    extracts = session.scalars(
        select(KnowledgeExtract).where(
            KnowledgeExtract.video_id == video.id, KnowledgeExtract.rejected == 0
        )
    ).all()
    parts = [video.title or "", video.description or ""]
    if transcript:
        parts.append(transcript.text[:20000])
    parts.extend(f"{row.kind}: {row.content} {row.detail or ''}" for row in extracts)
    body = normalize_text("\n".join(p for p in parts if p))
    counts = {"videos": 0, "segments": 0, "extracts": 0, "concepts": 0}
    if body and _upsert(session, owner_type="video", owner_id=video.id, text=body, engine=engine, force=force):
        counts["videos"] += 1
    if transcript and include_segments:
        segments = session.scalars(
            select(TranscriptSegment).where(TranscriptSegment.transcript_id == transcript.id)
        ).all()
        for seg in segments:
            if _upsert(
                session,
                owner_type="segment",
                owner_id=seg.id,
                text=seg.text,
                engine=engine,
                force=force,
            ):
                counts["segments"] += 1
    for row in extracts:
        if _upsert(
            session,
            owner_type="extract",
            owner_id=row.id,
            text=f"{row.content} {row.detail or ''}",
            engine=engine,
            force=force,
        ):
            counts["extracts"] += 1
    concept_links = session.execute(
        select(Concept.id, Concept.name)
        .join(VideoConcept, VideoConcept.concept_id == Concept.id)
        .where(VideoConcept.video_id == video.id)
    ).all()
    for concept_id, name in concept_links:
        if _upsert(
            session,
            owner_type="concept",
            owner_id=int(concept_id),
            text=name,
            engine=engine,
            force=force,
        ):
            counts["concepts"] += 1
    session.add(
        ProcessingProvenance(
            entity_type="video_embeddings",
            entity_id=video.yt_id,
            step="embedding_index",
            model_id=engine.model_id,
            content_hash=content_hash(body),
            status="ok",
            detail_json=json.dumps(counts),
        )
    )
    session.flush()
    return counts


def index_library(
    session: Session,
    *,
    video_yt_ids: Iterable[str] | None = None,
    limit: int | None = None,
    include_segments: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    engine = _engine()
    q = select(Video)
    if video_yt_ids:
        q = q.where(Video.yt_id.in_(list(video_yt_ids)))
    if limit:
        q = q.limit(limit)
    videos = session.scalars(q).all()
    totals = {"videos": 0, "segments": 0, "extracts": 0, "concepts": 0, "processed": 0}
    for video in videos:
        counts = index_video(session, video, include_segments=include_segments, force=force, engine=engine)
        totals["processed"] += 1
        for key, value in counts.items():
            totals[key] += value
    totals["model_id"] = engine.model_id
    totals["dim"] = engine.dim
    return totals


def _load_vectors(session: Session, owner_type: str, model_id: str, *, owner_ids: list[int] | None = None):
    q = select(EmbeddingVector).where(
        EmbeddingVector.owner_type == owner_type, EmbeddingVector.model_id == model_id
    )
    if owner_ids:
        q = q.where(EmbeddingVector.owner_id.in_(owner_ids))
    return session.scalars(q).all()


def semantic_search(
    session: Session,
    query: str,
    *,
    owner_types: tuple[str, ...] = ("video", "extract", "segment"),
    limit: int = 20,
    min_score: float = 0.05,
    candidate_video_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    engine = _engine()
    qvec = engine.embed(query)
    if not any(qvec):
        return []
    scored: list[dict[str, Any]] = []
    for owner_type in owner_types:
        rows = _load_vectors(session, owner_type, engine.model_id)
        for row in rows:
            if row.dim != engine.dim:
                continue
            vec = unpack_vector(row.vector, row.dim)
            score = cosine(qvec, vec)
            if score < min_score:
                continue
            scored.append(
                {
                    "owner_type": owner_type,
                    "owner_id": row.owner_id,
                    "score": score,
                    "model_id": row.model_id,
                }
            )
    scored.sort(key=lambda item: item["score"], reverse=True)
    return scored[:limit]


def related_videos(session: Session, video_yt_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
    video = session.scalar(select(Video).where(Video.yt_id == video_yt_id))
    if video is None:
        return []
    engine = _engine()
    seed = session.scalar(
        select(EmbeddingVector).where(
            EmbeddingVector.owner_type == "video",
            EmbeddingVector.owner_id == video.id,
            EmbeddingVector.model_id == engine.model_id,
        )
    )
    if seed is None:
        index_video(session, video, include_segments=False)
        seed = session.scalar(
            select(EmbeddingVector).where(
                EmbeddingVector.owner_type == "video",
                EmbeddingVector.owner_id == video.id,
                EmbeddingVector.model_id == engine.model_id,
            )
        )
    if seed is None:
        return []
    seed_vec = unpack_vector(seed.vector, seed.dim)
    rows = session.execute(
        select(EmbeddingVector, Video)
        .join(Video, Video.id == EmbeddingVector.owner_id)
        .where(
            EmbeddingVector.owner_type == "video",
            EmbeddingVector.model_id == engine.model_id,
            EmbeddingVector.owner_id != video.id,
            Video.is_available == 1,
        )
    ).all()
    out: list[dict[str, Any]] = []
    for emb, other in rows:
        score = cosine(seed_vec, unpack_vector(emb.vector, emb.dim))
        if score <= 0.02:
            continue
        out.append(
            {
                "video_id": other.yt_id,
                "title": other.title,
                "channel_id": other.channel_id,
                "score": round(score, 4),
                "provenance": {"method": "embedding_cosine", "model_id": engine.model_id},
            }
        )
    out.sort(key=lambda item: item["score"], reverse=True)
    return out[:limit]
