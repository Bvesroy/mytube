"""Intelligent learning paths (Feature 5).

Given an objective, the generator only uses concepts and videos already present in
the user's imported subscription library:

1. match objective terms to stored concepts (and to FTS video hits as a fallback);
2. order concepts by explicit prerequisite edges (topological, cycle-safe);
3. attach the best-evidenced video(s) per concept, never repeating a video;
4. group into beginner/intermediate/advanced/research stages by surface difficulty;
5. report concepts and objective terms the library cannot cover.

Nothing is invented when the library lacks material: the response says what is
missing and the generator refuses to create an empty path.
"""

from __future__ import annotations

import json
import secrets
from collections import defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Channel,
    Concept,
    ConceptRelation,
    LearningPath,
    LearningPathItem,
    LearningPathStage,
    Video,
    VideoConcept,
    WatchHistory,
    now_ms,
)
from app.services.knowledge.text import (
    difficulty_score,
    keywords,
    level_for_score,
    slugify,
    words,
)
from app.services.search import search_videos

LEVEL_ORDER = ["beginner", "intermediate", "advanced", "research"]


class LearningPathError(ValueError):
    pass


def _concepts(session: Session) -> list[Concept]:
    return list(session.scalars(select(Concept).limit(20000)).all())


def _concept_scores(session: Session, objective: str) -> list[tuple[Concept, float]]:
    terms = {w for w in words(objective) if len(w) > 2}
    if not terms:
        return []
    links = defaultdict(int)
    for concept_id, count in session.execute(
        select(VideoConcept.concept_id, func.count(VideoConcept.video_id)).group_by(
            VideoConcept.concept_id
        )
    ).all():
        links[int(concept_id)] = int(count)
    out: list[tuple[Concept, float]] = []
    for concept in _concepts(session):
        name_terms = set(words(concept.name))
        overlap = len(terms & name_terms)
        if overlap == 0:
            continue
        score = overlap * 2.0 + min(3.0, links.get(concept.id, 0) * 0.25) + 0.5
        out.append((concept, score))
    out.sort(key=lambda item: (-item[1], item[0].name))
    return out[:40]


def _fallback_videos(session: Session, objective: str, limit: int = 40) -> list[Video]:
    try:
        hits, _cursor, _count = search_videos(session, objective, limit=limit)
    except Exception:
        hits = []
    ids: list[int] = []
    for hit in hits:
        try:
            ids.append(int(hit.id))
        except (TypeError, ValueError):
            continue
    if not ids:
        return []
    return list(session.scalars(select(Video).where(Video.id.in_(ids), Video.is_available == 1)).all())


def _concepts_for_videos(session: Session, video_ids: list[int]) -> list[Concept]:
    if not video_ids:
        return []
    rows = session.execute(
        select(Concept)
        .join(VideoConcept, VideoConcept.concept_id == Concept.id)
        .where(VideoConcept.video_id.in_(video_ids))
        .distinct()
    ).scalars().all()
    return list(rows)


def _order_concepts(session: Session, selected: list[Concept]) -> list[Concept]:
    ids = {c.id for c in selected}
    deps: dict[int, set[int]] = {c.id: set() for c in selected}
    for relation in session.scalars(
        select(ConceptRelation).where(ConceptRelation.relation == "prerequisite")
    ).all():
        if relation.src_concept_id in ids and relation.dst_concept_id in ids:
            deps[relation.src_concept_id].add(relation.dst_concept_id)
    ordered: list[int] = []
    state: dict[int, int] = {}

    def visit(node: int) -> None:
        if state.get(node) == 2:
            return
        if state.get(node) == 1:
            return  # cycle: keep the first visit, do not loop
        state[node] = 1
        for dep in sorted(deps.get(node, set())):
            visit(dep)
        state[node] = 2
        ordered.append(node)

    for concept in selected:
        visit(concept.id)
    by_id = {c.id: c for c in selected}
    return [by_id[cid] for cid in ordered if cid in by_id]


def _video_rows(session: Session, concept_id: int, *, limit: int = 3) -> list[tuple[Video, Channel | None, float]]:
    rows = session.execute(
        select(Video, Channel, VideoConcept)
        .join(VideoConcept, VideoConcept.video_id == Video.id)
        .outerjoin(Channel, Channel.id == Video.channel_id)
        .where(VideoConcept.concept_id == concept_id, Video.is_available == 1)
        .order_by(VideoConcept.confidence.desc(), Video.published_at.desc())
        .limit(limit)
    ).all()
    return [(video, channel, float(link.confidence or 0)) for video, channel, link in rows]


