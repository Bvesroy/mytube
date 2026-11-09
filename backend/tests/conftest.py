"""Test fixtures: temp DB, session factory, seeded library."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.db.engine import Database
from app.services.importer import import_channels


SAMPLE_PAYLOADS = [
    {
        "id": "UC_TEST_A",
        "name": "Channel Alpha",
        "description": "Testing and software engineering",
        "handle": "@alpha",
        "videos": [
            {"id": "va1", "title": "Python testing basics", "duration_s": 600, "view_count": 1000, "published_at": 1700000000000},
            {"id": "va2", "title": "SQLite performance tips", "duration_s": 900, "view_count": 500, "published_at": 1700001000000},
            {"id": "va3", "title": "FastAPI internals", "duration_s": 1200, "view_count": 2000, "published_at": 1700002000000},
        ],
    },
    {
        "id": "UC_TEST_B",
        "name": "Channel Beta",
        "description": "Music and audio production",
        "handle": "@beta",
        "videos": [
            {"id": "vb1", "title": "Mixing vocals tutorial", "duration_s": 480, "view_count": 3000, "published_at": 1700003000000},
            {"id": "vb2", "title": "Synthesizer basics", "duration_s": 720, "view_count": 1500, "published_at": 1700004000000},
        ],
    },
]


@pytest.fixture()
def temp_db():
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        db = Database.open(db_path)
        db.migrate()
        yield db
        db.close()


@pytest.fixture()
def seeded_db(temp_db):
    import_channels(temp_db.session_factory, SAMPLE_PAYLOADS, library_name="test", source_path="test.json")
    return temp_db
