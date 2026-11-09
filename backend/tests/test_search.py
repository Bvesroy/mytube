"""Search service tests (FTS5-backed)."""

from __future__ import annotations

from app.services.search import search_videos


def test_search_finds_matching_title(seeded_db):
    with seeded_db.session() as s:
        hits, cursor, total = search_videos(s, "python")
    assert total >= 1
    assert any("python" in h.title.lower() for h in hits)


def test_search_returns_empty_for_no_match(seeded_db):
    with seeded_db.session() as s:
        hits, cursor, total = search_videos(s, "xyznonexistentterm123")
    assert total == 0
    assert hits == []


def test_search_cursor_is_string_or_none(seeded_db):
    with seeded_db.session() as s:
        hits, cursor, total = search_videos(s, "tutorial", limit=1)
    # cursor may be None or a string offset
    assert cursor is None or isinstance(cursor, str)
