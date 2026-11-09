"""SQLAlchemy 2.0 schema.

Timestamps are INTEGER epoch milliseconds (UTC) so they serialise to the frontend
without timezone ambiguity. Booleans are 0/1 INTEGER.
"""

from __future__ import annotations

import time

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now_ms() -> int:
    return int(time.time() * 1000)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------- structure


class Library(Base):
    __tablename__ = "libraries"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    source_path: Mapped[str | None] = mapped_column(String(500))
    per_channel: Mapped[int] = mapped_column(Integer, default=50, server_default=text("50"))
    created_at: Mapped[int] = mapped_column(Integer, default=now_ms)
    updated_at: Mapped[int] = mapped_column(Integer, default=now_ms)
    is_active: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("library_id", "slug", name="uq_categories_library_slug"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    library_id: Mapped[int] = mapped_column(ForeignKey("libraries.id", ondelete="CASCADE"))
    slug: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(120))
    position: Mapped[int] = mapped_column(Integer, default=100, server_default=text("100"))


class Channel(Base):
    __tablename__ = "channels"
    __table_args__ = (
        Index("ix_channels_library_name", "library_id", "name"),
        Index("ix_channels_category", "category_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    library_id: Mapped[int] = mapped_column(ForeignKey("libraries.id", ondelete="CASCADE"))
    yt_id: Mapped[str] = mapped_column(String(24), unique=True)
    name: Mapped[str] = mapped_column(String(300))
    url: Mapped[str | None] = mapped_column(String(400))
    handle: Mapped[str | None] = mapped_column(String(120))
    avatar_url: Mapped[str | None] = mapped_column(String(600))
    description: Mapped[str | None] = mapped_column(Text)
    subscriber_count: Mapped[int | None] = mapped_column(Integer)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    first_seen_at: Mapped[int] = mapped_column(Integer, default=now_ms)
    last_seen_at: Mapped[int] = mapped_column(Integer, default=now_ms)


class ChannelCategoryOverride(Base):
    __tablename__ = "channel_category_overrides"

    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), primary_key=True
    )
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"))
    updated_at: Mapped[int] = mapped_column(Integer, default=now_ms)


