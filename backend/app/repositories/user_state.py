"""User state behind a repository, so the API never touches tables directly.

The shapes mirror the frontend's `mytube-*` keys exactly; `export_state()` produces the same
JSON the page writes, plus the four keys its own backup currently drops (reco, filters,
ranker, bandit).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Bookmark,
    Category,
    Channel,
    ChannelCategoryOverride,
    ChannelRating,
    Course,
    CourseVideo,
    HiddenCategory,
    HiddenChannel,
    HiddenVideo,
    Note,
    UserPreference,
    Video,
    WatchHistory,
    now_ms,
)

STATE_KEYS = (
    "hidden-ch",
    "hidden-vid",
    "hidden-cat",
    "cat-ov",
    "ratings",
    "courses",
    "history",
    "seen",
    "neg",
)
EXTRA_KEYS = ("reco", "filters", "ranker", "bandit")


# ------------------------------------------------------------------ history


def remember_watch(
    session: Session,
    video_id: int,
    *,
    engagement: float = 1.0,
    position_s: float | None = None,
    completed: bool = False,
    watched_at: int | None = None,
) -> WatchHistory:
    row = session.get(WatchHistory, video_id)
    if row is None:
        row = WatchHistory(video_id=video_id, watched_at=watched_at or now_ms())
        session.add(row)
    row.engagement = max(0.0, min(5.0, engagement))
    row.position_s = position_s
    row.completed = 1 if completed else 0
    row.watched_at = watched_at or now_ms()
    session.flush()
    return row


def history(session: Session, *, limit: int = 100, before: int | None = None):
    statement = (
        select(WatchHistory, Video, Channel)
        .join(Video, Video.id == WatchHistory.video_id)
        .join(Channel, Channel.id == Video.channel_id)
        .order_by(WatchHistory.watched_at.desc())
        .limit(limit)
    )
    if before is not None:
        statement = statement.where(WatchHistory.watched_at < before)
    return list(session.execute(statement).all())


def forget(session: Session, video_id: int) -> bool:
    row = session.get(WatchHistory, video_id)
    if row is None:
        return False
    session.delete(row)
    return True


def clear_history(session: Session) -> int:
    return int(session.query(WatchHistory).delete(synchronize_session=False) or 0)


def watched_ids(session: Session) -> set[int]:
    return {row[0] for row in session.execute(select(WatchHistory.video_id))}


# ------------------------------------------------------------------ preferences


def get_preference(session: Session, key: str, default=None):
    row = session.get(UserPreference, key)
    if row is None:
        return default
    try:
        return json.loads(row.value_json)
    except json.JSONDecodeError:
        return default


def set_preference(session: Session, key: str, value) -> None:
    row = session.get(UserPreference, key)
    payload = json.dumps(value, separators=(",", ":"), sort_keys=True)
    if row is None:
        session.add(UserPreference(key=key, value_json=payload, updated_at=now_ms()))
    else:
        row.value_json = payload
        row.updated_at = now_ms()
    session.flush()


# ------------------------------------------------------------------ simple marks


def set_rating(session: Session, channel_id: int, rating: int) -> None:
    if not 1 <= rating <= 5:
        raise ValueError("rating must be between 1 and 5")
    row = session.get(ChannelRating, channel_id)
    if row is None:
        session.add(ChannelRating(channel_id=channel_id, rating=rating, updated_at=now_ms()))
    else:
        row.rating, row.updated_at = rating, now_ms()
    session.flush()


def clear_rating(session: Session, channel_id: int) -> bool:
    row = session.get(ChannelRating, channel_id)
    if row is None:
        return False
    session.delete(row)
    return True


def ratings_map(session: Session) -> dict[int, int]:
    return {row.channel_id: row.rating for row in session.scalars(select(ChannelRating))}


def _hide(session: Session, model, key: str, value: int, hide: bool) -> None:
    if hide:
        if session.get(model, value) is None:
            session.add(model(**{key: value, "hidden_at": now_ms()}))
            session.flush()
    else:
        row = session.get(model, value)
        if row is not None:
            session.delete(row)


def hide(
    session: Session,
    kind: str,
    *,
    channel_id: int | None = None,
    video_id: int | None = None,
    category_id: int | None = None,
    hidden: bool = True,
) -> None:
    if kind == "channel" and channel_id is not None:
        _hide(session, HiddenChannel, "channel_id", channel_id, hidden)
    elif kind == "video" and video_id is not None:
        _hide(session, HiddenVideo, "video_id", video_id, hidden)
    elif kind == "category" and category_id is not None:
        _hide(session, HiddenCategory, "category_id", category_id, hidden)
    else:
        raise ValueError(f"cannot hide {kind}")


def hidden_sets(session: Session) -> dict[str, set[int]]:
    return {
        "channel": {row.channel_id for row in session.scalars(select(HiddenChannel))},
        "video": {row.video_id for row in session.scalars(select(HiddenVideo))},
        "category": {row.category_id for row in session.scalars(select(HiddenCategory))},
    }


def set_category_override(session: Session, channel_id: int, category_id: int | None) -> None:
    row = session.get(ChannelCategoryOverride, channel_id)
    if category_id is None:
        if row is not None:
            session.delete(row)
        return
    if row is None:
        session.add(
            ChannelCategoryOverride(
                channel_id=channel_id, category_id=category_id, updated_at=now_ms()
            )
        )
    else:
        row.category_id, row.updated_at = category_id, now_ms()
    session.flush()


# ------------------------------------------------------------------ courses


def create_course(session: Session, name: str, video_ids: Sequence[int] = ()) -> Course:
    course = Course(id=uuid.uuid4().hex[:12], name=name[:200], created_at=now_ms(), updated_at=now_ms())
    session.add(course)
    session.flush()
    for position, video_id in enumerate(video_ids):
        session.add(
            CourseVideo(course_id=course.id, video_id=video_id, position=position, added_at=now_ms())
        )
    session.flush()
    return course


def add_to_course(session: Session, course_id: str, video_id: int, position: int = 0) -> None:
    if session.get(CourseVideo, (course_id, video_id)) is not None:
        return
    session.add(
        CourseVideo(course_id=course_id, video_id=video_id, position=position, added_at=now_ms())
    )
    session.flush()


def remove_course(session: Session, course_id: str) -> bool:
    course = session.get(Course, course_id)
    if course is None:
        return False
    session.delete(course)  # cascade removes memberships, never the videos
    return True


def list_courses(session: Session) -> list[dict]:
    out = []
    for course in session.scalars(select(Course).order_by(Course.created_at)):
        members = [
            row[0]
            for row in session.execute(
                select(Video.yt_id)
                .join(CourseVideo, CourseVideo.video_id == Video.id)
                .where(CourseVideo.course_id == course.id)
                .order_by(CourseVideo.position)
            )
        ]
        out.append({"id": course.id, "name": course.name, "videos": members})
    return out


# ------------------------------------------------------------------ misc


def toggle_bookmark(session: Session, video_id: int, value: bool = True) -> bool:
    row = session.get(Bookmark, video_id)
    if value and row is None:
        session.add(Bookmark(video_id=video_id, created_at=now_ms()))
    elif not value and row is not None:
        session.delete(row)
    session.flush()
    return value


def add_note(session: Session, body: str, *, video_id=None, course_id=None, timestamp_s=None):
    note = Note(
        body=body,
        video_id=video_id,
        course_id=course_id,
        timestamp_s=timestamp_s,
        created_at=now_ms(),
        updated_at=now_ms(),
    )
    session.add(note)
    session.flush()
    return note


def list_notes(session: Session, video_id: int | None = None) -> list[Note]:
    statement = select(Note).order_by(Note.created_at.desc())
    if video_id is not None:
        statement = statement.where(Note.video_id == video_id)
    return list(session.scalars(statement))


# ------------------------------------------------------------------ export / import


def _channel_index(session: Session) -> dict[int, int]:
    """Reproduce the page's channel ordering (name, case-folded) for `history[].ch`."""
    rows = list(
        session.execute(select(Channel.id, Channel.name).order_by(func.lower(Channel.name)))
    )
    return {channel_id: index for index, (channel_id, _name) in enumerate(rows)}


