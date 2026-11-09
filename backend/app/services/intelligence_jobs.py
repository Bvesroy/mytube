"""Background job handlers for the knowledge pipeline (Milestone 1/7).

All handlers are incremental and idempotent, report progress and stage, and stop
cleanly when cancellation is requested. None of them fetch from the network.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import BackgroundJob, Video
from app.services.ai.runtime import resolve_llm
from app.services.discovery.radar import get_config, scan as radar_scan
from app.services.jobs import JobHandler, register
from app.services.jobs_service import JobService
from app.services.knowledge.extraction import extract_knowledge
from app.services.knowledge.index import index_video
from app.services.knowledge.transcripts import ingest_transcript
from app.services.learning.fsrs import generate_cards_from_video
from app.services.video.chapters import generate_chapters


def _beat(session: Session, job: BackgroundJob, progress: float, stage: str) -> None:
    JobService(session).heartbeat(job.id, progress=progress, stage=stage)
    session.commit()


def _canceled(session: Session, job: BackgroundJob) -> bool:
    session.refresh(job)
    return bool(int(job.cancel_requested or 0)) or job.status == "canceled"


def _require(payload: dict[str, Any], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise ValueError(f"payload.{key} is required")
    return value


def run_transcript_ingest(session: Session, job: BackgroundJob, payload: dict[str, Any]) -> dict[str, Any]:
    video_id = _require(payload, "video_id")
    _beat(session, job, 0.1, "normalizing")
    if _canceled(session, job):
        return {"ok": False, "canceled": True}
    view = ingest_transcript(
        session,
        video_id,
        text=payload.get("text"),
        segments=payload.get("segments"),
        language=payload.get("language"),
        source=str(payload.get("source") or "user_supplied"),
        source_kind=payload.get("source_kind"),
        model_id=payload.get("model_id"),
        force=bool(payload.get("force")),
    )
    session.commit()
    _beat(session, job, 0.9, "indexed")
    return {
        "ok": True,
        "video_id": video_id,
        "transcript_id": view.transcript.id,
        "segments": len(view.segments),
        "timing_available": view.timing_available,
        "word_count": view.transcript.word_count,
    }


def run_knowledge_extract(session: Session, job: BackgroundJob, payload: dict[str, Any]) -> dict[str, Any]:
    video_id = _require(payload, "video_id")
    _beat(session, job, 0.1, "extracting")
    llm = resolve_llm(session) if payload.get("allow_llm") else None
    result = extract_knowledge(
        session,
        video_id,
        force=bool(payload.get("force")),
        allow_llm=bool(payload.get("allow_llm")),
        llm=llm,
    )
    session.commit()
    _beat(session, job, 0.95, "stored")
    return {
        "ok": True,
        "video_id": video_id,
        "source_kind": result["source_kind"],
        "timing_available": result["timing_available"],
        "created": result["created"],
        "updated": result["updated"],
        "concepts": result["concepts"],
        "extracts": len(result["extracts"]),
        "llm_used": bool(payload.get("allow_llm") and llm is not None and llm.available),
    }


def run_embedding_index(session: Session, job: BackgroundJob, payload: dict[str, Any]) -> dict[str, Any]:
    ids = payload.get("video_ids") or []
    q = select(Video)
    if ids:
        q = q.where(Video.yt_id.in_([str(x) for x in ids]))
    limit = int(payload.get("limit") or 500)
    videos = list(session.scalars(q.limit(limit)).all())
    totals = {"processed": 0, "videos": 0, "segments": 0, "extracts": 0, "concepts": 0}
    _beat(session, job, 0.02, "indexing")
    for index, video in enumerate(videos):
        if _canceled(session, job):
            return {"ok": False, "canceled": True, **totals}
        counts = index_video(session, video, include_segments=bool(payload.get("include_segments", True)))
        for key, value in counts.items():
            totals[key] += value
        totals["processed"] += 1
        if index % 10 == 0:
            session.commit()
            _beat(session, job, 0.05 + 0.9 * (index + 1) / max(1, len(videos)), "indexing")
    session.commit()
    return {"ok": True, **totals, "model_id": "active"}


def run_chapters(session: Session, job: BackgroundJob, payload: dict[str, Any]) -> dict[str, Any]:
    video_id = _require(payload, "video_id")
    _beat(session, job, 0.2, "segmenting")
    result = generate_chapters(session, video_id, min_chapter_s=int(payload.get("min_chapter_s") or 45))
    session.commit()
    return {"ok": True, "video_id": video_id, **result}


def run_flashcards(session: Session, job: BackgroundJob, payload: dict[str, Any]) -> dict[str, Any]:
    video_id = _require(payload, "video_id")
    _beat(session, job, 0.2, "generating_cards")
    result = generate_cards_from_video(
        session, video_id, deck_id=payload.get("deck_id"), limit=int(payload.get("limit") or 40)
    )
    session.commit()
    return {"ok": True, "video_id": video_id, "deck_id": result["deck_id"], "created": result["created"], "skipped": result["skipped"]}


def run_radar(session: Session, job: BackgroundJob, payload: dict[str, Any]) -> dict[str, Any]:
    config = get_config(session)
    if not config["enabled"] or config["paused"]:
        return {"ok": True, "skipped": True, "reason": "discovery disabled or paused"}
    _beat(session, job, 0.2, "scanning_library")
    result = radar_scan(
        session,
        period=str(payload.get("period") or "manual"),
        window_days=payload.get("window_days"),
    )
    session.commit()
    return {"ok": True, "report_id": result.get("id"), "events": len(result.get("events", []))}


def run_pipeline(session: Session, job: BackgroundJob, payload: dict[str, Any]) -> dict[str, Any]:
    video_ids = [str(v) for v in (payload.get("video_ids") or [])]
    if not video_ids:
        raise ValueError("payload.video_ids is required")
    steps = payload.get("steps") or ["extract", "embed", "chapters"]
    allowed = {"transcript", "extract", "embed", "chapters", "cards"}
    unknown = [step for step in steps if step not in allowed]
    if unknown:
        raise ValueError(f"unsupported pipeline steps: {unknown}")
    allow_llm = bool(payload.get("allow_llm"))
    llm = resolve_llm(session) if allow_llm else None
    results: list[dict[str, Any]] = []
    total = max(1, len(video_ids) * len(steps))
    done = 0
    for video_id in video_ids:
        video_result: dict[str, Any] = {"video_id": video_id, "steps": {}, "errors": []}
        video = session.scalar(select(Video).where(Video.yt_id == video_id))
        if video is None:
            video_result["errors"].append("video not in subscription library")
            results.append(video_result)
            done += len(steps)
            continue
        for step in steps:
            if _canceled(session, job):
                return {"ok": False, "canceled": True, "results": results}
            try:
                if step == "transcript":
                    if payload.get("text") or payload.get("segments"):
                        view = ingest_transcript(
                            session,
                            video_id,
                            text=payload.get("text"),
                            segments=payload.get("segments"),
                            source=str(payload.get("source") or "user_supplied"),
                        )
                        video_result["steps"][step] = {"segments": len(view.segments)}
                    else:
                        video_result["steps"][step] = {"skipped": "no transcript payload; nothing fetched"}
                elif step == "extract":
                    res = extract_knowledge(session, video_id, allow_llm=allow_llm, llm=llm)
                    video_result["steps"][step] = {"created": res["created"], "concepts": res["concepts"]}
                elif step == "embed":
                    video_result["steps"][step] = index_video(session, video)
                elif step == "chapters":
                    res = generate_chapters(session, video_id)
                    video_result["steps"][step] = {"available": res.get("available"), "chapters": len(res.get("chapters", []))}
                elif step == "cards":
                    res = generate_cards_from_video(session, video_id)
                    video_result["steps"][step] = {"created": res["created"]}
                session.commit()
            except Exception as exc:  # keep processing other videos; report per-step
                session.rollback()
                video_result["errors"].append(f"{step}: {type(exc).__name__}: {exc}")
            done += 1
            _beat(session, job, 0.05 + 0.9 * done / total, f"{step}:{video_id}")
        results.append(video_result)
    return {
        "ok": not any(r["errors"] for r in results),
        "videos": len(results),
        "steps": steps,
        "results": results,
        "fetch_network": False,
    }


register("transcript_ingest", JobHandler(run=run_transcript_ingest))
register("knowledge_extract", JobHandler(run=run_knowledge_extract))
register("embedding_index", JobHandler(run=run_embedding_index))
register("chapters_generate", JobHandler(run=run_chapters))
register("flashcards_generate", JobHandler(run=run_flashcards))
register("discovery_radar", JobHandler(run=run_radar))
register("intelligence_pipeline", JobHandler(run=run_pipeline))
