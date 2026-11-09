"""Features 9 and 10: analytics timeline and discovery radar."""

from __future__ import annotations

from datetime import datetime, timezone

from app.db.models import Video, WatchHistory, WatchSession, now_ms
from app.services.analytics.timeline import build_timeline
from app.services.discovery.radar import get_config, latest_report, scan, set_event_status, update_config
from app.services.knowledge.extraction import extract_knowledge
from app.services.knowledge.transcripts import ingest_transcript

TEXT = (
    "A convolutional neural network is a model that learns filters from images. "
    "The convolution layer slides a kernel and detects edges. "
    "Before you learn this, you should understand linear algebra. "
    "Training uses backpropagation and gradient descent."
)


def _prepare(db):
    session = db.session_factory()
    ingest_transcript(session, "va1", text=TEXT, source="x")
    extract_knowledge(session, "va1")
    ingest_transcript(session, "va2", text=TEXT.replace("convolutional", "convolution"), source="x")
    extract_knowledge(session, "va2")
    session.commit()
    return session


def test_timeline_counts_real_events_and_separates_metrics(seeded_db):
    session = _prepare(seeded_db)
    now = now_ms()
    month_ms = 30 * 86_400_000
    va1 = session.query(Video).filter(Video.yt_id == "va1").one()
    va2 = session.query(Video).filter(Video.yt_id == "va2").one()
    session.add_all(
        [
            WatchSession(
                id="s1",
                video_id=va1.id,
                channel_id=va1.channel_id,
                started_at=now - 5 * month_ms,
                updated_at=now - 5 * month_ms,
                watched_s=600,
                completed=1,
                position_s=600,
                status="completed",
            ),
            WatchSession(
                id="s2",
                video_id=va2.id,
                channel_id=va2.channel_id,
                started_at=now - 2 * month_ms,
                updated_at=now - 2 * month_ms,
                watched_s=300,
                completed=0,
                position_s=300,
                status="active",
            ),
            WatchHistory(
                video_id=va1.id,
                channel_id=va1.channel_id,
                last_watched_at=now - 5 * month_ms,
                total_watch_s=600,
                completed=1,
                watch_count=1,
            ),
        ]
    )
    session.commit()

    timeline = build_timeline(session, period="month", buckets=6, now=now)
    assert len(timeline["buckets"]) == 6
    summary = timeline["summary"]
    assert summary["learning_sessions"] == 2
    assert summary["videos_completed"] >= 1
    assert summary["top_subjects"]
    assert timeline["metric_definitions"]["exposure"]
    # No reviews yet: retention is null, not a fabricated number.
    assert summary["revision"]["retention_proxy"] is None
    # The timeline must be labelled as measured/inferred, not mastery.
    assert "not" in timeline["note"].lower()


def test_radar_scans_only_library_and_respects_config(seeded_db):
    session = _prepare(seeded_db)
    now = now_ms()
    va1 = session.query(Video).filter(Video.yt_id == "va1").one()
    va2 = session.query(Video).filter(Video.yt_id == "va2").one()
    va1.published_at = now - 2 * 86_400_000
    va2.published_at = now - 1 * 86_400_000
    session.commit()

    report = scan(session, period="daily", now=now)
    assert report["id"]
    assert report["events"]
    library_ids = {v.yt_id for v in session.query(Video).all()}
    for event in report["events"]:
        assert event["reason"]
        if event["video_id"]:
            assert event["video_id"] in library_ids
    assert report["note"].startswith("Only videos from your imported subscriptions")

    latest = latest_report(session, period="daily")
    assert latest and latest["id"] == report["id"]
    event_id = latest["events"][0]["id"]
    set_event_status(session, event_id, "dismissed")
    latest = latest_report(session, period="daily")
    assert all(event["id"] != event_id for event in latest["events"])

    update_config(session, {"enabled": False})
    disabled = scan(session, now=now)
    assert disabled["enabled"] is False and disabled["events"] == []
    update_config(session, {"enabled": True, "paused": True})
    paused = scan(session, now=now)
    assert paused["paused"] is True
    update_config(session, {"paused": False, "schedule": "weekly", "window_days": 7})
    assert get_config(session)["schedule"] == "weekly"
    assert get_config(session)["window_days"] == 7
