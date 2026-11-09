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
    seq: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    content_hash: Mapped[str] = mapped_column(String(32), default="")
    normalized_hash: Mapped[str] = mapped_column(String(32), default="")
    word_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    segment_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    source_kind: Mapped[str | None] = mapped_column(String(24))
    # supplied|creator|none - "none" means timestamps are unknown and must be reported as null.
    timing_source: Mapped[str] = mapped_column(String(16), default="none", server_default="'none'")
    provenance_json: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[int] = mapped_column(Integer, default=now_ms)


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
    # creator|generated|user - generated rows are always labelled in the API.
    source: Mapped[str] = mapped_column(String(16), default="creator", server_default="'creator'")
    confidence: Mapped[float] = mapped_column(default=0.0, server_default="0")
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    generated_at: Mapped[int | None] = mapped_column(Integer)
    evidence_json: Mapped[str | None] = mapped_column(Text)


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


# ---------------------------------------------------------------- intelligence (My Tube 3.0)


class Concept(Base):
    """A topic or concept extracted from the user's own library.

    `origin` records where the row came from; nothing is invented without a stored
    `ProcessingProvenance` / evidence link.
    """

    __tablename__ = "concepts"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_concepts_slug"),
        Index("ix_concepts_kind", "kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(16), default="concept")  # topic|concept
    description: Mapped[str | None] = mapped_column(Text)
    aliases_json: Mapped[str | None] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(String(24), default="extracted")
    created_at: Mapped[int] = mapped_column(Integer, default=now_ms)
    updated_at: Mapped[int] = mapped_column(Integer, default=now_ms)


class ConceptRelation(Base):
    __tablename__ = "concept_relations"
    __table_args__ = (
        Index("ix_relations_src", "src_concept_id"),
        Index("ix_relations_dst", "dst_concept_id"),
    )

    src_concept_id: Mapped[int] = mapped_column(
        ForeignKey("concepts.id", ondelete="CASCADE"), primary_key=True
    )
    dst_concept_id: Mapped[int] = mapped_column(
        ForeignKey("concepts.id", ondelete="CASCADE"), primary_key=True
    )
    relation: Mapped[str] = mapped_column(String(24), primary_key=True)  # prerequisite|related
    confidence: Mapped[float] = mapped_column(default=0.5, server_default=text("0.5"))
    provenance_json: Mapped[str | None] = mapped_column(Text)
    evidence_video_id: Mapped[int | None] = mapped_column(
        ForeignKey("videos.id", ondelete="SET NULL")
    )
    created_at: Mapped[int] = mapped_column(Integer, default=now_ms)


class VideoConcept(Base):
    __tablename__ = "video_concepts"
    __table_args__ = (
        Index("ix_video_concepts_concept", "concept_id", "weight"),
    )

    video_id: Mapped[int] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True
    )
    concept_id: Mapped[int] = mapped_column(
        ForeignKey("concepts.id", ondelete="CASCADE"), primary_key=True
    )
    weight: Mapped[float] = mapped_column(default=1.0, server_default=text("1.0"))
    provenance: Mapped[str] = mapped_column(String(24), default="extracted")
    confidence: Mapped[float] = mapped_column(default=0.5, server_default=text("0.5"))
    created_at: Mapped[int] = mapped_column(Integer, default=now_ms)


class KnowledgeExtract(Base):
    __tablename__ = "knowledge_extracts"
    __table_args__ = (
        Index("ix_extracts_video_kind", "video_id", "kind"),
        Index("ix_extracts_hash", "content_hash"),
        UniqueConstraint("video_id", "kind", "content_hash", name="uq_extracts_video_kind_hash"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(24))
    content: Mapped[str] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)
    start_s: Mapped[float | None] = mapped_column()
    end_s: Mapped[float | None] = mapped_column()
    confidence: Mapped[float] = mapped_column(default=0.5, server_default=text("0.5"))
    source: Mapped[str] = mapped_column(String(16), default="deterministic")
    model_id: Mapped[str] = mapped_column(String(60), default="deterministic-v1")
    content_hash: Mapped[str] = mapped_column(String(32))
    edited: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    rejected: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    provenance_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[int] = mapped_column(Integer, default=now_ms)
    updated_at: Mapped[int] = mapped_column(Integer, default=now_ms)