class Video(Base):
    __tablename__ = "videos"
    __table_args__ = (
        Index("ix_videos_channel_recent", "channel_id", "published_at", "id"),
        Index("ix_videos_recent", "published_at", "id"),
        Index("ix_videos_duration", "duration_s"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"))
    yt_id: Mapped[str] = mapped_column(String(16), unique=True)
    title: Mapped[str] = mapped_column(String(500))
    duration_s: Mapped[int | None] = mapped_column(Integer)
    view_count: Mapped[int | None] = mapped_column(Integer)
    published_at: Mapped[int | None] = mapped_column(Integer)
    thumbnail_url: Mapped[str | None] = mapped_column(String(600))
    description: Mapped[str | None] = mapped_column(Text)
    is_available: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    metadata_hash: Mapped[str] = mapped_column(String(32), default="")
    first_seen_at: Mapped[int] = mapped_column(Integer, default=now_ms)
    last_seen_at: Mapped[int] = mapped_column(Integer, default=now_ms)
    extra_json: Mapped[str | None] = mapped_column(Text)


class VideoMetadataVersion(Base):
    __tablename__ = "video_metadata_versions"
    __table_args__ = (Index("ix_vmv_video", "video_id", "captured_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"))
    captured_at: Mapped[int] = mapped_column(Integer, default=now_ms)
    metadata_hash: Mapped[str] = mapped_column(String(32))
    title: Mapped[str | None] = mapped_column(String(500))
    duration_s: Mapped[int | None] = mapped_column(Integer)
    view_count: Mapped[int | None] = mapped_column(Integer)
    changed_fields: Mapped[str | None] = mapped_column(String(200))


# ---------------------------------------------------------------- ingestion


class ChannelSyncState(Base):
    __tablename__ = "channel_sync_state"
    __table_args__ = (Index("ix_sync_due", "status", "next_attempt_at"),)

    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(16), default="never", server_default="never")
    last_attempt_at: Mapped[int | None] = mapped_column(Integer)
    last_success_at: Mapped[int | None] = mapped_column(Integer)
    last_error_stage: Mapped[str | None] = mapped_column(String(40))
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    next_attempt_at: Mapped[int | None] = mapped_column(Integer)


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"
    __table_args__ = (Index("ix_runs_started", "started_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    library_id: Mapped[int] = mapped_column(ForeignKey("libraries.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(16), default="running")
    started_at: Mapped[int] = mapped_column(Integer, default=now_ms)
    finished_at: Mapped[int | None] = mapped_column(Integer)
    channels_ok: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    channels_failed: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    videos_seen: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    videos_new: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    videos_updated: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    job_id: Mapped[str | None] = mapped_column(String(32))
    notes: Mapped[str | None] = mapped_column(Text)


class IngestionError(Base):
    __tablename__ = "ingestion_errors"
    __table_args__ = (Index("ix_errors_run", "run_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("ingestion_runs.id", ondelete="CASCADE"))
    channel_yt_id: Mapped[str | None] = mapped_column(String(24))
    stage: Mapped[str] = mapped_column(String(40))
    message: Mapped[str] = mapped_column(Text)
    retryable: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[int] = mapped_column(Integer, default=now_ms)


# ---------------------------------------------------------------- jobs


class BackgroundJob(Base):
    __tablename__ = "background_jobs"
    __table_args__ = (
        Index("ix_jobs_claim", "status", "priority", "created_at"),
        Index("ix_jobs_stale", "status", "heartbeat_at"),
        Index(
            "uq_jobs_active_dedupe",
            "type",
            "dedupe_key",
            unique=True,
            sqlite_where=text("status IN ('queued','running') AND dedupe_key IS NOT NULL"),
        ),
        CheckConstraint("progress >= 0 AND progress <= 1", name="ck_jobs_progress"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    type: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(12), default="queued")
    priority: Mapped[int] = mapped_column(Integer, default=5, server_default=text("5"))
    created_at: Mapped[int] = mapped_column(Integer, default=now_ms)
    started_at: Mapped[int | None] = mapped_column(Integer)
    finished_at: Mapped[int | None] = mapped_column(Integer)
    progress: Mapped[float] = mapped_column(default=0.0, server_default=text("0"))
    stage: Mapped[str | None] = mapped_column(String(60))
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, server_default=text("3"))
    cancel_requested: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    worker_id: Mapped[str | None] = mapped_column(String(64))
    heartbeat_at: Mapped[int | None] = mapped_column(Integer)
    dedupe_key: Mapped[str | None] = mapped_column(String(120))


# ---------------------------------------------------------------- user state


class WatchHistory(Base):
    """Aggregated per-video My Tube watch state (backward-compatible base + Phase 2 fields)."""
    __tablename__ = "watch_history"
    __table_args__ = (
        Index("ix_history_watched", "watched_at"),
        Index("ix_history_last_watched", "last_watched_at"),
    )

    video_id: Mapped[int] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True
    )
    # Legacy fields kept for compatibility with existing user_state import/export.
    watched_at: Mapped[int] = mapped_column(Integer, default=now_ms)
    engagement: Mapped[float] = mapped_column(default=1.0, server_default=text("1.0"))
    position_s: Mapped[float | None] = mapped_column()
    completed: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    # Phase 2 dual-history foundation (nullable for additive migration).
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("channels.id", ondelete="SET NULL"))
    first_watched_at: Mapped[int | None] = mapped_column(Integer)
    last_watched_at: Mapped[int | None] = mapped_column(Integer)
    total_watch_s: Mapped[float | None] = mapped_column()
    watch_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    # 'embedded' or 'external_youtube' (My Tube playback source). Never means confirmed YouTube history.
    playback_source: Mapped[str | None] = mapped_column(String(24))
    last_session_id: Mapped[str | None] = mapped_column(String(40))
    # Timestamp when user initiated external YouTube playback from My Tube (NOT confirmed YouTube watch).
    external_initiated_at: Mapped[int | None] = mapped_column(Integer)


class WatchSession(Base):
    """Individual My Tube playback session. YouTube account history is NOT tracked here."""
    __tablename__ = "watch_sessions"
    __table_args__ = (
        Index("ix_watch_sessions_video", "video_id", "started_at"),
        Index("ix_watch_sessions_started", "started_at"),
        Index("ix_watch_sessions_status", "status", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"))
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("channels.id", ondelete="SET NULL"))
    started_at: Mapped[int] = mapped_column(Integer, default=now_ms)
    updated_at: Mapped[int] = mapped_column(Integer, default=now_ms)
    ended_at: Mapped[int | None] = mapped_column(Integer)
    position_s: Mapped[float] = mapped_column(default=0.0, server_default=text("0"))
    duration_s: Mapped[float | None] = mapped_column()
    watched_s: Mapped[float] = mapped_column(default=0.0, server_default=text("0"))
    completed: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    # 'embedded' or 'external_youtube'
    playback_source: Mapped[str] = mapped_column(String(24), default="embedded")
    status: Mapped[str] = mapped_column(String(16), default="active")  # active/completed/abandoned
    # For external_youtube: records initiation only. Never set as confirmed YouTube account history.
    external_initiated_at: Mapped[int | None] = mapped_column(Integer)
    meta_json: Mapped[str | None] = mapped_column(Text)


class RecommendationEvent(Base):
    __tablename__ = "recommendation_events"
    __table_args__ = (
        Index("ix_events_video", "video_id", "at"),
        Index("ix_events_kind_at", "kind", "at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    video_id: Mapped[int | None] = mapped_column(ForeignKey("videos.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(20))
    surface: Mapped[str | None] = mapped_column(String(30))
    position: Mapped[int | None] = mapped_column(Integer)
    at: Mapped[int] = mapped_column(Integer, default=now_ms)
    meta_json: Mapped[str | None] = mapped_column(Text)


class ChannelRating(Base):
    __tablename__ = "channel_ratings"
    __table_args__ = (CheckConstraint("rating BETWEEN 1 AND 5", name="ck_rating_range"),)

    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), primary_key=True
    )
    rating: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[int] = mapped_column(Integer, default=now_ms)


class HiddenChannel(Base):
    __tablename__ = "hidden_channels"

    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), primary_key=True
    )
    hidden_at: Mapped[int] = mapped_column(Integer, default=now_ms)