def _video_level(video: Video) -> str:
    return level_for_score(difficulty_score(video.title, video.description or "", video.duration_s))


def generate_path(
    session: Session,
    objective: str,
    *,
    level: str = "auto",
    max_concepts: int = 10,
    max_videos_per_concept: int = 2,
) -> dict[str, Any]:
    objective = (objective or "").strip()
    if len(objective) < 3:
        raise LearningPathError("objective required")
    if level not in {"auto", *LEVEL_ORDER}:
        raise LearningPathError("level must be auto, beginner, intermediate, advanced or research")

    scored = _concept_scores(session, objective)
    concepts = [concept for concept, _score in scored][:max_concepts]
    missing_terms: list[str] = []
    if not concepts:
        fallback = _fallback_videos(session, objective)
        concepts = _concepts_for_videos(session, [v.id for v in fallback])[:max_concepts]
    if not concepts:
        raise LearningPathError(
            "The library has no processed concepts or matching videos for this objective. "
            "Process transcripts first or choose a different objective."
        )

    ordered = _order_concepts(session, concepts)
    watched = {
        int(row[0])
        for row in session.execute(select(WatchHistory.video_id).where(WatchHistory.completed == 1)).all()
    }
    used_videos: set[int] = set()
    planned: list[dict[str, Any]] = []
    for concept in ordered:
        rows = _video_rows(session, concept.id, limit=max_videos_per_concept + 3)
        chosen = []
        for video, channel, confidence in rows:
            if video.id in used_videos:
                continue
            used_videos.add(video.id)
            chosen.append(
                {
                    "video": video,
                    "channel": channel,
                    "confidence": confidence,
                    "level": _video_level(video),
                    "watched": video.id in watched,
                }
            )
            if len(chosen) >= max_videos_per_concept:
                break
        if not chosen:
            continue
        concept_level = level_for_score(
            sum(difficulty_score(item["video"].title, item["video"].description or "", item["video"].duration_s) for item in chosen)
            / len(chosen)
        )
        planned.append({"concept": concept, "videos": chosen, "level": concept_level})

    if level != "auto":
        cutoff = LEVEL_ORDER.index(level)
        planned = [p for p in planned if LEVEL_ORDER.index(p["level"]) <= cutoff]
    if not planned:
        raise LearningPathError(
            f"The library has material for this objective but nothing at or below '{level}'."
        )

    path = LearningPath(
        id="path_" + secrets.token_hex(10),
        objective=objective,
        level=level,
        status="active",
        summary=(
            f"{len(planned)} concepts and "
            f"{sum(len(p['videos']) for p in planned)} videos from your subscriptions. "
            "Stages are ordered by explicit prerequisites; difficulty is a surface estimate."
        ),
        created_at=now_ms(),
        updated_at=now_ms(),
    )
    session.add(path)
    session.flush()

    rank = {"beginner": 0, "intermediate": 1, "advanced": 2, "research": 3}
    stage_titles = {
        "beginner": "Foundations",
        "intermediate": "Core concepts",
        "advanced": "Advanced material",
        "research": "Research-level material",
    }
    stages: dict[str, LearningPathStage] = {}
    for planned_item in planned:
        stage_level = planned_item["level"]
        if stage_level not in stages:
            stage = LearningPathStage(
                id="stage_" + secrets.token_hex(8),
                path_id=path.id,
                position=len(stages),
                title=stage_titles[stage_level],
                level=stage_level,
                concept_id=planned_item["concept"].id,
            )
            session.add(stage)
            session.flush()
            stages[stage_level] = stage
    # Ensure stage rows are ordered by level, then renumber positions.
    for index, stage_level in enumerate(sorted(stages, key=lambda lv: rank[lv])):
        stages[stage_level].position = index

    item_positions: dict[str, int] = defaultdict(int)
    for planned_item in planned:
        stage = stages[planned_item["level"]]
        for video_item in planned_item["videos"]:
            video = video_item["video"]
            channel = video_item["channel"]
            reason = (
                f"Covers '{planned_item['concept'].name}' "
                f"(confidence {video_item['confidence']:.2f})"
                + (f" from {channel.name}" if channel else "")
                + (". Already watched locally." if video_item["watched"] else ".")
            )
            session.add(
                LearningPathItem(
                    id="item_" + secrets.token_hex(8),
                    stage_id=stage.id,
                    video_id=video.id,
                    position=item_positions[stage.id],
                    status="done" if video_item["watched"] else "todo",
                    reason=reason,
                    completed_at=now_ms() if video_item["watched"] else None,
                )
            )
            item_positions[stage.id] += 1
    session.flush()

    # Objective terms that no chosen concept covers are reported, not invented.
    covered_terms = set()
    for item in planned:
        covered_terms |= set(words(item["concept"].name))
    missing_terms = sorted({w for w in words(objective) if len(w) > 3 and w not in covered_terms})[:8]
    if missing_terms:
        path.summary += " Objective terms with no matching concept: " + ", ".join(missing_terms) + "."
    session.flush()
    return get_path(session, path.id)


