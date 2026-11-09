"""Knowledge and interest evolution timeline (Feature 9).

Built exclusively from stored events: watch sessions, aggregated watch history,
flashcard reviews, extracted knowledge, concepts and learning-path completions.

The response keeps five different things separate, because watching a video is not
mastery:

- exposure      distinct videos touched;
- engagement    seconds actually watched (My Tube sessions);
- completion    sessions/videos marked complete;
- revision      flashcards reviewed;
- retention     share of Good/Easy reviews (a proxy, explicitly not mastery).

Every inferred interest shift lists the bucket and the counts that produced it.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Channel,
    Concept,
    KnowledgeExtract,
    LearningPathItem,
    LearningPathStage,
    ReviewLog,
    Video,
    VideoConcept,
    WatchHistory,
    WatchSession,
)
from app.services.knowledge.text import difficulty_score


def _bucket_key(ms: int, period: str) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    if period == "week":
        iso = dt.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    return dt.strftime("%Y-%m")


def _bucket_labels(period: str, count: int, now: datetime) -> list[str]:
    if period == "week":
        labels = []
        cursor = now
        for _ in range(count):
            iso = cursor.isocalendar()
            labels.append(f"{iso.year}-W{iso.week:02d}")
            cursor -= timedelta(days=7)
        return list(reversed(labels))
    labels = []
    year, month = now.year, now.month
    for _ in range(count):
        labels.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return list(reversed(labels))


def _topic_map(session: Session, video_ids: set[int]) -> dict[int, str]:
    if not video_ids:
        return {}
    rows = session.execute(
        select(VideoConcept.video_id, Concept.name, VideoConcept.weight)
        .join(Concept, Concept.id == VideoConcept.concept_id)
        .where(VideoConcept.video_id.in_(list(video_ids)))
        .order_by(VideoConcept.weight.desc())
    ).all()
    out: dict[int, str] = {}
    for video_id, name, _weight in rows:
        out.setdefault(int(video_id), name)
    return out


def build_timeline(
    session: Session,
    *,
    period: str = "month",
    buckets: int = 12,
    now: int | None = None,
) -> dict[str, Any]:
    if period not in {"month", "week"}:
        raise ValueError("period must be month or week")
    now = now or int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    labels = _bucket_labels(period, max(2, min(52, buckets)), datetime.fromtimestamp(now / 1000, tz=timezone.utc))
    since = now - (buckets * (7 if period == "week" else 31) + 40) * 86_400_000
    bucket_set = set(labels)
    state: dict[str, dict[str, Any]] = {
        label: {
            "bucket": label,
            "exposed_videos": set(),
            "engagement_s": 0.0,
            "completed": 0,
            "reviews": 0,
            "good_or_easy": 0,
            "new_concepts": 0,
            "knowledge_items": 0,
            "courses_completed": 0,
            "topics": Counter(),
            "channels": Counter(),
        }
        for label in labels
    }

    def bucket_of(ms: int | None) -> dict[str, Any] | None:
        if not ms:
            return None
        label = _bucket_key(ms, period)
        return state.get(label)

    # Watch sessions (My Tube playback).
    sessions = session.scalars(select(WatchSession).where(WatchSession.started_at >= since)).all()
    session_video_ids = {int(s.video_id) for s in sessions}
    history = session.scalars(
        select(WatchHistory).where(WatchHistory.last_watched_at.is_not(None), WatchHistory.last_watched_at >= since)
    ).all()
    all_video_ids = session_video_ids | {int(h.video_id) for h in history}
    topics = _topic_map(session, all_video_ids)
    channel_of = {
        int(row[0]): row[1]
        for row in session.execute(select(Video.id, Video.channel_id).where(Video.id.in_(list(all_video_ids)))).all()
    } if all_video_ids else {}
    channel_names = {
        int(row[0]): row[1]
        for row in session.execute(select(Channel.id, Channel.name)).all()
    }

    session_by_bucket: dict[str, list[WatchSession]] = defaultdict(list)
    for ws in sessions:
        bucket = bucket_of(ws.started_at)
        if bucket is None:
            continue
        bucket["exposed_videos"].add(int(ws.video_id))
        bucket["engagement_s"] += float(ws.watched_s or 0)
        if int(ws.completed or 0):
            bucket["completed"] += 1
        session_by_bucket[bucket["bucket"]].append(ws)
        topic = topics.get(int(ws.video_id))
        if topic:
            bucket["topics"][topic] += 1
        channel_id = channel_of.get(int(ws.video_id))
        if channel_id:
            bucket["channels"][channel_names.get(int(channel_id), "")] += 1

    # Aggregated history fills exposure/completion for older or externally initiated watches.
    for row in history:
        bucket = bucket_of(row.last_watched_at)
        if bucket is None:
            continue
        bucket["exposed_videos"].add(int(row.video_id))
        if int(row.completed or 0):
            bucket["completed"] += 1
        if not session_by_bucket.get(bucket["bucket"]):
            bucket["engagement_s"] += float(row.total_watch_s or 0)
        topic = topics.get(int(row.video_id))
        if topic:
            bucket["topics"][topic] += 1
        channel_id = channel_of.get(int(row.video_id))
        if channel_id:
            bucket["channels"][channel_names.get(int(channel_id), "")] += 1

    for log in session.scalars(select(ReviewLog).where(ReviewLog.reviewed_at >= since)).all():
        bucket = bucket_of(log.reviewed_at)
        if bucket is None:
            continue
        bucket["reviews"] += 1
        if int(log.rating) >= 3:
            bucket["good_or_easy"] += 1

    for extract in session.scalars(select(KnowledgeExtract).where(KnowledgeExtract.created_at >= since)).all():
        bucket = bucket_of(extract.created_at)
        if bucket:
            bucket["knowledge_items"] += 1
    for concept in session.scalars(select(Concept).where(Concept.created_at >= since)).all():
        bucket = bucket_of(concept.created_at)
        if bucket:
            bucket["new_concepts"] += 1
    for item in session.scalars(
        select(LearningPathItem).where(LearningPathItem.completed_at.is_not(None), LearningPathItem.completed_at >= since)
    ).all():
        bucket = bucket_of(item.completed_at)
        if bucket:
            bucket["courses_completed"] += 1

    timeline = []
    for label in labels:
        bucket = state[label]
        timeline.append(
            {
                "bucket": label,
                "exposure": {
                    "distinct_videos": len(bucket["exposed_videos"]),
                    "label": "videos touched",
                },
                "engagement_s": round(bucket["engagement_s"], 1),
                "completion": {"completed": bucket["completed"]},
                "revision": {"reviews": bucket["reviews"], "good_or_easy": bucket["good_or_easy"]},
                "knowledge_growth": {
                    "new_concepts": bucket["new_concepts"],
                    "extracted_items": bucket["knowledge_items"],
                },
                "learning_path_completions": bucket["courses_completed"],
                "top_topics": [{"topic": topic, "count": n} for topic, n in bucket["topics"].most_common(6)],
                "top_channels": [{"channel": ch, "count": n} for ch, n in bucket["channels"].most_common(5)],
            }
        )

    # Overall topic counts and shifts.
    topic_totals: Counter[str] = Counter()
    topic_bucket_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for label in labels:
        for topic, count in state[label]["topics"].items():
            topic_totals[topic] += count
            topic_bucket_counts[topic][label] += count
    half = max(1, len(labels) // 2)
    recent_labels = set(labels[-half:])
    prior_labels = set(labels[:-half]) or set(labels[:1])
    emerging = []
    declining = []
    for topic, per_bucket in topic_bucket_counts.items():
        recent = sum(per_bucket[l] for l in recent_labels)
        prior = sum(per_bucket[l] for l in prior_labels)
        if recent >= 2 and recent > prior:
            emerging.append({"topic": topic, "recent": recent, "prior": prior, "delta": recent - prior})
        elif prior >= 2 and prior > recent:
            declining.append({"topic": topic, "recent": recent, "prior": prior, "delta": recent - prior})
    emerging.sort(key=lambda item: (-item["delta"], item["topic"]))
    declining.sort(key=lambda item: (item["delta"], item["topic"]))

    # Transitions: change of the dominant topic between consecutive non-empty buckets.
    transitions = []
    previous_topic = None
    previous_label = None
    for label in labels:
        topics_here = state[label]["topics"]
        if not topics_here:
            continue
        top_topic, top_count = topics_here.most_common(1)[0]
        if previous_topic and top_topic != previous_topic:
            transitions.append(
                {
                    "from": previous_topic,
                    "to": top_topic,
                    "from_bucket": previous_label,
                    "to_bucket": label,
                    "evidence": {
                        "from_count": state[previous_label]["topics"].get(previous_topic, 0),
                        "to_count": top_count,
                        "rule": "dominant topic changed between non-empty buckets",
                    },
                    "claim_type": "inferred",
                }
            )
        previous_topic = top_topic
        previous_label = label

    # Revisited concepts: watched videos on a topic in at least two buckets.
    revisited = [
        {
            "topic": topic,
            "buckets": sorted(per_bucket),
            "watch_count": sum(per_bucket.values()),
        }
        for topic, per_bucket in topic_bucket_counts.items()
        if sum(1 for count in per_bucket.values() if count > 0) >= 2
    ]
    revisited.sort(key=lambda item: -item["watch_count"])

    channel_totals: Counter[str] = Counter()
    for label in labels:
        channel_totals.update(state[label]["channels"])
    total_reviews = sum(row["revision"]["reviews"] for row in timeline)
    good = sum(row["revision"]["good_or_easy"] for row in timeline)
    retention = good / total_reviews if total_reviews else None

    return {
        "period": period,
        "buckets": timeline,
        "summary": {
            "top_subjects": [{"topic": t, "count": n} for t, n in topic_totals.most_common(10)],
            "emerging_interests": emerging[:6],
            "declining_interests": declining[:6],
            "transitions": transitions,
            "most_watched_channels": [
                {"channel": ch, "count": n} for ch, n in channel_totals.most_common(8) if ch
            ],
            "learning_sessions": len(sessions),
            "videos_completed": sum(row["completion"]["completed"] for row in timeline),
            "revision": {
                "reviews": total_reviews,
                "retention_proxy": round(retention, 3) if retention is not None else None,
            },
            "knowledge_growth": {
                "new_concepts": sum(row["knowledge_growth"]["new_concepts"] for row in timeline),
                "extracted_items": sum(row["knowledge_growth"]["extracted_items"] for row in timeline),
            },
            "revisited_concepts": revisited[:10],
        },
        "metric_definitions": {
            "exposure": "Distinct videos touched; not mastery.",
            "engagement": "Seconds recorded by My Tube watch sessions.",
            "completion": "Videos/sessions marked complete in My Tube.",
            "revision": "Flashcard reviews and the Good/Easy share (retention proxy).",
            "claim_types": {
                "measured": "directly counted from stored rows",
                "inferred": "computed shift with rule and counts shown",
            },
        },
        "note": (
            "All figures come from stored My Tube events. Watching a video is exposure, not "
            "evidence of mastery; retention is a flashcard proxy only."
        ),
    }


def topic_difficulty(session: Session, topic: str) -> dict[str, Any]:
    """Surface-only difficulty estimate for one topic (used by the UI)."""
    rows = session.execute(
        select(Video)
        .join(VideoConcept, VideoConcept.video_id == Video.id)
        .join(Concept, Concept.id == VideoConcept.concept_id)
        .where(Concept.name == topic)
    ).scalars().all()
    if not rows:
        return {"topic": topic, "videos": 0, "estimated_difficulty": None}
    scores = [difficulty_score(v.title, v.description or "", v.duration_s) for v in rows]
    return {
        "topic": topic,
        "videos": len(rows),
        "estimated_difficulty": round(sum(scores) / len(scores), 3),
        "note": "Surface estimate from titles/descriptions/duration.",
    }