class HiddenVideo(Base):
    __tablename__ = "hidden_videos"

    video_id: Mapped[int] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True
    )
    hidden_at: Mapped[int] = mapped_column(Integer, default=now_ms)


class HiddenCategory(Base):
    __tablename__ = "hidden_categories"

    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id", ondelete="CASCADE"), primary_key=True
    )
    hidden_at: Mapped[int] = mapped_column(Integer, default=now_ms)


class UserPreference(Base):
    __tablename__ = "user_preferences"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value_json: Mapped[str] = mapped_column(Text, default="null")
    updated_at: Mapped[int] = mapped_column(Integer, default=now_ms)


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[int] = mapped_column(Integer, default=now_ms)
    updated_at: Mapped[int] = mapped_column(Integer, default=now_ms)
    archived: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))


class CourseVideo(Base):
    __tablename__ = "course_videos"

    course_id: Mapped[str] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), primary_key=True
    )
    video_id: Mapped[int] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0)
    added_at: Mapped[int] = mapped_column(Integer, default=now_ms)


class Collection(Base):
    __tablename__ = "collections"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(24), default="collection")
    created_at: Mapped[int] = mapped_column(Integer, default=now_ms)


class CollectionVideo(Base):
    __tablename__ = "collection_videos"

    collection_id: Mapped[str] = mapped_column(
        ForeignKey("collections.id", ondelete="CASCADE"), primary_key=True
    )
    video_id: Mapped[int] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0)
    added_at: Mapped[int] = mapped_column(Integer, default=now_ms)