def _item_out(session: Session, item: LearningPathItem) -> dict[str, Any]:
    video = session.get(Video, item.video_id)
    channel = session.get(Channel, video.channel_id) if video and video.channel_id else None
    watched = session.scalar(
        select(WatchHistory.video_id).where(WatchHistory.video_id == item.video_id)
    )
    return {
        "id": item.id,
        "video_id": video.yt_id if video else None,
        "title": video.title if video else None,
        "channel": channel.name if channel else None,
        "duration_s": video.duration_s if video else None,
        "position": item.position,
        "status": item.status,
        "reason": item.reason,
        "completed_at": item.completed_at,
        "watched_locally": bool(watched),
    }


def get_path(session: Session, path_id: str) -> dict[str, Any]:
    path = session.get(LearningPath, path_id)
    if path is None:
        raise LearningPathError("path not found")
    stages = session.scalars(
        select(LearningPathStage)
        .where(LearningPathStage.path_id == path.id)
        .order_by(LearningPathStage.position)
    ).all()
    stage_out = []
    total = done = 0
    next_item = None
    for stage in stages:
        items = session.scalars(
            select(LearningPathItem)
            .where(LearningPathItem.stage_id == stage.id)
            .order_by(LearningPathItem.position)
        ).all()
        items_out = [_item_out(session, item) for item in items]
        total += len(items_out)
        done += sum(1 for item in items_out if item["status"] == "done")
        if next_item is None:
            next_item = next((item for item in items_out if item["status"] == "todo"), None)
        stage_out.append(
            {
                "id": stage.id,
                "position": stage.position,
                "title": stage.title,
                "level": stage.level,
                "items": items_out,
            }
        )
    # Prerequisite gaps: a concept required by a covered concept but absent from the path.
    path_concept_ids = {
        row[0]
        for row in session.execute(
            select(VideoConcept.concept_id)
            .join(LearningPathItem, LearningPathItem.video_id == VideoConcept.video_id)
            .join(LearningPathStage, LearningPathStage.id == LearningPathItem.stage_id)
            .where(LearningPathStage.path_id == path.id)
            .distinct()
        ).all()
    }
    gaps = []
    for relation in session.scalars(
        select(ConceptRelation).where(ConceptRelation.relation == "prerequisite")
    ).all():
        if (
            relation.src_concept_id in path_concept_ids
            and relation.dst_concept_id not in path_concept_ids
        ):
            concept = session.get(Concept, relation.dst_concept_id)
            if concept:
                gaps.append(
                    {
                        "concept": concept.name,
                        "slug": concept.slug,
                        "reason": "prerequisite referenced by a concept already in the path",
                    }
                )
    return {
        "id": path.id,
        "objective": path.objective,
        "level": path.level,
        "status": path.status,
        "summary": path.summary,
        "created_at": path.created_at,
        "updated_at": path.updated_at,
        "stages": stage_out,
        "progress": {
            "total": total,
            "done": done,
            "percent": round(done / total * 100, 1) if total else 0.0,
        },
        "next_item": next_item,
        "prerequisite_gaps": gaps,
        "only_subscription_library": True,
        "difficulty_note": "Stage difficulty is a surface estimate from titles, text and duration.",
    }


def list_paths(session: Session, *, status: str | None = "active") -> list[dict[str, Any]]:
    q = select(LearningPath)
    if status:
        q = q.where(LearningPath.status == status)
    rows = session.scalars(q.order_by(LearningPath.updated_at.desc())).all()
    out = []
    for path in rows:
        data = get_path(session, path.id)
        out.append(
            {
                "id": data["id"],
                "objective": data["objective"],
                "level": data["level"],
                "status": data["status"],
                "progress": data["progress"],
                "stages": len(data["stages"]),
                "updated_at": data["updated_at"],
            }
        )
    return out


