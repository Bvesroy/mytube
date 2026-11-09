"""Phase 2 watch history foundation tests: idempotency, transactions, dual-history truthfulness."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_tmp = tempfile.mkdtemp()
os.environ["MYTUBE_DB_PATH"] = str(Path(_tmp) / "history_api_test.db")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select, func  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.models import WatchHistory, WatchSession  # noqa: E402
from app.main import make_app  # noqa: E402
from app.services.importer import import_channels  # noqa: E402
from app.services import history as history_svc  # noqa: E402


SAMPLE = [
    {
        "id": "UC_HIST_A",
        "name": "History Alpha",
        "handle": "@histalpha",
        "videos": [
            {"id": "vh1", "title": "History video one", "duration_s": 600, "view_count": 10},
            {"id": "vh2", "title": "History video two", "duration_s": 300, "view_count": 20},
        ],
    }
]


@pytest.fixture(scope="module")
def app_client():
    get_settings.cache_clear()  # type: ignore[attr-defined]
    app = make_app()
    with TestClient(app) as c:
        db = app.state.db
        if hasattr(db, "migrate"):
            db.migrate()
        import_channels(db.session_factory, SAMPLE, library_name="main", source_path="test")
        yield app, c


def test_schema_has_watch_sessions(app_client):
    app, _ = app_client
    with app.state.db.engine.connect() as conn:
        names = [r[0] for r in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")]
    assert "watch_sessions" in names
    assert "watch_history" in names


def test_start_progress_complete_idempotent(app_client):
    app, client = app_client
    # start
    r = client.post("/api/v1/history/start", json={"video_id": "vh1", "playback_source": "embedded", "session_id": "sess1", "position_s": 5})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["session_id"] == "sess1"
    assert d["playback_source"] == "embedded"
    assert d["youtube_history_confirmed"] is False

    # idempotent start with same session_id/video
    r2 = client.post("/api/v1/history/start", json={"video_id": "vh1", "playback_source": "embedded", "session_id": "sess1", "position_s": 10})
    assert r2.status_code == 200
    assert r2.json()["session_id"] == "sess1"

    # progress
    rp = client.post("/api/v1/history/progress", json={"session_id": "sess1", "position_s": 30, "watched_delta_s": 25})
    assert rp.status_code == 200
    assert rp.json()["position_s"] >= 30

    # complete
    rc = client.post("/api/v1/history/complete", json={"session_id": "sess1", "position_s": 60, "watched_s": 55, "completed": True})
    assert rc.status_code == 200
    assert rc.json()["completed"] is True
    assert rc.json()["status"] == "completed"

    # aggregated history exists
    rg = client.get("/api/v1/history/vh1")
    assert rg.status_code == 200
    h = rg.json()
    assert h["video_id"] == "vh1"
    assert h["mytube_recorded"] is True
    assert h["youtube_history_confirmed"] is False
    assert h["watch_count"] >= 1


def test_repeated_events_do_not_duplicate_session_rows(app_client):
    app, client = app_client
    # use a new session for vh2
    client.post("/api/v1/history/start", json={"video_id": "vh2", "session_id": "sess2"})
    client.post("/api/v1/history/progress", json={"session_id": "sess2", "position_s": 1})
    client.post("/api/v1/history/progress", json={"session_id": "sess2", "position_s": 2})
    client.post("/api/v1/history/progress", json={"session_id": "sess2", "position_s": 3})
    with app.state.db.session() as s:
        n = s.scalar(select(func.count()).select_from(WatchSession).where(WatchSession.id == "sess2"))
    assert int(n or 0) == 1


def test_external_youtube_is_initiation_only(app_client):
    app, client = app_client
    r = client.post("/api/v1/history/start", json={"video_id": "vh2", "playback_source": "external_youtube", "session_id": "sess_ext"})
    assert r.status_code == 200
    d = r.json()
    assert d["playback_source"] == "external_youtube"
    assert d["youtube_history_confirmed"] is False
    assert d["external_initiated_at"] is not None
    rg = client.get("/api/v1/history/vh2")
    assert rg.status_code == 200
    h = rg.json()
    # must never be marked as confirmed YouTube account history
    assert h["youtube_history_confirmed"] is False
    assert h["external_youtube_initiated_at"] is not None or h["playback_source"] == "external_youtube"


def test_history_list_and_delete(app_client):
    _, client = app_client
    rl = client.get("/api/v1/history")
    assert rl.status_code == 200
    data = rl.json()
    assert "items" in data
    # delete one
    rd = client.delete("/api/v1/history/vh1")
    assert rd.status_code == 200
    assert rd.json().get("deleted") is True
    rg = client.get("/api/v1/history/vh1")
    assert rg.status_code == 404


def test_unknown_video_rejected(app_client):
    _, client = app_client
    r = client.post("/api/v1/history/start", json={"video_id": "DOES_NOT_EXIST", "playback_source": "embedded"})
    assert r.status_code == 404


def test_unknown_session_rejected(app_client):
    _, client = app_client
    r = client.post("/api/v1/history/progress", json={"session_id": "nope", "position_s": 1})
    assert r.status_code == 404
