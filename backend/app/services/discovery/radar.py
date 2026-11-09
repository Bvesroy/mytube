"""Autonomous Discovery Radar (Feature 10).

Scans only the user's already-imported subscription library. It never searches the
wider internet, never introduces an unsubscribed channel and never uploads viewing
data. All scoring is deterministic and every event carries a human-readable reason.

The radar can be disabled or paused, and the scheduler window is configurable. A
background job runs the same `scan()` function; it is not started automatically.
"""

from __future__ import annotations

import json
import secrets
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Channel,
    Concept,
    DiscoveryEvent,
    DiscoveryReport,
    LearningPath,
    LearningPathItem,
    LearningPathStage,
    UserPreference,
    Video,
    VideoConcept,
    WatchHistory,
    WatchSession,
    now_ms,
)
from app.services.learning.paths import path_updates

CONFIG_KEY = "radar_config"
DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "paused": False,
    "schedule": "daily",
    "window_days": 30,
    "max_events": 24,
    "per_kind": 6,
    "include_new_uploads": True,
    "include_underwatched": True,
    "include_topic_clusters": True,
    "include_cross_channel": True,
    "include_overlooked": True,
    "include_objectives": True,
    "include_connections": True,
    "include_path_updates": True,
}


def get_config(session: Session) -> dict[str, Any]:
    row = session.get(UserPreference, CONFIG_KEY)
    stored: dict[str, Any] = {}
    if row and row.value_json:
        try:
            value = json.loads(row.value_json)
            if isinstance(value, dict):
                stored = value
        except json.JSONDecodeError:
            stored = {}
    return {**DEFAULT_CONFIG, **stored}


def update_config(session: Session, patch: dict[str, Any]) -> dict[str, Any]:
    current = get_config(session)
    for key, value in patch.items():
        if key not in DEFAULT_CONFIG:
            continue
        if key == "schedule" and value not in {"daily", "weekly", "manual"}:
            raise ValueError("schedule must be daily, weekly or manual")
        if key in {"window_days", "max_events", "per_kind"}:
            try:
                value = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} must be an integer") from exc
            if key == "window_days":
                value = max(1, min(365, value))
            else:
                value = max(1, min(200, value))
        if key in {"enabled", "paused"} or key.startswith("include_"):
            value = bool(value)
        current[key] = value
    row = session.get(UserPreference, CONFIG_KEY)
    if row is None:
        row = UserPreference(key=CONFIG_KEY, value_json=json.dumps(current), updated_at=now_ms())
        session.add(row)
    else:
        row.value_json = json.dumps(current)
        row.updated_at = now_ms()
    session.flush()
    return current


def _watched_ids(session: Session) -> set[int]:
    ids = {
        int(row[0])
        for row in session.execute(
            select(WatchHistory.video_id).where(WatchHistory.completed == 1)
        ).all()
    }
    ids |= {
        int(row[0])
        for row in session.execute(
            select(WatchSession.video_id).where(WatchSession.completed == 1)
        ).all()
    }
    return ids


def _channel_names(session: Session) -> dict[int, str]:
    return {int(row[0]): row[1] for row in session.execute(select(Channel.id, Channel.name)).all()}


def _event(
    *,
    kind: str,
    video: Video | None,
    channel_id: int | None,
    score: float,
    reason: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "video_id": video.yt_id if video else None,
        "title": video.title if video else None,
        "channel_id": channel_id,
        "score": round(float(score), 4),
        "reason": reason,
        "payload": payload or {},
    }