def export_state(session: Session) -> dict:
    hidden = hidden_sets(session)
    index = _channel_index(session)
    channels_by_id = {row.id: row.yt_id for row in session.scalars(select(Channel))}
    videos_by_id = {row.id: row.yt_id for row in session.scalars(select(Video))}
    categories_by_id = {row.id: row.slug for row in session.scalars(select(Category))}

    history_rows = list(session.scalars(select(WatchHistory).order_by(WatchHistory.watched_at)))
    overrides = list(session.scalars(select(ChannelCategoryOverride)))
    ratings = {
        channels_by_id.get(row.channel_id, ""): row.rating
        for row in session.scalars(select(ChannelRating))
        if row.channel_id in channels_by_id
    }
    return {
        "hidden-ch": sorted(channels_by_id[i] for i in hidden["channel"] if i in channels_by_id),
        "hidden-vid": sorted(videos_by_id[i] for i in hidden["video"] if i in videos_by_id),
        "hidden-cat": sorted(
            categories_by_id[i] for i in hidden["category"] if i in categories_by_id
        ),
        "cat-ov": {
            channels_by_id[row.channel_id]: categories_by_id[row.category_id]
            for row in overrides
            if row.channel_id in channels_by_id and row.category_id in categories_by_id
        },
        "ratings": ratings,
        "courses": list_courses(session),
        "history": [
            {
                "id": videos_by_id.get(row.video_id, ""),
                "ch": index.get(
                    session.scalar(
                        select(Video.channel_id).where(Video.id == row.video_id)
                    ),
                    0,
                ),
                "t": row.watched_at,
                "w": round(row.engagement, 2),
            }
            for row in history_rows
            if row.video_id in videos_by_id
        ],
        "seen": get_preference(session, "seen", {"cats": [], "chs": []}),
        "neg": get_preference(session, "neg", []),
        "reco": get_preference(session, "reco", {}),
        "filters": get_preference(session, "filters", {}),
        "ranker": get_preference(session, "ranker", {}),
        "bandit": get_preference(session, "bandit", {}),
    }