class EmbeddingVector(Base):
    __tablename__ = "embedding_vectors"
    __table_args__ = (
        UniqueConstraint("owner_type", "owner_id", "model_id", name="uq_embedding_owner_model"),
        Index("ix_embeddings_model", "model_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_type: Mapped[str] = mapped_column(String(16))  # video|segment|concept|extract|channel
    owner_id: Mapped[int] = mapped_column(Integer)
    model_id: Mapped[str] = mapped_column(String(60))
    dim: Mapped[int] = mapped_column(Integer)
    vector: Mapped[bytes] = mapped_column(LargeBinary)
    norm: Mapped[float] = mapped_column(default=1.0)
    content_hash: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[int] = mapped_column(Integer, default=now_ms)


class FlashcardDeck(Base):
    __tablename__ = "flashcard_decks"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    archived: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    created_at: Mapped[int] = mapped_column(Integer, default=now_ms)
    updated_at: Mapped[int] = mapped_column(Integer, default=now_ms)


class Flashcard(Base):
    __tablename__ = "flashcards"
    __table_args__ = (
        Index("ix_flashcards_due", "status", "due_at"),
        Index("ix_flashcards_video", "video_id"),
        UniqueConstraint("deck_id", "content_hash", name="uq_flashcards_deck_hash"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    deck_id: Mapped[str] = mapped_column(ForeignKey("flashcard_decks.id", ondelete="CASCADE"))
    video_id: Mapped[int | None] = mapped_column(ForeignKey("videos.id", ondelete="SET NULL"))
    extract_id: Mapped[int | None] = mapped_column(
        ForeignKey("knowledge_extracts.id", ondelete="SET NULL")
    )
    card_type: Mapped[str] = mapped_column(String(24), default="question_answer")
    front: Mapped[str] = mapped_column(Text)
    back: Mapped[str] = mapped_column(Text)
    source_start_s: Mapped[float | None] = mapped_column()
    source_excerpt: Mapped[str | None] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(String(12), default="auto")
    status: Mapped[str] = mapped_column(String(12), default="active")  # active|deleted
    state: Mapped[str] = mapped_column(String(12), default="new")
    stability: Mapped[float] = mapped_column(default=0.0, server_default=text("0"))
    difficulty: Mapped[float] = mapped_column(default=0.0, server_default=text("0"))
    due_at: Mapped[int] = mapped_column(Integer, default=now_ms)
    reps: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    lapses: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    last_review_at: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[int] = mapped_column(Integer, default=now_ms)
    updated_at: Mapped[int] = mapped_column(Integer, default=now_ms)


class ReviewLog(Base):
    __tablename__ = "review_logs"
    __table_args__ = (Index("ix_reviews_card", "card_id", "reviewed_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    card_id: Mapped[str] = mapped_column(ForeignKey("flashcards.id", ondelete="CASCADE"))
    rating: Mapped[int] = mapped_column(Integer)
    state_before: Mapped[str] = mapped_column(String(12))
    state_after: Mapped[str] = mapped_column(String(12))
    stability: Mapped[float] = mapped_column(default=0.0)
    difficulty: Mapped[float] = mapped_column(default=0.0)
    scheduled_days: Mapped[float] = mapped_column(default=0.0)
    elapsed_days: Mapped[float] = mapped_column(default=0.0)
    retrievability: Mapped[float] = mapped_column(default=0.0)
    due_at: Mapped[int] = mapped_column(Integer, default=now_ms)
    reviewed_at: Mapped[int] = mapped_column(Integer, default=now_ms)


class LearningPath(Base):
    __tablename__ = "learning_paths"
    __table_args__ = (Index("ix_paths_status", "status", "updated_at"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    objective: Mapped[str] = mapped_column(Text)
    level: Mapped[str] = mapped_column(String(16), default="auto")
    status: Mapped[str] = mapped_column(String(12), default="active")
    summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[int] = mapped_column(Integer, default=now_ms)
    updated_at: Mapped[int] = mapped_column(Integer, default=now_ms)


class LearningPathStage(Base):
    __tablename__ = "learning_path_stages"
    __table_args__ = (Index("ix_path_stages_path", "path_id", "position"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    path_id: Mapped[str] = mapped_column(
        ForeignKey("learning_paths.id", ondelete="CASCADE")
    )
    position: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(200))
    level: Mapped[str] = mapped_column(String(16), default="beginner")
    concept_id: Mapped[int | None] = mapped_column(
        ForeignKey("concepts.id", ondelete="SET NULL")
    )


class LearningPathItem(Base):
    __tablename__ = "learning_path_items"
    __table_args__ = (Index("ix_path_items_stage", "stage_id", "position"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    stage_id: Mapped[str] = mapped_column(
        ForeignKey("learning_path_stages.id", ondelete="CASCADE")
    )
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(12), default="todo")
    reason: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[int | None] = mapped_column(Integer)


class ResearchDocument(Base):
    __tablename__ = "research_documents"
    __table_args__ = (Index("ix_research_kind", "kind", "created_at"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    kind: Mapped[str] = mapped_column(String(24), default="note")
    title: Mapped[str] = mapped_column(String(300))
    body_json: Mapped[str] = mapped_column(Text)
    archived: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    created_at: Mapped[int] = mapped_column(Integer, default=now_ms)
    updated_at: Mapped[int] = mapped_column(Integer, default=now_ms)


class VideoComparison(Base):
    __tablename__ = "video_comparisons"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("research_documents.id", ondelete="CASCADE"), unique=True
    )
    video_ids_json: Mapped[str] = mapped_column(Text)
    objective: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[int] = mapped_column(Integer, default=now_ms)


class TimestampBookmark(Base):
    __tablename__ = "timestamp_bookmarks"
    __table_args__ = (Index("ix_ts_bookmarks_video", "video_id", "timestamp_s"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"))
    timestamp_s: Mapped[float] = mapped_column()
    label: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[int] = mapped_column(Integer, default=now_ms)


class DiscoveryReport(Base):
    __tablename__ = "discovery_reports"
    __table_args__ = (Index("ix_discovery_reports_at", "generated_at"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    period: Mapped[str] = mapped_column(String(12))  # daily|weekly|manual
    generated_at: Mapped[int] = mapped_column(Integer, default=now_ms)
    window_start: Mapped[int | None] = mapped_column(Integer)
    window_end: Mapped[int | None] = mapped_column(Integer)
    summary_json: Mapped[str] = mapped_column(Text, default="{}")


class DiscoveryEvent(Base):
    __tablename__ = "discovery_events"
    __table_args__ = (
        Index("ix_discovery_events_report", "report_id"),
        Index("ix_discovery_events_kind", "kind", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    report_id: Mapped[str] = mapped_column(
        ForeignKey("discovery_reports.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(32))
    video_id: Mapped[int | None] = mapped_column(
        ForeignKey("videos.id", ondelete="SET NULL")
    )
    channel_id: Mapped[int | None] = mapped_column(
        ForeignKey("channels.id", ondelete="SET NULL")
    )
    score: Mapped[float] = mapped_column(default=0.0)
    reason: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(12), default="new")
    created_at: Mapped[int] = mapped_column(Integer, default=now_ms)


class ModelConfig(Base):
    __tablename__ = "model_configs"
    __table_args__ = (
        UniqueConstraint("role", "provider", "model", name="uq_model_role_provider_model"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    role: Mapped[str] = mapped_column(String(24))  # llm|embedding|transcription
    provider: Mapped[str] = mapped_column(String(40))  # ollama|llamacpp|hash|sentence-transformers
    model: Mapped[str] = mapped_column(String(120))
    endpoint: Mapped[str | None] = mapped_column(String(300))
    enabled: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    config_json: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[int] = mapped_column(Integer, default=now_ms)


class ProcessingProvenance(Base):
    __tablename__ = "processing_provenance"
    __table_args__ = (Index("ix_provenance_entity", "entity_type", "entity_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(24))
    entity_id: Mapped[str] = mapped_column(String(64))
    step: Mapped[str] = mapped_column(String(32))
    model_id: Mapped[str | None] = mapped_column(String(60))
    content_hash: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="ok")
    detail_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[int] = mapped_column(Integer, default=now_ms)


class PipelineConfig(Base):
    __tablename__ = "pipeline_config"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value_json: Mapped[str] = mapped_column(Text, default="null")
    updated_at: Mapped[int] = mapped_column(Integer, default=now_ms)
