"""Pydantic shapes for the public API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ------------------------------------------------------------------ request envelopes


class PaginatedRequest(BaseModel):
    limit: int = Field(50, ge=1, le=500)
    cursor: str | None = None


class VideoFilterRequest(BaseModel):
    shorts: bool = False
    duration_min_s: int | None = Field(None, ge=0)
    duration_max_s: int | None = Field(None, ge=0)
    category_slug: str | None = None
    sort: str = "recent"


# ------------------------------------------------------------------ common objects


class ChannelSummary(BaseModel):
    id: str
    name: str
    handle: str | None = None
    avatar_url: str | None = None
    subscriber_count: int | None = None
    video_count: int = 0
    category_slug: str | None = None


class VideoSummary(BaseModel):
    id: str
    title: str
    duration_s: int | None = None
    view_count: int | None = None
    published_at: datetime | None = None
    thumbnail_url: str | None = None
    channel: ChannelSummary | None = None


class SearchHitOut(BaseModel):
    id: str
    title: str
    duration_s: int | None = None
    view_count: int | None = None
    published_at: datetime | None = None
    snippet: str = ""
    rank: float = 0.0
    channel: ChannelSummary | None = None


class SearchResultOut(BaseModel):
    query: str
    hits: list[SearchHitOut] = Field(default_factory=list)
    next_cursor: str | None = None
    count: int = 0


class StatBlock(BaseModel):
    channels: int = 0
    videos: int = 0
    hours: float = 0.0
    categories: int = 0
    videos_unavailable: int = 0


class ErrorBody(BaseModel):
    detail: str
    code: str | None = None
    request_id: str | None = None


# ------------------------------------------------------------------ recommendation payloads


class KnobUpdate(BaseModel):
    lexical: float | None = Field(None, ge=0, le=5)
    semantic: float | None = Field(None, ge=0, le=5)
    channel: float | None = Field(None, ge=0, le=5)
    neighbours: float | None = Field(None, ge=0, le=5)
    session: float | None = Field(None, ge=0, le=5)
    popular: float | None = Field(None, ge=0, le=5)
    rating: float | None = Field(None, ge=0, le=5)
    duration: float | None = Field(None, ge=0, le=5)
    new_channel: float | None = Field(None, ge=0, le=5)
    negative: float | None = Field(None, ge=-5, le=0)

    model_config = {"validate_by_name": True}


class RecommendationRequest(BaseModel):
    user_id: str = Field(default="default")
    count: int = Field(25, ge=1, le=200)
    filters: VideoFilterRequest | None = None
    knobs: KnobUpdate | None = None
    diversity: float = Field(30.0, ge=0, le=100)
    channel_cap: int | None = None

    model_config = {"validate_by_name": True}


class RecommendationItem(BaseModel):
    id: str
    title: str
    duration_s: int | None = None
    view_count: int | None = None
    thumbnail_url: str | None = None
    channel: ChannelSummary | None = None
    score: float = 0.0
    reason: str = ""


class RecommendationOut(BaseModel):
    user_id: str
    items: list[RecommendationItem] = Field(default_factory=list)
    model: str = "baseline"


# ------------------------------------------------------------------ job results


class JobStatus(BaseModel):
    id: str
    type: str
    title: str | None = None
    status: str
    progress: float = 0.0
    stage: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None


class JobResult(BaseModel):
    id: str
    status: str
    result: dict[str, Any] | None = None
    error: str | None = None


# ------------------------------------------------------------------ user state


class LocalStorageState(BaseModel):
    hidden_channels: list[str] = Field(default_factory=list, alias="hidden-ch")
    hidden_videos: list[str] = Field(default_factory=list, alias="hidden-vid")
    hidden_categories: list[str] = Field(default_factory=list, alias="hidden-cat")
    category_overrides: dict[str, str] = Field(default_factory=dict, alias="cat-ov")
    ratings: dict[str, int] = Field(default_factory=dict)
    courses: list[dict[str, Any]] = Field(default_factory=list)
    history: list[dict[str, Any]] = Field(default_factory=list)
    seen: dict[str, Any] = Field(default_factory=dict)
    negative: list[str] = Field(default_factory=list)
    reco: dict[str, Any] = Field(default_factory=dict)
    filters: dict[str, Any] = Field(default_factory=dict)
    ranker: dict[str, Any] = Field(default_factory=dict)
    bandit: dict[str, Any] = Field(default_factory=dict)

    model_config = {"validate_by_name": True}


class UserSettingsOut(BaseModel):
    data_dir: str
    host: str
    port: int
    requires_auth: bool
    docs_url: str = "/docs"
    openapi_url: str = "/openapi.json"
