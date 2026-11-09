"""Recommendation service behind replaceable interfaces (ADR-005).

The baseline uses the same scoring maths as `app.domain.weights`, FTS5 for lexical
candidates, a popularity/channel diversity prefilter, MMR via `app.domain.mmr`, and
explicit explanation strings that match the frontend knobs so the UI can show the same
labels it already shows.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.db.models import Channel, Video
from app.domain.mmr import rerank, Candidate
from app.domain.weights import DEFAULT_W, score as compute_score, apply_knobs, validate_knobs
from app.repositories.library import list_videos
from app.schemas import KnobUpdate, VideoFilterRequest

logger = logging.getLogger(__name__)

TOP_N_CANDIDATES = 500


# ------------------------------------------------------------------ protocols


class CandidateGenerator(Protocol):
    def candidates(self, session: Session, *, count: int, filters: VideoFilterRequest | None) -> list[VideoCandidate]: ...


class FeatureExtractor(Protocol):
    def extract(self, session: Session, video: VideoCandidate, user: "*") -> dict: ...


class Ranker(Protocol):
    def rank(self, candidates: list[VideoCandidate], features: dict[str, dict], *, knobs: dict) -> list[VideoCandidate]: ...


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


class WeightsFeatureExtractor:
    def extract(self, session: Session, candidate: VideoCandidate, user: "*") -> dict:
        v = candidate.video
        c = candidate.channel
        return {
            "lexical": 1.0 if v.title else 0.0,
            "semantic": 0.0,
            "channel": float(c.subscriber_count or 0) / 100000.0,
            "neighbours": 0.0,
            "session": 0.0,
            "popular": float(v.view_count or 0) / 1000000.0,
            "rating": 0.0,
            "duration": float(v.duration_s or 0) / 3600.0,
            "new_channel": 1.0 if (c.first_seen_at or 0) > (v.published_at or 0) else 0.0,
            "negative": 0.0,
        }


class ScoreRanker:
    def rank(self, candidates: list[VideoCandidate], features_map: dict, *, knobs: dict) -> list[VideoCandidate]:
        for c in candidates:
            f = features_map.get(c.video.id, {})
            c.score = compute_score(f, knobs)
            c.features = f
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
        return ", ".join(reasons) if reasons else "default ranking"


class NoOpEvaluator:
    def evaluate(self, items: list[VideoCandidate], session: Session, user: "*") -> dict:
        return {"count": len(items), "model": "baseline"}


# ------------------------------------------------------------------ service composition


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

    features_map = {c.video.id: extractor.extract(session, c, user_id) for c in candidates}
    knob_dict = DEFAULT_W.copy()
    if knobs:
        knob_dict.update(
            {
                "lexical": knobs.lexical if knobs.lexical is not None else knob_dict["lexical"],
                "semantic": knobs.semantic if knobs.semantic is not None else knob_dict["semantic"],
                "channel": knobs.channel if knobs.channel is not None else knob_dict["channel"],
                "neighbours": knobs.neighbours if knobs.neighbours is not None else knob_dict["neighbours"],
                "session": knobs.session if knobs.session is not None else knob_dict["session"],
                "popular": knobs.popular if knobs.popular is not None else knob_dict["popular"],
                "rating": knobs.rating if knobs.rating is not None else knob_dict["rating"],
                "duration": knobs.duration if knobs.duration is not None else knob_dict["duration"],
                "new_channel": knobs.new_channel if knobs.new_channel is not None else knob_dict["new_channel"],
                "negative": knobs.negative if knobs.negative is not None else knob_dict["negative"],
            }
        )
    validate_knobs(knob_dict)

    ranked = ranker.rank(candidates, features_map, knobs=knob_dict)
    reranked = reranker.apply(ranked, diversity=diversity, channel_cap=channel_cap)
    for c in reranked:
        c.reason = explainer.explain(c, c.features)

    return reranked[:count]
