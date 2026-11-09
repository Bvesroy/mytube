"""Importer service tests."""

from __future__ import annotations

from sqlalchemy import select, func

from app.db.models import Channel, Video
from app.services.importer import import_channels, _parse_channel


def test_parse_channel_extracts_fields():
    raw = {
        "id": "UC123",
        "name": "Test Channel",
        "handle": "@test",
        "videos": [
            {"id": "v1", "title": "Video One", "durationSeconds": 120, "viewCount": 42},
            {"id": "v2", "title": ""},  # should get fallback title
            {"no_id": "x"},  # skipped
        ],
    }
    ch = _parse_channel(raw)
    assert ch.id == "UC123"
    assert ch.name == "Test Channel"
    assert ch.handle == "@test"
    assert len(ch.videos) == 2
    assert ch.videos[0]["duration_s"] == 120
    assert ch.videos[0]["view_count"] == 42


def test_import_channels_upserts(seeded_db):
    with seeded_db.session() as s:
        assert s.scalar(select(func.count()).select_from(Channel)) == 2
        assert s.scalar(select(func.count()).select_from(Video)) == 5


def test_import_is_idempotent(seeded_db):
    # Re-import same payloads
    from tests.conftest import SAMPLE_PAYLOADS

    result = import_channels(seeded_db.session_factory, SAMPLE_PAYLOADS, library_name="test", source_path="test.json")
    with seeded_db.session() as s:
        assert s.scalar(select(func.count()).select_from(Channel)) == 2
        assert s.scalar(select(func.count()).select_from(Video)) == 5
    # Should not create duplicates
    assert result.channels_upserted >= 0