def scan(
    session: Session,
    *,
    period: str = "manual",
    window_days: int | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    config = get_config(session)
    if not config["enabled"]:
        return {"enabled": False, "reason": "Discovery is disabled in settings.", "events": []}
    if config["paused"]:
        return {"enabled": True, "paused": True, "reason": "Discovery is paused.", "events": []}
    if period not in {"daily", "weekly", "manual"}:
        raise ValueError("period must be daily, weekly or manual")
    now = now or now_ms()
    window_days = int(window_days or config["window_days"])
    window_start = now - window_days * 86_400_000
    watched = _watched_ids(session)
    channels = _channel_names(session)
    events: list[dict[str, Any]] = []
    per_kind = max(1, int(config["per_kind"]))

    videos = list(
        session.scalars(
            select(Video).where(Video.is_available == 1).order_by(Video.published_at.desc())
        ).all()
    )
    by_id = {v.id: v for v in videos}

    if config["include_new_uploads"]:
        fresh = [
            v for v in videos
            if (v.published_at or 0) >= window_start and v.id not in watched
        ][: per_kind * 2]
        for video in fresh[:per_kind]:
            age_days = max(0.0, (now - (video.published_at or now)) / 86_400_000)
            events.append(
                _event(
                    kind="new_upload",
                    video=video,
                    channel_id=channels.get(video.channel_id or 0),
                    score=0.75 + max(0.0, 0.2 * (1 - age_days / max(1, window_days))),
                    reason=f"New upload from {channels.get(video.channel_id or 0, 'your subscriptions')} in the last {window_days} days, not watched yet.",
                    payload={"published_at": video.published_at, "age_days": round(age_days, 1)},
                )
            )

    if config["include_underwatched"]:
        per_channel_total = Counter(v.channel_id for v in videos)
        per_channel_watched = Counter(
            v.channel_id for v in videos if v.id in watched
        )
        candidates = []
        for channel_id, total in per_channel_total.items():
            if not channel_id or total < 3:
                continue
            ratio = per_channel_watched[channel_id] / total
            if ratio <= 0.25:
                options = [v for v in videos if v.channel_id == channel_id and v.id not in watched]
                if not options:
                    continue
                best = max(options, key=lambda v: (v.view_count or 0, v.published_at or 0))
                candidates.append(
                    _event(
                        kind="underwatched_channel",
                        video=best,
                        channel_id=channels.get(channel_id),
                        score=0.4 + (1 - ratio) * 0.35,
                        reason=(
                            f"You watched {per_channel_watched[channel_id]}/{total} videos "
                            f"from {channels.get(channel_id, 'this channel')}."
                        ),
                        payload={"watched": per_channel_watched[channel_id], "total": total},
                    )
                )
        candidates.sort(key=lambda item: -item["score"])
        events.extend(candidates[:per_kind])

    concept_rows = session.execute(
        select(Concept, func.count(VideoConcept.video_id).label("videos"))
        .join(VideoConcept, VideoConcept.concept_id == Concept.id)
        .group_by(Concept.id)
        .having(func.count(VideoConcept.video_id) >= 2)
    ).all()
    concept_videos: dict[int, list[Video]] = defaultdict(list)
    for link in session.scalars(select(VideoConcept)).all():
        video = by_id.get(link.video_id)
        if video is not None:
            concept_videos[link.concept_id].append(video)

    if config["include_topic_clusters"]:
        clusters = []
        for concept, count in concept_rows:
            if (concept.created_at or 0) < window_start or count < 2:
                continue
            channels_here = {v.channel_id for v in concept_videos.get(concept.id, [])}
            if len(channels_here) < 1:
                continue
            options = [v for v in concept_videos.get(concept.id, []) if v.id not in watched]
            if not options:
                continue
            best = max(options, key=lambda v: (v.view_count or 0, v.published_at or 0))
            clusters.append(
                _event(
                    kind="topic_cluster",
                    video=best,
                    channel_id=channels.get(best.channel_id or 0),
                    score=0.55 + min(0.25, len(channels_here) * 0.08),
                    reason=(
                        f"New topic cluster around '{concept.name}' "
                        f"({count} videos, {len(channels_here)} channel(s)); this one is unwatched."
                    ),
                    payload={
                        "concept": concept.name,
                        "concept_slug": concept.slug,
                        "video_count": count,
                        "channel_count": len(channels_here),
                    },
                )
            )
        clusters.sort(key=lambda item: -item["score"])
        events.extend(clusters[:per_kind])

    if config["include_cross_channel"]:
        cross = []
        for concept, count in concept_rows:
            members = concept_videos.get(concept.id, [])
            channel_set = {v.channel_id for v in members}
            if len(channel_set) < 2:
                continue
            options = [v for v in members if v.id not in watched]
            if not options:
                continue
            best = max(options, key=lambda v: (v.view_count or 0))
            cross.append(
                _event(
                    kind="cross_channel_related",
                    video=best,
                    channel_id=channels.get(best.channel_id or 0),
                    score=0.45 + min(0.25, len(channel_set) * 0.05),
                    reason=(
                        f"'{concept.name}' is explained by {len(channel_set)} different channels; "
                        "this unwatched one offers another perspective."
                    ),
                    payload={"concept": concept.name, "channels": len(channel_set)},
                )
            )
        cross.sort(key=lambda item: -item["score"])
        events.extend(cross[:per_kind])

    if config["include_overlooked"] and videos:
        views = sorted(v.view_count or 0 for v in videos)
        median = views[len(views) // 2] if views else 0
        older = [
            v for v in videos
            if (v.published_at or now) < window_start
            and v.id not in watched
            and (v.view_count or 0) >= median
        ]
        older.sort(key=lambda v: (v.view_count or 0), reverse=True)
        for video in older[:per_kind]:
            events.append(
                _event(
                    kind="overlooked",
                    video=video,
                    channel_id=channels.get(video.channel_id or 0),
                    score=0.35 + min(0.2, (video.view_count or 0) / max(1, views[-1] or 1) * 0.2),
                    reason="Popular in your subscriptions and still unwatched, but older than the discovery window.",
                    payload={"view_count": video.view_count, "published_at": video.published_at},
                )
            )

    if config["include_objectives"]:
        paths = session.scalars(select(LearningPath).where(LearningPath.status == "active")).all()
        for path in paths:
            concept_ids = {
                row[0]
                for row in session.execute(
                    select(LearningPathStage.concept_id).where(
                        LearningPathStage.path_id == path.id,
                        LearningPathStage.concept_id.is_not(None),
                    )
                ).all()
            }
            item_video_ids = {
                row[0]
                for row in session.execute(
                    select(LearningPathItem.video_id)
                    .join(LearningPathStage, LearningPathStage.id == LearningPathItem.stage_id)
                    .where(LearningPathStage.path_id == path.id)
                ).all()
            }
            if not concept_ids:
                continue
            matches = [
                v for v in videos
                if v.id not in watched
                and v.id not in item_video_ids
                and any(
                    link.concept_id in concept_ids
                    for link in session.scalars(
                        select(VideoConcept).where(VideoConcept.video_id == v.id)
                    ).all()
                )
            ]
            matches.sort(key=lambda v: (v.published_at or 0), reverse=True)
            for video in matches[: max(1, per_kind // 2)]:
                events.append(
                    _event(
                        kind="objective_match",
                        video=video,
                        channel_id=channels.get(video.channel_id or 0),
                        score=0.65,
                        reason=f"Matches the active learning objective '{path.objective}'.",
                        payload={"path_id": path.id, "objective": path.objective},
                    )
                )

    if config["include_connections"]:
        recent = [v for v in videos if (v.published_at or 0) >= window_start]
        pairs: Counter[tuple[int, int]] = Counter()
        for video in recent:
            cids = sorted(
                {
                    link.concept_id
                    for link in session.scalars(
                        select(VideoConcept).where(VideoConcept.video_id == video.id)
                    ).all()
                }
            )
            for i, a in enumerate(cids):
                for b in cids[i + 1 :]:
                    pairs[(a, b)] += 1
        for (a, b), count in pairs.most_common(per_kind):
            if count < 2:
                continue
            ca = session.get(Concept, a)
            cb = session.get(Concept, b)
            if not ca or not cb:
                continue
            events.append(
                _event(
                    kind="new_connection",
                    video=None,
                    channel_id=None,
                    score=0.4 + min(0.2, count * 0.05),
                    reason=f"'{ca.name}' and '{cb.name}' appear together in {count} recent library videos.",
                    payload={"concepts": [ca.name, cb.name], "co_occurrences": count},
                )
            )

    if config["include_path_updates"]:
        for update in path_updates(session, limit=per_kind):
            video = session.scalar(select(Video).where(Video.yt_id == update["video_id"]))
            events.append(
                _event(
                    kind="path_update",
                    video=video,
                    channel_id=channels.get(video.channel_id or 0) if video else None,
                    score=0.55,
                    reason=update["reason"],
                    payload={"path_id": update["path_id"], "concept": update["concept"]},
                )
            )

    # One event per video keeps a report readable; concept-level events may remain.
    seen_videos: set[str] = set()
    deduped = []
    for event in sorted(events, key=lambda item: -item["score"]):
        video_id = event.get("video_id")
        if video_id:
            if video_id in seen_videos:
                continue
            seen_videos.add(video_id)
        deduped.append(event)
        if len(deduped) >= int(config["max_events"]):
            break

    report = DiscoveryReport(
        id="radar_" + secrets.token_hex(10),
        period=period,
        generated_at=now,
        window_start=window_start,
        window_end=now,
        summary_json=json.dumps(
            {
                "counts": dict(Counter(event["kind"] for event in deduped)),
                "window_days": window_days,
                "config": config,
            },
            sort_keys=True,
        ),
    )
    session.add(report)
    session.flush()
    stored = []
    for event in deduped:
        video_id = None
        if event.get("video_id"):
            video = session.scalar(select(Video).where(Video.yt_id == event["video_id"]))
            video_id = video.id if video else None
        channel_id = video.channel_id if video_id is not None and (video := session.get(Video, video_id)) else None
        row = DiscoveryEvent(
            id="dev_" + secrets.token_hex(8),
            report_id=report.id,
            kind=event["kind"],
            video_id=video_id,
            channel_id=channel_id,
            score=event["score"],
            reason=event["reason"],
            payload_json=json.dumps(event.get("payload") or {}, sort_keys=True),
            status="new",
            created_at=now,
        )
        session.add(row)
        stored.append(event)
    session.flush()
    return {
        "id": report.id,
        "period": period,
        "generated_at": now,
        "window": {"start": window_start, "end": now, "days": window_days},
        "events": stored,
        "counts": dict(Counter(event["kind"] for event in stored)),
        "note": (
            "Only videos from your imported subscriptions are considered. "
            "Every item explains why it was surfaced."
        ),
    }


def _report_out(session: Session, report: DiscoveryReport, *, include_dismissed: bool = False) -> dict[str, Any]:
    q = select(DiscoveryEvent).where(DiscoveryEvent.report_id == report.id)
    if not include_dismissed:
        q = q.where(DiscoveryEvent.status != "dismissed")
    rows = session.scalars(q.order_by(DiscoveryEvent.score.desc())).all()
    events = []
    for row in rows:
        video = session.get(Video, row.video_id) if row.video_id else None
        channel = session.get(Channel, video.channel_id) if video and video.channel_id else None
        try:
            payload = json.loads(row.payload_json or "{}")
        except json.JSONDecodeError:
            payload = {}
        events.append(
            {
                "id": row.id,
                "kind": row.kind,
                "video_id": video.yt_id if video else None,
                "title": video.title if video else None,
                "channel": channel.name if channel else None,
                "score": round(float(row.score or 0), 4),
                "reason": row.reason,
                "payload": payload,
                "status": row.status,
            }
        )
    try:
        summary = json.loads(report.summary_json or "{}")
    except json.JSONDecodeError:
        summary = {}
    return {
        "id": report.id,
        "period": report.period,
        "generated_at": report.generated_at,
        "window": {"start": report.window_start, "end": report.window_end},
        "summary": summary,
        "events": events,
    }


def latest_report(
    session: Session, *, period: str | None = None, include_dismissed: bool = False
) -> dict[str, Any] | None:
    q = select(DiscoveryReport)
    if period:
        q = q.where(DiscoveryReport.period == period)
    report = session.scalar(q.order_by(DiscoveryReport.generated_at.desc()).limit(1))
    if report is None:
        return None
    return _report_out(session, report, include_dismissed=include_dismissed)


def list_reports(session: Session, *, limit: int = 20) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(DiscoveryReport).order_by(DiscoveryReport.generated_at.desc()).limit(limit)
    ).all()
    return [
        {
            "id": row.id,
            "period": row.period,
            "generated_at": row.generated_at,
            "window": {"start": row.window_start, "end": row.window_end},
        }
        for row in rows
    ]


def set_event_status(session: Session, event_id: str, status: str) -> dict[str, Any]:
    if status not in {"new", "dismissed", "saved"}:
        raise ValueError("status must be new, dismissed or saved")
    row = session.get(DiscoveryEvent, event_id)
    if row is None:
        raise ValueError("event not found")
    row.status = status
    session.flush()
    return {"id": row.id, "status": row.status}