def import_state(session: Session, state: dict) -> dict:
    """Apply a frontend backup in one transaction. Returns per-key counts."""
    counts: dict[str, int] = {}
    lookup = {
        "channels": {row.yt_id: row.id for row in session.scalars(select(Channel))},
        "videos": {row.yt_id: row.id for row in session.scalars(select(Video))},
        "categories": {row.slug: row.id for row in session.scalars(select(Category))},
    }

    for yt_id in state.get("hidden-ch", []) or []:
        if yt_id in lookup["channels"]:
            hide(session, "channel", channel_id=lookup["channels"][yt_id])
    counts["hidden-ch"] = len(state.get("hidden-ch", []) or [])

    for yt_id in state.get("hidden-vid", []) or []:
        if yt_id in lookup["videos"]:
            hide(session, "video", video_id=lookup["videos"][yt_id])
    counts["hidden-vid"] = len(state.get("hidden-vid", []) or [])

    for slug in state.get("hidden-cat", []) or []:
        if slug in lookup["categories"]:
            hide(session, "category", category_id=lookup["categories"][slug])
    counts["hidden-cat"] = len(state.get("hidden-cat", []) or [])

    for yt_id, slug in (state.get("cat-ov", {}) or {}).items():
        if yt_id in lookup["channels"] and slug in lookup["categories"]:
            set_category_override(session, lookup["channels"][yt_id], lookup["categories"][slug])
    counts["cat-ov"] = len(state.get("cat-ov", {}) or {})

    for yt_id, rating in (state.get("ratings", {}) or {}).items():
        if yt_id in lookup["channels"] and isinstance(rating, int) and 1 <= rating <= 5:
            set_rating(session, lookup["channels"][yt_id], rating)
    counts["ratings"] = len(state.get("ratings", {}) or {})

    imported = 0
    for entry in state.get("history", []) or []:
        video_id = lookup["videos"].get(entry.get("id"))
        if video_id is None:
            continue
        remember_watch(
            session,
            video_id,
            engagement=float(entry.get("w") or 1.0),
            watched_at=int(entry.get("t") or now_ms()),
        )
        imported += 1
    counts["history"] = imported

    for course in state.get("courses", []) or []:
        if not isinstance(course, dict) or not course.get("name"):
            continue
        video_ids = [lookup["videos"][v] for v in course.get("videos", []) if v in lookup["videos"]]
        create_course(session, str(course["name"]), video_ids)
    counts["courses"] = len(state.get("courses", []) or [])

    # accept both frontend key 'neg' and schema field name 'negative'
    pref_state = dict(state)
    if "negative" in pref_state and "neg" not in pref_state:
        pref_state["neg"] = pref_state["negative"]
    for key in ("seen", "neg", "reco", "filters", "ranker", "bandit"):
        if key in pref_state and pref_state[key] is not None:
            set_preference(session, key, pref_state[key])
            counts[key] = len(pref_state[key]) if hasattr(pref_state[key], "__len__") else 1
    return counts


def purge_events(session: Session, older_than_ms: int) -> int:
    from app.db.models import RecommendationEvent

    return int(
        session.query(RecommendationEvent)
        .filter(RecommendationEvent.at < older_than_ms)
        .delete(synchronize_session=False)
        or 0
    )