def update_path(
    session: Session,
    path_id: str,
    *,
    status: str | None = None,
    objective: str | None = None,
    level: str | None = None,
) -> dict[str, Any]:
    path = session.get(LearningPath, path_id)
    if path is None:
        raise LearningPathError("path not found")
    if status is not None:
        if status not in {"active", "paused", "archived", "completed"}:
            raise LearningPathError("invalid status")
        path.status = status
    if objective is not None and objective.strip():
        path.objective = objective.strip()
    if level is not None:
        if level not in {"auto", *LEVEL_ORDER}:
            raise LearningPathError("invalid level")
        path.level = level
    path.updated_at = now_ms()
    session.flush()
    return get_path(session, path.id)


def set_item_status(session: Session, item_id: str, status: str) -> dict[str, Any]:
    item = session.get(LearningPathItem, item_id)
    if item is None:
        raise LearningPathError("item not found")
    if status not in {"todo", "done", "skipped"}:
        raise LearningPathError("invalid item status")
    item.status = status
    item.completed_at = now_ms() if status == "done" else None
    session.flush()
    return _item_out(session, item)


def move_item(session: Session, item_id: str, *, position: int) -> dict[str, Any]:
    item = session.get(LearningPathItem, item_id)
    if item is None:
        raise LearningPathError("item not found")
    siblings = session.scalars(
        select(LearningPathItem)
        .where(LearningPathItem.stage_id == item.stage_id)
        .order_by(LearningPathItem.position)
    ).all()
    siblings = [row for row in siblings if row.id != item.id]
    position = max(0, min(len(siblings), int(position)))
    siblings.insert(position, item)
    for index, row in enumerate(siblings):
        row.position = index
    session.flush()
    return _item_out(session, item)


def add_item(session: Session, path_id: str, *, stage_id: str, video_yt_id: str, reason: str | None = None) -> dict[str, Any]:
    stage = session.get(LearningPathStage, stage_id)
    if stage is None or stage.path_id != path_id:
        raise LearningPathError("stage not found for this path")
    video = session.scalar(select(Video).where(Video.yt_id == video_yt_id))
    if video is None:
        raise LearningPathError("video not in the subscription library")
    position = int(
        session.scalar(
            select(func.max(LearningPathItem.position)).where(
                LearningPathItem.stage_id == stage.id
            )
        )
        or 0
    ) + 1
    item = LearningPathItem(
        id="item_" + secrets.token_hex(8),
        stage_id=stage.id,
        video_id=video.id,
        position=position,
        status="todo",
        reason=reason or "Added manually from the library.",
    )
    session.add(item)
    session.flush()
    return _item_out(session, item)


def path_updates(session: Session, *, limit: int = 6) -> list[dict[str, Any]]:
    """Suggest newly available videos for active paths (used by the radar)."""
    out: list[dict[str, Any]] = []
    paths = session.scalars(select(LearningPath).where(LearningPath.status == "active")).all()
    for path in paths:
        item_video_ids = {
            row[0]
            for row in session.execute(
                select(LearningPathItem.video_id)
                .join(LearningPathStage, LearningPathStage.id == LearningPathItem.stage_id)
                .where(LearningPathStage.path_id == path.id)
            ).all()
        }
        concept_ids = {
            row[0]
            for row in session.execute(
                select(LearningPathStage.concept_id).where(
                    LearningPathStage.path_id == path.id, LearningPathStage.concept_id.is_not(None)
                )
            ).all()
        }
        if not concept_ids:
            continue
        candidates = session.execute(
            select(Video, VideoConcept, Concept)
            .join(VideoConcept, VideoConcept.video_id == Video.id)
            .join(Concept, Concept.id == VideoConcept.concept_id)
            .where(
                VideoConcept.concept_id.in_(list(concept_ids)),
                Video.is_available == 1,
                Video.id.not_in(item_video_ids) if item_video_ids else True,
                Video.published_at.is_not(None),
            )
            .order_by(Video.published_at.desc())
            .limit(limit)
        ).all()
        for video, _link, concept in candidates:
            out.append(
                {
                    "path_id": path.id,
                    "objective": path.objective,
                    "video_id": video.yt_id,
                    "title": video.title,
                    "concept": concept.name,
                    "published_at": video.published_at,
                    "reason": f"Newer library video covering '{concept.name}' for this path.",
                }
            )
    return out[:limit]
