"""Schema/migration smoke tests."""

from __future__ import annotations

from sqlalchemy import text

from app.db.engine import table_names


def test_fresh_db_creates_core_tables(temp_db):
    names = table_names(temp_db.engine)
    assert "channels" in names
    assert "videos" in names
    assert "categories" in names
    assert "background_jobs" in names
    assert "schema_version" in names
    assert "video_fts" in names
    assert "watch_history" in names
    assert "watch_sessions" in names


def test_schema_version_seeded(temp_db):
    with temp_db.engine.connect() as conn:
        v = conn.execute(text("SELECT max(version) FROM schema_version")).scalar_one()
    assert int(v) == 1


def test_integrity_check_ok(temp_db):
    result = temp_db.integrity_check()
    assert result == "ok"
