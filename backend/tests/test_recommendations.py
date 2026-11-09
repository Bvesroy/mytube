from datetime import datetime, timezone
import pytest
from app.db.engine import Database
from app.db.models import Channel, Library, Video, WatchHistory
from app.services.recommendations import recommend
from app.domain.weights import DEFAULT_W, FEATURES


@pytest.fixture()
def db(tmp_path):
    d = Database.open(tmp_path / "t.db")
    d.migrate()
    return d


def _seed(session):
    lib = Library(name="test")
    session.add(lib); session.flush()
    c = Channel(library_id=lib.id, yt_id="UCx", name="Chan", handle="@c")
    session.add(c); session.flush()
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    v1 = Video(yt_id="aaa", channel_id=c.id, title="Alpha", view_count=1000, duration_s=120, published_at=now_ms, is_available=1)
    v2 = Video(yt_id="bbb", channel_id=c.id, title="Beta", view_count=900, duration_s=120, published_at=now_ms, is_available=1)
    session.add_all([v1, v2]); session.flush()
    return v1, v2


def test_recommend_empty(db):
    with db.session() as s:
        assert recommend(s, count=10) == []


def test_recommend_returns_items_and_preserves_engine_contract(db):
    # Algorithms/weights preserved: uses same DEFAULT_W/score/MMR structure.
    assert "lexical" in DEFAULT_W and len(FEATURES) == 10
    with db.session() as s:
        _seed(s); s.commit()
        items = recommend(s, count=10)
        assert len(items) == 2
        assert {i.video.yt_id for i in items} == {"aaa", "bbb"}
        assert all(isinstance(i.score, float) for i in items)
        assert all(i.reason for i in items)


def test_recommend_consumes_watch_events_suppresses_completed(db):
    with db.session() as s:
        v1, v2 = _seed(s)
        s.add(WatchHistory(video_id=v2.id, watched_at=1, engagement=1.4, completed=1, playback_source="embedded"))
        s.commit()
        items = recommend(s, user_id="default", count=10)
        by = {i.video.yt_id: i for i in items}
        # Completed My Tube watch event should apply negative signal (penalty) without rewriting weights/engine.
        assert by["bbb"].features.get("negative", 0) > by["aaa"].features.get("negative", 0)
        assert by["aaa"].score >= by["bbb"].score
