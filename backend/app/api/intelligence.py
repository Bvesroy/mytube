"""My Tube 3.0 intelligence API surface.

All endpoints are additive under `/api/v1`. They share the existing request-scoped
SQLite session, the same local-first defaults and the same optional API token guard
(applied in `main.py` because the path starts with `/api/`).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import get_db
from app.db.models import BackgroundJob, PipelineConfig, Video
from app.services.ai.runtime import ai_status, resolve_llm, save_llm_config
from app.services.analytics.timeline import build_timeline
from app.services.discovery import radar
from app.services.jobs import ensure_default_handlers_registered, run_job
from app.services.jobs_service import JobService
from app.services.knowledge.ask import ask
from app.services.knowledge.compare import (
    ComparisonError,
    compare_videos,
    get_document,
    list_documents,
    save_answer_document,
)
from app.services.knowledge.extraction import (
    ExtractionError,
    extract_knowledge,
    knowledge_summary,
    list_extracts,
    regenerate_extract,
    update_extract,
)
from app.services.knowledge.graph import build_graph, graph_node, search_nodes
from app.services.knowledge.index import index_library
from app.services.knowledge.transcripts import (
    TranscriptError,
    get_transcript,
    ingest_transcript,
    search_transcript,
)
from app.services.learning import fsrs
from app.services.learning.paths import (
    LearningPathError,
    add_item,
    generate_path,
    get_path,
    list_paths,
    move_item,
    set_item_status,
    update_path,
)
from app.services.video.chapters import (
    add_bookmark,
    delete_bookmark,
    delete_chapter,
    generate_chapters,
    list_bookmarks,
    list_chapters,
    search_in_transcript,
    update_chapter,
)
from app.services.video.perspectives import PerspectiveError, perspectives_for

router = APIRouter(prefix="/api/v1", tags=["intelligence"])


def _dt(ms: int | None):
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc) if ms else None


def _job_out(job: BackgroundJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "type": job.type,
        "status": job.status,
        "progress": float(job.progress or 0),
        "stage": job.stage,
        "result": json.loads(job.result_json) if job.result_json else None,
        "error": job.error,
        "created_at": _dt(job.created_at),
        "started_at": _dt(job.started_at),
    }


def _bad(exc: Exception, status: int = 400):
    raise HTTPException(status_code=status, detail=str(exc))


# ------------------------------------------------------------------ models


class SegmentIn(BaseModel):
    start_s: float | None = None
    end_s: float | None = None
    text: str = Field(..., min_length=1)


class TranscriptIn(BaseModel):
    video_id: str = Field(..., min_length=1, max_length=64)
    text: str | None = None
    segments: list[SegmentIn] | None = None
    language: str | None = None
    source: str = "user_supplied"
    source_kind: str | None = None
    model_id: str | None = None
    force: bool = False


class ExtractIn(BaseModel):
    video_id: str = Field(..., min_length=1, max_length=64)
    force: bool = False
    allow_llm: bool = False


class ExtractPatch(BaseModel):
    content: str | None = None
    detail: str | None = None
    rejected: bool | None = None


class AskIn(BaseModel):
    question: str = Field(..., min_length=2, max_length=1000)
    limit: int = Field(8, ge=1, le=20)
    use_llm: bool = False


class CompareIn(BaseModel):
    video_ids: list[str] = Field(..., min_length=2, max_length=8)
    objective: str | None = None
    title: str | None = None
    persist: bool = True


class SaveAnswerIn(BaseModel):
    question: str
    answer: str
    citations: list[dict[str, Any]] = Field(default_factory=list)


class PathIn(BaseModel):
    objective: str = Field(..., min_length=3, max_length=500)
    level: str = "auto"
    max_concepts: int = Field(10, ge=1, le=30)
    max_videos_per_concept: int = Field(2, ge=1, le=5)


class PathPatch(BaseModel):
    status: str | None = None
    objective: str | None = None
    level: str | None = None


class ItemStatusIn(BaseModel):
    status: str


class MoveIn(BaseModel):
    position: int = Field(..., ge=0)


class AddItemIn(BaseModel):
    stage_id: str
    video_id: str
    reason: str | None = None


class DeckIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = None


class CardGenerateIn(BaseModel):
    video_id: str
    deck_id: str | None = None
    limit: int = Field(40, ge=1, le=200)


class CardIn(BaseModel):
    front: str = Field(..., min_length=1)
    back: str = Field(..., min_length=1)
    deck_id: str | None = None
    video_id: str | None = None
    card_type: str = "manual"


class CardPatch(BaseModel):
    front: str | None = None
    back: str | None = None
    card_type: str | None = None


class ReviewIn(BaseModel):
    rating: int = Field(..., ge=1, le=4)


class ImportCardsIn(BaseModel):
    payload: dict[str, Any] | None = None


class ChapterGenerateIn(BaseModel):
    min_chapter_s: int = Field(45, ge=15, le=600)


class ChapterPatch(BaseModel):
    title: str | None = None
    start_s: float | None = Field(None, ge=0)


class BookmarkIn(BaseModel):
    timestamp_s: float = Field(..., ge=0)
    label: str = Field("", max_length=200)


class RadarConfigIn(BaseModel):
    enabled: bool | None = None
    paused: bool | None = None
    schedule: str | None = None
    window_days: int | None = Field(None, ge=1, le=365)
    max_events: int | None = Field(None, ge=1, le=200)
    per_kind: int | None = Field(None, ge=1, le=200)
    include_new_uploads: bool | None = None
    include_underwatched: bool | None = None
    include_topic_clusters: bool | None = None
    include_cross_channel: bool | None = None
    include_overlooked: bool | None = None
    include_objectives: bool | None = None
    include_connections: bool | None = None
    include_path_updates: bool | None = None


class RadarScanIn(BaseModel):
    period: str = "manual"
    window_days: int | None = Field(None, ge=1, le=365)


class AIConfigIn(BaseModel):
    enabled: bool = False
    provider: str = "ollama"
    model: str = ""
    endpoint: str | None = None


class PipelineConfigIn(BaseModel):
    steps: list[str] | None = None
    allow_llm: bool | None = None
    auto_process_new: bool | None = None
    max_videos_per_run: int | None = Field(None, ge=1, le=500)


class PipelineIn(BaseModel):
    video_ids: list[str] = Field(..., min_length=1, max_length=50)
    steps: list[str] = Field(default_factory=lambda: ["extract", "embed", "chapters"])
    allow_llm: bool = False
    run_now: bool = False


# ------------------------------------------------------------------ transcripts


@router.post("/transcripts/ingest", tags=["knowledge"])
def transcript_ingest(request: Request, body: TranscriptIn):
    session = get_db(request)
    try:
        view = ingest_transcript(
            session,
            body.video_id,
            text=body.text,
            segments=body.segments,
            language=body.language,
            source=body.source,
            source_kind=body.source_kind,
            model_id=body.model_id,
            force=body.force,
        )
    except TranscriptError as exc:
        _bad(exc)
    session.commit()
    return {
        "video_id": body.video_id,
        "transcript_id": view.transcript.id,
        "language": view.transcript.language,
        "source": view.transcript.source,
        "timing_available": view.timing_available,
        "word_count": view.transcript.word_count,
        "segment_count": view.transcript.segment_count,
        "segments": [
            {"start_s": s.start_s, "end_s": s.end_s, "text": s.text} for s in view.segments
        ],
    }


@router.get("/transcripts/{video_id}", tags=["knowledge"])
def transcript_get(request: Request, video_id: str):
    session = get_db(request)
    view = get_transcript(session, video_id)
    if view is None:
        raise HTTPException(status_code=404, detail="transcript not found")
    return {
        "video_id": video_id,
        "transcript_id": view.transcript.id,
        "language": view.transcript.language,
        "source": view.transcript.source,
        "timing_available": view.timing_available,
        "word_count": view.transcript.word_count,
        "text": view.transcript.text,
        "segments": [
            {"start_s": s.start_s, "end_s": s.end_s, "text": s.text} for s in view.segments
        ],
    }


@router.get("/transcripts/{video_id}/search", tags=["knowledge"])
def transcript_search(request: Request, video_id: str, q: str = Query(..., min_length=1), limit: int = Query(20, ge=1, le=100)):
    session = get_db(request)
    hits = search_transcript(session, video_id, q, limit=limit)
    return {
        "query": q,
        "hits": [
            {
                "segment_id": h.segment_id,
                "start_s": h.start_s,
                "text": h.text,
                "score": round(h.score, 4),
            }
            for h in hits
        ],
    }


# ------------------------------------------------------------------ knowledge extraction


@router.post("/knowledge/extract", tags=["knowledge"])
def knowledge_extract(request: Request, body: ExtractIn):
    session = get_db(request)
    llm = resolve_llm(session) if body.allow_llm else None
    try:
        result = extract_knowledge(
            session, body.video_id, force=body.force, allow_llm=body.allow_llm, llm=llm
        )
    except ExtractionError as exc:
        _bad(exc)
    session.commit()
    return result


@router.get("/knowledge/videos/{video_id}", tags=["knowledge"])
def knowledge_video(request: Request, video_id: str):
    session = get_db(request)
    try:
        return knowledge_summary(session, video_id)
    except ExtractionError as exc:
        _bad(exc, 404)


@router.get("/knowledge/videos/{video_id}/export", tags=["knowledge"])
def knowledge_export(request: Request, video_id: str):
    session = get_db(request)
    try:
        data = knowledge_summary(session, video_id)
    except ExtractionError as exc:
        _bad(exc, 404)
    return {
        "format": "mytube-knowledge-v1",
        "exported_at": int(datetime.now(tz=timezone.utc).timestamp() * 1000),
        "video_id": video_id,
        "knowledge": data,
        "note": "Every item retains its source video and timestamp when the source had timing.",
    }


@router.patch("/knowledge/extracts/{extract_id}", tags=["knowledge"])
def knowledge_extract_patch(request: Request, extract_id: int, body: ExtractPatch):
    session = get_db(request)
    try:
        result = update_extract(
            session, extract_id, content=body.content, detail=body.detail, rejected=body.rejected
        )
    except ExtractionError as exc:
        _bad(exc, 404)
    session.commit()
    return result


@router.post("/knowledge/extracts/{extract_id}/regenerate", tags=["knowledge"])
def knowledge_extract_regenerate(request: Request, extract_id: int):
    session = get_db(request)
    try:
        result = regenerate_extract(session, extract_id)
    except ExtractionError as exc:
        _bad(exc, 404)
    session.commit()
    return result


@router.get("/knowledge/search", tags=["knowledge"])
def knowledge_search(request: Request, q: str = Query(..., min_length=1), limit: int = Query(20, ge=1, le=100)):
    from app.services.knowledge.ask import _knowledge_hits

    session = get_db(request)
    rows = _knowledge_hits(session, q, limit=limit)
    return {
        "query": q,
        "hits": [
            {
                "extract_id": int(r["id"]),
                "kind": r["kind"],
                "content": r["content"],
                "detail": r["detail"],
                "start_s": r["start_s"],
                "video_id": r["yt_id"],
                "title": r["title"],
                "channel": r["channel_name"],
            }
            for r in rows
        ],
    }


# ------------------------------------------------------------------ embeddings / graph


@router.post("/embeddings/index", tags=["knowledge"])
def embeddings_index(
    request: Request,
    video_ids: list[str] | None = None,
    limit: int = Query(200, ge=1, le=2000),
    include_segments: bool = True,
):
    session = get_db(request)
    result = index_library(
        session, video_yt_ids=video_ids, limit=limit, include_segments=include_segments
    )
    session.commit()
    return result


@router.get("/graph", tags=["graph"])
def graph(
    request: Request,
    include_videos: bool = True,
    include_collections: bool = False,
    channel: str | None = None,
    concept: str | None = None,
    watched: bool | None = None,
    min_confidence: float = Query(0.0, ge=0, le=1),
    limit: int = Query(200, ge=1, le=1000),
):
    session = get_db(request)
    return build_graph(
        session,
        include_videos=include_videos,
        include_collections=include_collections,
        channel_yt_id=channel,
        concept_slug=concept,
        watched=watched,
        min_confidence=min_confidence,
        limit=limit,
    )


@router.get("/graph/node", tags=["graph"])
def graph_node_view(request: Request, node_id: str = Query(..., min_length=3), limit: int = Query(30, ge=1, le=200)):
    session = get_db(request)
    data = graph_node(session, node_id, limit=limit)
    if data is None:
        raise HTTPException(status_code=404, detail="node not found")
    return data


@router.get("/graph/search", tags=["graph"])
def graph_search(request: Request, q: str = Query(..., min_length=1), limit: int = Query(30, ge=1, le=100)):
    session = get_db(request)
    return {"nodes": search_nodes(session, q, limit=limit)}


# ------------------------------------------------------------------ ask / research


@router.post("/ask", tags=["ask"])
def ask_endpoint(request: Request, body: AskIn):
    session = get_db(request)
    llm = resolve_llm(session) if body.use_llm else None
    try:
        result = ask(session, body.question, limit=body.limit, use_llm=body.use_llm, llm=llm)
    except ValueError as exc:
        _bad(exc)
    return result


@router.post("/research/ask/save", tags=["research"])
def research_save_answer(request: Request, body: SaveAnswerIn):
    session = get_db(request)
    result = save_answer_document(
        session, question=body.question, answer=body.answer, citations=body.citations
    )
    session.commit()
    return result


@router.post("/research/compare", tags=["research"])
def research_compare(request: Request, body: CompareIn):
    session = get_db(request)
    try:
        result = compare_videos(
            session, body.video_ids, objective=body.objective, persist=body.persist, title=body.title
        )
    except ComparisonError as exc:
        _bad(exc)
    session.commit()
    return result


@router.get("/research/documents", tags=["research"])
def research_documents(request: Request, kind: str | None = None, limit: int = Query(50, ge=1, le=200)):
    session = get_db(request)
    return {"documents": list_documents(session, kind=kind, limit=limit)}


@router.get("/research/documents/{document_id}", tags=["research"])
def research_document(request: Request, document_id: str):
    session = get_db(request)
    doc = get_document(session, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    return doc


# ------------------------------------------------------------------ learning paths


@router.post("/learning/paths", tags=["learning"])
def learning_path_create(request: Request, body: PathIn):
    session = get_db(request)
    try:
        result = generate_path(
            session,
            body.objective,
            level=body.level,
            max_concepts=body.max_concepts,
            max_videos_per_concept=body.max_videos_per_concept,
        )
    except LearningPathError as exc:
        _bad(exc)
    session.commit()
    return result


@router.get("/learning/paths", tags=["learning"])
def learning_paths(request: Request, status: str | None = "active"):
    session = get_db(request)
    return {"paths": list_paths(session, status=status)}


@router.get("/learning/paths/{path_id}", tags=["learning"])
def learning_path_get(request: Request, path_id: str):
    session = get_db(request)
    try:
        return get_path(session, path_id)
    except LearningPathError as exc:
        _bad(exc, 404)


@router.patch("/learning/paths/{path_id}", tags=["learning"])
def learning_path_patch(request: Request, path_id: str, body: PathPatch):
    session = get_db(request)
    try:
        result = update_path(
            session, path_id, status=body.status, objective=body.objective, level=body.level
        )
    except LearningPathError as exc:
        _bad(exc)
    session.commit()
    return result


@router.post("/learning/paths/{path_id}/items/{item_id}/status", tags=["learning"])
def learning_item_status(request: Request, path_id: str, item_id: str, body: ItemStatusIn):
    session = get_db(request)
    try:
        item = set_item_status(session, item_id, body.status)
        data = get_path(session, path_id)
    except LearningPathError as exc:
        _bad(exc)
    session.commit()
    return {"item": item, "progress": data["progress"], "next_item": data["next_item"]}


@router.post("/learning/paths/{path_id}/items/{item_id}/move", tags=["learning"])
def learning_item_move(request: Request, path_id: str, item_id: str, body: MoveIn):
    session = get_db(request)
    try:
        item = move_item(session, item_id, position=body.position)
    except LearningPathError as exc:
        _bad(exc)
    session.commit()
    return item


@router.post("/learning/paths/{path_id}/items", tags=["learning"])
def learning_item_add(request: Request, path_id: str, body: AddItemIn):
    session = get_db(request)
    try:
        item = add_item(
            session, path_id, stage_id=body.stage_id, video_yt_id=body.video_id, reason=body.reason
        )
    except LearningPathError as exc:
        _bad(exc)
    session.commit()
    return item


@router.get("/learning/paths/{path_id}/next", tags=["learning"])
def learning_path_next(request: Request, path_id: str):
    session = get_db(request)
    try:
        data = get_path(session, path_id)
    except LearningPathError as exc:
        _bad(exc, 404)
    return {"next_item": data["next_item"], "progress": data["progress"]}


# ------------------------------------------------------------------ flashcards


@router.get("/flashcards/decks", tags=["flashcards"])
def flashcard_decks(request: Request):
    session = get_db(request)
    return {"decks": fsrs.list_decks(session)}


@router.post("/flashcards/decks", tags=["flashcards"])
def flashcard_deck_create(request: Request, body: DeckIn):
    session = get_db(request)
    try:
        deck = fsrs.create_deck(session, body.name, body.description)
    except fsrs.FlashcardError as exc:
        _bad(exc)
    session.commit()
    return deck


@router.post("/flashcards/generate", tags=["flashcards"])
def flashcard_generate(request: Request, body: CardGenerateIn):
    session = get_db(request)
    try:
        result = fsrs.generate_cards_from_video(
            session, body.video_id, deck_id=body.deck_id, limit=body.limit
        )
    except fsrs.FlashcardError as exc:
        _bad(exc)
    session.commit()
    return result


@router.get("/flashcards/due", tags=["flashcards"])
def flashcard_due(
    request: Request,
    deck_id: str | None = None,
    limit: int = Query(50, ge=1, le=200),
):
    session = get_db(request)
    return {"cards": fsrs.due_cards(session, deck_id=deck_id, limit=limit)}


@router.post("/flashcards", tags=["flashcards"])
def flashcard_create(request: Request, body: CardIn):
    session = get_db(request)
    try:
        card = fsrs.create_card(
            session,
            front=body.front,
            back=body.back,
            deck_id=body.deck_id,
            video_yt_id=body.video_id,
            card_type=body.card_type,
        )
    except fsrs.FlashcardError as exc:
        _bad(exc)
    session.commit()
    return card


@router.patch("/flashcards/{card_id}", tags=["flashcards"])
def flashcard_patch(request: Request, card_id: str, body: CardPatch):
    session = get_db(request)
    try:
        card = fsrs.update_card(
            session, card_id, front=body.front, back=body.back, card_type=body.card_type
        )
    except fsrs.FlashcardError as exc:
        _bad(exc, 404)
    session.commit()
    return card


@router.delete("/flashcards/{card_id}", tags=["flashcards"])
def flashcard_delete(request: Request, card_id: str):
    session = get_db(request)
    try:
        result = fsrs.delete_card(session, card_id)
    except fsrs.FlashcardError as exc:
        _bad(exc, 404)
    session.commit()
    return result


@router.post("/flashcards/{card_id}/restore", tags=["flashcards"])
def flashcard_restore(request: Request, card_id: str):
    session = get_db(request)
    try:
        card = fsrs.restore_card(session, card_id)
    except fsrs.FlashcardError as exc:
        _bad(exc, 404)
    session.commit()
    return card


@router.post("/flashcards/{card_id}/review", tags=["flashcards"])
def flashcard_review(request: Request, card_id: str, body: ReviewIn):
    session = get_db(request)
    try:
        result = fsrs.review_card(session, card_id, body.rating)
    except fsrs.FlashcardError as exc:
        _bad(exc, 404)
    session.commit()
    return result


@router.get("/flashcards/stats", tags=["flashcards"])
def flashcard_stats(request: Request, days: int = Query(30, ge=1, le=365)):
    session = get_db(request)
    return fsrs.review_stats(session, days=days)


@router.get("/flashcards/export/{deck_id}", tags=["flashcards"])
def flashcard_export(request: Request, deck_id: str, include_history: bool = True):
    session = get_db(request)
    try:
        return fsrs.export_deck(session, deck_id, include_history=include_history)
    except fsrs.FlashcardError as exc:
        _bad(exc, 404)


@router.post("/flashcards/import", tags=["flashcards"])
def flashcard_import(request: Request, body: ImportCardsIn):
    if not body.payload:
        raise HTTPException(status_code=400, detail="payload required")
    session = get_db(request)
    try:
        result = fsrs.import_deck(session, body.payload)
    except fsrs.FlashcardError as exc:
        _bad(exc)
    session.commit()
    return result


# ------------------------------------------------------------------ chapters / navigation


@router.get("/videos/{video_id}/chapters", tags=["navigation"])
def video_chapters(request: Request, video_id: str):
    session = get_db(request)
    return list_chapters(session, video_id)


@router.post("/videos/{video_id}/chapters/generate", tags=["navigation"])
def video_chapters_generate(request: Request, video_id: str, body: ChapterGenerateIn):
    session = get_db(request)
    result = generate_chapters(session, video_id, min_chapter_s=body.min_chapter_s)
    session.commit()
    return result


@router.patch("/chapters/{chapter_id}", tags=["navigation"])
def chapter_patch(request: Request, chapter_id: int, body: ChapterPatch):
    session = get_db(request)
    try:
        result = update_chapter(session, chapter_id, title=body.title, start_s=body.start_s)
    except ValueError as exc:
        _bad(exc)
    session.commit()
    return result


@router.delete("/chapters/{chapter_id}", tags=["navigation"])
def chapter_delete(request: Request, chapter_id: int):
    session = get_db(request)
    try:
        result = delete_chapter(session, chapter_id)
    except ValueError as exc:
        _bad(exc)
    session.commit()
    return result


@router.get("/videos/{video_id}/transcript/search", tags=["navigation"])
def video_transcript_search(
    request: Request, video_id: str, q: str = Query(..., min_length=1), limit: int = Query(30, ge=1, le=100)
):
    session = get_db(request)
    return search_in_transcript(session, video_id, q, limit=limit)


@router.get("/videos/{video_id}/bookmarks", tags=["navigation"])
def video_bookmarks(request: Request, video_id: str):
    session = get_db(request)
    return {"bookmarks": list_bookmarks(session, video_id)}


@router.post("/videos/{video_id}/bookmarks", tags=["navigation"])
def video_bookmark_create(request: Request, video_id: str, body: BookmarkIn):
    session = get_db(request)
    try:
        result = add_bookmark(session, video_id, timestamp_s=body.timestamp_s, label=body.label)
    except ValueError as exc:
        _bad(exc)
    session.commit()
    return result


@router.delete("/bookmarks/{bookmark_id}", tags=["navigation"])
def bookmark_delete(request: Request, bookmark_id: str):
    session = get_db(request)
    try:
        result = delete_bookmark(session, bookmark_id)
    except ValueError as exc:
        _bad(exc, 404)
    session.commit()
    return result


# ------------------------------------------------------------------ perspectives


@router.get("/videos/{video_id}/perspectives", tags=["discovery"])
def video_perspectives(
    request: Request, video_id: str, limit: int = Query(12, ge=1, le=40)
):
    session = get_db(request)
    try:
        return perspectives_for(session, video_id, limit=limit)
    except PerspectiveError as exc:
        _bad(exc, 404)


# ------------------------------------------------------------------ analytics


@router.get("/analytics/timeline", tags=["analytics"])
def analytics_timeline(
    request: Request,
    period: str = Query("month", pattern="^(month|week)$"),
    buckets: int = Query(12, ge=2, le=52),
):
    session = get_db(request)
    return build_timeline(session, period=period, buckets=buckets)


@router.get("/analytics/summary", tags=["analytics"])
def analytics_summary(
    request: Request,
    period: str = Query("month", pattern="^(month|week)$"),
    buckets: int = Query(12, ge=2, le=52),
):
    session = get_db(request)
    timeline = build_timeline(session, period=period, buckets=buckets)
    return {
        "summary": timeline["summary"],
        "metric_definitions": timeline["metric_definitions"],
        "note": timeline["note"],
        "period": period,
    }


# ------------------------------------------------------------------ radar


@router.get("/radar/config", tags=["discovery"])
def radar_config(request: Request):
    session = get_db(request)
    return {"config": radar.get_config(session)}


@router.put("/radar/config", tags=["discovery"])
def radar_config_update(request: Request, body: RadarConfigIn):
    session = get_db(request)
    try:
        config = radar.update_config(session, body.model_dump(exclude_none=True))
    except ValueError as exc:
        _bad(exc)
    session.commit()
    return {"config": config}


@router.post("/radar/scan", tags=["discovery"])
def radar_scan(request: Request, body: RadarScanIn):
    session = get_db(request)
    try:
        result = radar.scan(session, period=body.period, window_days=body.window_days)
    except ValueError as exc:
        _bad(exc)
    session.commit()
    return result


@router.get("/radar/report", tags=["discovery"])
def radar_report(request: Request, period: str | None = None, include_dismissed: bool = False):
    session = get_db(request)
    report = radar.latest_report(session, period=period, include_dismissed=include_dismissed)
    return {"report": report}


@router.get("/radar/reports", tags=["discovery"])
def radar_reports(request: Request, limit: int = Query(20, ge=1, le=100)):
    session = get_db(request)
    return {"reports": radar.list_reports(session, limit=limit)}


@router.post("/radar/events/{event_id}/status", tags=["discovery"])
def radar_event_status(request: Request, event_id: str, body: ItemStatusIn):
    session = get_db(request)
    try:
        result = radar.set_event_status(session, event_id, body.status)
    except ValueError as exc:
        _bad(exc)
    session.commit()
    return result


# ------------------------------------------------------------------ local AI


@router.get("/ai/status", tags=["ai"])
def ai_status_endpoint(request: Request):
    session = get_db(request)
    return ai_status(session)


@router.put("/ai/config", tags=["ai"])
def ai_config_endpoint(request: Request, body: AIConfigIn):
    session = get_db(request)
    try:
        row = save_llm_config(
            session,
            enabled=body.enabled,
            provider=body.provider,
            model=body.model,
            endpoint=body.endpoint,
        )
    except ValueError as exc:
        _bad(exc)
    session.commit()
    return {
        "selected": {
            "provider": row.provider,
            "model": row.model,
            "endpoint": row.endpoint,
            "enabled": bool(row.enabled),
        },
        "status": ai_status(session),
        "note": "Saving a selection never downloads a model or calls network services.",
    }


@router.post("/ai/test", tags=["ai"])
def ai_test_endpoint(request: Request):
    session = get_db(request)
    llm = resolve_llm(session)
    if not llm.available:
        raise HTTPException(status_code=400, detail=llm.error or "no local model available")
    try:
        text = llm.generate("Reply with exactly: OK", max_tokens=8, temperature=0.0)
    except Exception as exc:  # AIUnavailable and runtime errors
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True, "model": llm.model, "provider": llm.provider, "response": text[:200]}


@router.get("/ai/models", tags=["ai"])
def ai_models_endpoint(request: Request):
    session = get_db(request)
    status = ai_status(session)
    return {"models": status["llm"]["models"], "llm": status["llm"], "embedding": status["embedding"]}


# ------------------------------------------------------------------ processing pipeline


def _pipeline_config(session) -> dict[str, Any]:
    row = session.get(PipelineConfig, "pipeline")
    default = {
        "steps": ["extract", "embed", "chapters"],
        "allow_llm": False,
        "auto_process_new": False,
        "max_videos_per_run": 50,
    }
    if row is None or not row.value_json:
        return default
    try:
        value = json.loads(row.value_json)
    except json.JSONDecodeError:
        return default
    return {**default, **(value if isinstance(value, dict) else {})}


@router.get("/pipeline/config", tags=["pipeline"])
def pipeline_config_get(request: Request):
    session = get_db(request)
    return {"config": _pipeline_config(session)}


@router.put("/pipeline/config", tags=["pipeline"])
def pipeline_config_put(request: Request, body: PipelineConfigIn):
    session = get_db(request)
    current = _pipeline_config(session)
    patch = body.model_dump(exclude_none=True)
    if "steps" in patch:
        allowed = {"transcript", "extract", "embed", "chapters", "cards"}
        unknown = [step for step in patch["steps"] if step not in allowed]
        if unknown:
            raise HTTPException(status_code=400, detail=f"unsupported steps: {unknown}")
    current.update(patch)
    row = session.get(PipelineConfig, "pipeline")
    if row is None:
        session.add(PipelineConfig(key="pipeline", value_json=json.dumps(current), updated_at=int(datetime.now(tz=timezone.utc).timestamp() * 1000)))
    else:
        row.value_json = json.dumps(current)
        row.updated_at = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    session.commit()
    return {"config": current}


@router.post("/pipeline/run", tags=["pipeline"])
def pipeline_run(request: Request, body: PipelineIn):
    session = get_db(request)
    allowed = {"transcript", "extract", "embed", "chapters", "cards"}
    unknown = [step for step in body.steps if step not in allowed]
    if unknown:
        raise HTTPException(status_code=400, detail=f"unsupported steps: {unknown}")
    ensure_default_handlers_registered()
    payload = {"video_ids": body.video_ids, "steps": body.steps, "allow_llm": body.allow_llm}
    job = JobService(session).enqueue(
        "intelligence_pipeline",
        payload,
        dedupe_key="pipeline:" + ",".join(sorted(body.video_ids)) + ":" + ",".join(body.steps),
        priority=4,
    )
    session.commit()
    if body.run_now:
        job.status = "running"
        job.started_at = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        session.commit()
        result = run_job(session, job)
        JobService(session).finish(job.id, status="done", result=result)
        session.commit()
        return {"job": _job_out(job), "result": result}
    return {"job": _job_out(job), "note": "Queued; run the local worker to process it."}