class Note(Base):
    __tablename__ = "notes"
    __table_args__ = (Index("ix_notes_video", "video_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    video_id: Mapped[int | None] = mapped_column(ForeignKey("videos.id", ondelete="SET NULL"))
    course_id: Mapped[str | None] = mapped_column(ForeignKey("courses.id", ondelete="SET NULL"))
    body: Mapped[str] = mapped_column(Text)
    timestamp_s: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[int] = mapped_column(Integer, default=now_ms)
    updated_at: Mapped[int] = mapped_column(Integer, default=now_ms)


class Bookmark(Base):
    __tablename__ = "bookmarks"

    video_id: Mapped[int] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[int] = mapped_column(Integer, default=now_ms)


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)


class VideoTag(Base):
    __tablename__ = "video_tags"

    video_id: Mapped[int] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True)


# ---------------------------------------------------------------- enrichment


class Transcript(Base):
    __tablename__ = "transcripts"

    id: Mapped[int] = mapped_column(primary_key=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), unique=True)
    language: Mapped[str | None] = mapped_column(String(16))
    source: Mapped[str | None] = mapped_column(String(24))
    text: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[int] = mapped_column(Integer, default=now_ms)


class TranscriptSegment(Base):
    __tablename__ = "transcript_segments"
    __table_args__ = (Index("ix_segments_transcript", "transcript_id", "start_s"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    transcript_id: Mapped[int] = mapped_column(ForeignKey("transcripts.id", ondelete="CASCADE"))
    start_s: Mapped[float] = mapped_column()
    end_s: Mapped[float] = mapped_column()
    text: Mapped[str] = mapped_column(Text)


class VideoChapter(Base):
    __tablename__ = "video_chapters"
    __table_args__ = (Index("ix_chapters_video", "video_id", "start_s"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"))
    start_s: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(300))


# ---------------------------------------------------------------- recommenders


class TopicVector(Base):
    __tablename__ = "topic_vectors"

    video_id: Mapped[int] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True
    )
    model_id: Mapped[str] = mapped_column(String(40))
    dim: Mapped[int] = mapped_column(Integer)
    vector: Mapped[bytes] = mapped_column(LargeBinary)
    norm: Mapped[float] = mapped_column(default=1.0)


class ChannelVector(Base):
    __tablename__ = "channel_vectors"

    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), primary_key=True
    )
    model_id: Mapped[str] = mapped_column(String(40))
    dim: Mapped[int] = mapped_column(Integer)
    vector: Mapped[bytes] = mapped_column(LargeBinary)
    norm: Mapped[float] = mapped_column(default=1.0)


class RecommendationModel(Base):
    __tablename__ = "recommendation_models"
    __table_args__ = (UniqueConstraint("kind", "version", name="uq_models_kind_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(24))
    version: Mapped[int] = mapped_column(Integer)
    weights_json: Mapped[str] = mapped_column(Text)
    config_json: Mapped[str | None] = mapped_column(Text)
    metrics_json: Mapped[str | None] = mapped_column(Text)
    trained_at: Mapped[int] = mapped_column(Integer, default=now_ms)
    is_active: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))


class RecommendationExperiment(Base):
    __tablename__ = "recommendation_experiments"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    config_json: Mapped[str] = mapped_column(Text)
    results_json: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="running")
    started_at: Mapped[int] = mapped_column(Integer, default=now_ms)
    finished_at: Mapped[int | None] = mapped_column(Integer)


class ShelfStatistic(Base):
    __tablename__ = "shelf_statistics"
    __table_args__ = (UniqueConstraint("shelf", "day", name="uq_shelf_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shelf: Mapped[str] = mapped_column(String(40))
    day: Mapped[str] = mapped_column(String(10))
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    clicks: Mapped[int] = mapped_column(Integer, default=0)


# ---------------------------------------------------------------- operations


class Backup(Base):
    __tablename__ = "backups"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    path: Mapped[str] = mapped_column(String(600))
    bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[int] = mapped_column(Integer, default=now_ms)
    verified_at: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="ok")


class SchemaVersion(Base):
    __tablename__ = "schema_version"

    version: Mapped[int] = mapped_column(primary_key=True)
    applied_at: Mapped[int] = mapped_column(Integer, default=now_ms)
