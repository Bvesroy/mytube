"""Recommendation service behind replaceable interfaces (ADR-005).

The baseline uses the same scoring maths as `app.domain.weights`, FTS5 for lexical
candidates, a popularity/channel diversity prefilter, MMR via `app.domain.mmr`, and
explicit explanation strings that match the frontend knobs so the UI can show the same
labels it already shows.

Phase 4: consumes My Tube watch events (WatchHistory) to populate the `negative`
feature for already-completed/strongly-watched videos. Algorithms and weight
definitions (`DEFAULT_W`, `score`, MMR) are preserved; only feature extraction is
enriched with watch-event signals.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Channel, Video, WatchHistory
from app.domain.mmr import rerank, Candidate
from app.domain.weights import DEFAULT_W, FEATURES, score as compute_score, weight_vector
from app.repositories.library import list_videos
from app.schemas import KnobUpdate, VideoFilterRequest

logger = logging.getLogger(__name__)

TOP_N_CANDIDATES = 500

# Feature-name mapping: schema/extractor snake_case -> domain FEATURES camelCase where needed.
_FEATURE_KEYS = {
    "lexical": "lexical", "semantic": "semantic", "channel": "channel",
    "neighbours": "neighbours", "session": "session", "popular": "popular",
    "rating": "rating", "duration": "duration", "newChannel": "newChannel",
    "negative": "negative",
}


# ------------------------------------------------------------------ protocols


class CandidateGenerator(Protocol):
    def candidates(self, session: Session, *, count: int, filters: VideoFilterRequest | None) -> list[VideoCandidate]: ...


class FeatureExtractor(Protocol):
    def extract(self, session: Session, candidate: VideoCandidate, user: "*", history: dict | None = None) -> dict: ...


class Ranker(Protocol):
    def rank(self, candidates: list[VideoCandidate], features: dict[str, dict], *, weights: Sequence[float]) -> list[VideoCandidate]: ...


class Reranker(Protocol):
    def apply(self, ranked: list[VideoCandidate], *, diversity: float, channel_cap: int | None) -> list[VideoCandidate]: ...


class Explainer(Protocol):
    def explain(self, item: VideoCandidate, features: dict) -> str: ...


class Evaluator(Protocol):
    def evaluate(self, items: list[VideoCandidate], session: Session, user: "*") -> dict: ...


# ------------------------------------------------------------------ concrete record


@dataclass
class VideoCandidate:
    video: Video
    channel: Channel
    score: float = 0.0
    reason: str = ""
    features: dict = field(default_factory=dict)


# ------------------------------------------------------------------ baseline implementations


class FtsCandidateGenerator:
    def candidates(
        self, session: Session, *, count: int = TOP_N_CANDIDATES, filters: VideoFilterRequest | None = None
    ) -> list[VideoCandidate]:
        rows, _ = list_videos(
            session,
            limit=count,
            channel_id=None,
            category_id=None,
            min_duration=filters.duration_min_s if filters else None,
            max_duration=filters.duration_max_s if filters else None,
            available_only=True,
            sort="recent",
        )
        return [
            VideoCandidate(video=v, channel=c, features={})
            for v, c in rows
        ]


def _watch_negative(h: WatchHistory | None) -> float:
    """Conservative My Tube watch-event penalty for the `negative` feature.

    Uses completed/engagement signals; preserves the engine (negative weight -2.0).
    External initiation alone (no verified watch) is not treated as completion.
    Defensive about model fields across schema revisions.
    """
    if not h:
        return 0.0
    try:
        if int(getattr(h, "completed", 0) or 0):
            return 0.7
    except Exception:
        pass
    # Fallback: strong engagement/watch-time hint (legacy/phase2 fields may vary).
    eng = float(getattr(h, "engagement", 0.0) or 0.0)
    tw = float(getattr(h, "total_watch_s", 0.0) or 0.0)
    if eng >= 1.2 or tw >= 240:
        return 0.35
    if eng >= 0.8 or tw >= 60:
        return 0.15
    return 0.0


class WeightsFeatureExtractor:
    def extract(self, session: Session, candidate: VideoCandidate, user: "*", history: dict | None = None) -> dict:
        v = candidate.video
        c = candidate.channel
        h = (history or {}).get(v.id)
        return {
            "lexical": 1.0 if v.title else 0.0,
            "semantic": 0.0,
            "channel": float(c.subscriber_count or 0) / 100000.0,
            "neighbours": 0.0,
            "session": 0.0,
            "popular": float(v.view_count or 0) / 1000000.0,
            "rating": 0.0,
            "duration": float(v.duration_s or 0) / 3600.0,
            "newChannel": 1.0 if (c.first_seen_at or 0) > (v.published_at or 0) else 0.0,
            "negative": _watch_negative(h),
        }


def _feature_vector(features: dict) -> list[float]:
    # FEATURES order: lexical, semantic, channel, neighbours, session, popular, rating, duration, newChannel, negative
    return [float(features.get(k, 0.0) or 0.0) for k in FEATURES]


class ScoreRanker:
    def rank(self, candidates: list[VideoCandidate], features_map: dict, *, weights: Sequence[float]) -> list[VideoCandidate]:
        for c in candidates:
            f = features_map.get(c.video.id, {})
            c.features = f
            c.score = compute_score(_feature_vector(f), weights)
        candidates.sort(key=lambda x: x.score, reverse=True)
        return candidates


class MMRReranker:
    def apply(self, ranked: list[VideoCandidate], *, diversity: float = 30.0, channel_cap: int | None = None) -> list[VideoCandidate]:
        candidates = [
            Candidate(
                video_id=c.video.yt_id,
                channel_id=c.channel.id,
                score=c.score,
                vector=None,
                payload=c,
            )
            for c in ranked
        ]
        selected = rerank(candidates, limit=len(candidates), serendipity=diversity, cap=channel_cap)
        return [c.payload for c in selected]


class DefaultExplainer:
    def explain(self, item: VideoCandidate, features: dict) -> str:
        reasons = []
        if features.get("lexical"):
            reasons.append("lexical match")
        if features.get("channel"):
            reasons.append("channel relevance")
        if features.get("popular"):
            reasons.append("popularity")
        if features.get("session"):
            reasons.append("session match")
        if features.get("rating"):
            reasons.append("rating")
        if features.get("negative"):
            reasons.append("already watched")
        return ", ".join(reasons) if reasons else "default ranking"


class NoOpEvaluator:
    def evaluate(self, items: list[VideoCandidate], session: Session, user: "*") -> dict:
        return {"count": len(items), "model": "baseline"}


# ------------------------------------------------------------------ service composition


def _load_watch_history(session: Session, user_id: str, video_ids: list[int]) -> dict[int, WatchHistory]:
    """Batch-load My Tube watch history. WatchHistory is per-video (local-first model);
    `user_id` is accepted for API compatibility but not used as a DB filter."""
    if not video_ids:
        return {}
    rows = session.scalars(
        select(WatchHistory).where(WatchHistory.video_id.in_(video_ids))
    ).all()
    return {r.video_id: r for r in rows}


def _weight_overrides(knobs: KnobUpdate | None) -> dict:
    if not knobs:
        return {}
    # map schema snake_case -> domain key names where needed
    m = {
        "lexical": knobs.lexical, "semantic": knobs.semantic, "channel": knobs.channel,
        "neighbours": knobs.neighbours, "session": knobs.session, "popular": knobs.popular,
        "rating": knobs.rating, "duration": knobs.duration, "newChannel": knobs.new_channel,
        "negative": knobs.negative,
    }
    return {k: float(v) for k, v in m.items() if v is not None}


def recommend(
    session: Session,
    *,
    user_id: str = "default",
    count: int = 25,
    filters: VideoFilterRequest | None = None,
    knobs: KnobUpdate | None = None,
    diversity: float = 30.0,
    channel_cap: int | None = None,
) -> list[VideoCandidate]:
    generator = FtsCandidateGenerator()
    extractor = WeightsFeatureExtractor()
    ranker = ScoreRanker()
    reranker = MMRReranker()
    explainer = DefaultExplainer()

    candidates = generator.candidates(session, count=TOP_N_CANDIDATES, filters=filters)
    if not candidates:
        return []

    history = _load_watch_history(session, user_id, [c.video.id for c in candidates])
    features_map = {c.video.id: extractor.extract(session, c, user_id, history=history) for c in candidates}

    overrides = _weight_overrides(knobs)
    weights = weight_vector({**DEFAULT_W, **overrides})

    ranked = ranker.rank(candidates, features_map, weights=weights)
    reranked = reranker.apply(ranked, diversity=diversity, channel_cap=channel_cap)
    for c in reranked:
        c.reason = explainer.explain(c, c.features)

    return reranked[:count]
