"""Multi-perspective discovery (Feature 8).

Extends the existing recommender instead of replacing it: candidate videos come
from `app.services.recommendations.recommend` (already restricted to imported
subscriptions and already MMR-diversified). Those candidates are then classified
into complementary perspectives using stored concept links, local embeddings and
surface cues in titles. Every item explains *why* it was selected.

Near-duplicates are suppressed by title-token similarity, so a perspective shelf
does not fill up with five re-uploads of the same talk.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Channel, Concept, ConceptRelation, Video, VideoConcept, WatchHistory
from app.services.ai.embeddings import configured_engine, cosine
from app.services.knowledge.text import difficulty_score, keywords, level_for_score, words
from app.services.recommendations import recommend

PERSPECTIVES = (
    "beginner",
    "advanced",
    "theoretical",
    "practical",
    "real_world",
    "critical",
    "alternative",
    "prerequisite",
)

CUES: dict[str, tuple[str, ...]] = {
    "beginner": ("beginner", "intro", "introduction", "basics", "from scratch", "explained simply", "for beginners", "101"),
    "advanced": ("advanced", "deep dive", "internals", "in depth", "expert", "hard mode", "under the hood"),
    "theoretical": ("theory", "theoretical", "math", "mathematics", "proof", "principle", "fundamentals", "formal"),
    "practical": ("tutorial", "hands-on", "hands on", "build", "project", "walkthrough", "implement", "code along", "demo"),
    "real_world": ("production", "case study", "real world", "industry", "in practice", "deploy", "at scale"),
    "critical": ("mistake", "pitfall", "why not", "critique", "problem with", "anti-pattern", "limitations", "wrong"),
    "prerequisite": ("prerequisite", "before you", "foundations", "getting started", "basics", "fundamentals"),
}


class PerspectiveError(ValueError):
    pass


def _concepts_for(session: Session, video_id: int) -> dict[int, str]:
    rows = session.execute(
        select(Concept.id, Concept.name)
        .join(VideoConcept, VideoConcept.concept_id == Concept.id)
        .where(VideoConcept.video_id == video_id)
    ).all()
    return {int(r[0]): r[1] for r in rows}


def _prerequisite_ids(session: Session, concept_ids: set[int]) -> set[int]:
    if not concept_ids:
        return set()
    rows = session.execute(
        select(ConceptRelation.dst_concept_id).where(
            ConceptRelation.relation == "prerequisite",
            ConceptRelation.src_concept_id.in_(list(concept_ids)),
        )
    ).all()
    return {int(r[0]) for r in rows}


def _title_signature(title: str) -> set[str]:
    return {w for w in words(title) if len(w) > 2}


def _is_duplicate(title: str, chosen: list[set[str]]) -> bool:
    sig = _title_signature(title)
    if not sig:
        return False
    for other in chosen:
        union = sig | other
        if union and len(sig & other) / len(union) >= 0.8:
            return True
    return False


def _classify(
    *,
    title: str,
    shared: list[str],
    similarity: float,
    candidate_level: str,
    seed_level: str,
    is_prerequisite: bool,
    different_channel: bool,
) -> tuple[str, str]:
    low = title.lower()
    cue_hits = {category: [cue for cue in cues if cue in low] for category, cues in CUES.items()}
    for category in ("critical", "practical", "real_world", "theoretical", "beginner", "advanced"):
        if cue_hits[category]:
            cue = cue_hits[category][0]
            return category, f"title contains '{cue}'"
    if is_prerequisite:
        return "prerequisite", "its concepts are prerequisites of concepts in the seed video"
    if shared and different_channel:
        return "alternative", "a different channel explains the shared concepts"
    if shared:
        return "alternative", "the same concepts are revisited in another video"
    order = {"beginner": 0, "intermediate": 1, "advanced": 2, "research": 3}
    if order.get(candidate_level, 1) < order.get(seed_level, 1):
        return "beginner", f"estimated {candidate_level} level vs the seed's {seed_level}"
    if order.get(candidate_level, 1) > order.get(seed_level, 1):
        return "advanced", f"estimated {candidate_level} level vs the seed's {seed_level}"
    return "practical", "semantically close library video"


def perspectives_for(
    session: Session, video_yt_id: str, *, limit: int = 12
) -> dict[str, Any]:
    seed = session.scalar(select(Video).where(Video.yt_id == video_yt_id))
    if seed is None:
        raise PerspectiveError("video not found")
    seed_concepts = _concepts_for(session, seed.id)
    seed_prereqs = _prerequisite_ids(session, set(seed_concepts))
    engine = configured_engine()
    seed_text = f"{seed.title} {seed.description or ''}"
    seed_vec = engine.embed(seed_text)
    seed_level = level_for_score(difficulty_score(seed.title, seed.description or "", seed.duration_s))
    watched = {
        int(row[0])
        for row in session.execute(select(WatchHistory.video_id).where(WatchHistory.completed == 1)).all()
    }

    chosen_by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    chosen_titles: list[set[str]] = []
    channels_used: dict[str, set[int]] = defaultdict(set)
    candidates = recommend(session, count=max(120, limit * 12))

    for candidate in candidates:
        video = candidate.video
        channel = candidate.channel
        if video.id == seed.id:
            continue
        cand_concepts = _concepts_for(session, video.id)
        shared_ids = set(seed_concepts) & set(cand_concepts)
        shared = [seed_concepts[cid] for cid in sorted(shared_ids)]
        similarity = cosine(seed_vec, engine.embed(f"{video.title} {video.description or ''}"))
        is_prereq = bool(set(cand_concepts) & seed_prereqs)
        if not shared and similarity < 0.22 and not is_prereq:
            continue
        if _is_duplicate(video.title, chosen_titles):
            continue
        cand_level = level_for_score(difficulty_score(video.title, video.description or "", video.duration_s))
        category, cue_reason = _classify(
            title=video.title,
            shared=shared,
            similarity=similarity,
            candidate_level=cand_level,
            seed_level=seed_level,
            is_prerequisite=is_prereq,
            different_channel=channel.id != seed.channel_id,
        )
        if len(chosen_by_category[category]) >= 2:
            continue
        if channel.id in channels_used[category] and len(chosen_by_category[category]) >= 1:
            continue
        evidence = {
            "shared_concepts": shared[:6],
            "embedding_similarity": round(similarity, 3),
            "title_cue": cue_reason,
            "different_channel": channel.id != seed.channel_id,
            "estimated_level": cand_level,
            "prerequisite_of_seed_concepts": is_prereq,
        }
        reason = (
            f"{category.replace('_', ' ').capitalize()} perspective because {cue_reason}"
            + (f"; shares {', '.join(shared[:3])}" if shared else "")
            + f"; similarity {similarity:.2f}"
            + ("" if channel.id != seed.channel_id else "; same channel")
            + "."
        )
        chosen_by_category[category].append(
            {
                "video_id": video.yt_id,
                "title": video.title,
                "channel": channel.name,
                "channel_id": channel.yt_id,
                "duration_s": video.duration_s,
                "perspective": category,
                "reason": reason,
                "evidence": evidence,
                "watched_locally": video.id in watched,
                "score": round(float(candidate.score or 0), 4),
            }
        )
        chosen_titles.append(_title_signature(video.title))
        channels_used[category].add(channel.id)
        if sum(len(v) for v in chosen_by_category.values()) >= limit:
            break

    items = [item for category in PERSPECTIVES for item in chosen_by_category.get(category, [])]
    return {
        "video_id": seed.yt_id,
        "title": seed.title,
        "perspectives": {category: chosen_by_category.get(category, []) for category in PERSPECTIVES},
        "items": items,
        "coverage": [category for category in PERSPECTIVES if not chosen_by_category.get(category)],
        "only_subscription_library": True,
        "notes": (
            "Candidates come from the existing subscription-only recommender. "
            "Perspective labels are deterministic heuristics from titles, concept links and "
            "local embeddings; every item shows its evidence."
        ),
    }
