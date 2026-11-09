"""Features 7 and 8: chapters/navigation and multi-perspective discovery."""

from __future__ import annotations

import pytest

from app.db.models import Video, VideoChapter
from app.services.knowledge.extraction import extract_knowledge
from app.services.knowledge.transcripts import ingest_transcript
from app.services.video.chapters import (
    add_bookmark,
    delete_bookmark,
    generate_chapters,
    list_bookmarks,
    list_chapters,
    search_in_transcript,
    update_chapter,
)
from app.services.video.perspectives import perspectives_for

TEXT = (
    "A convolutional neural network is a model that learns filters. "
    "For example, a kernel slides over the image and detects edges. "
    "Next, let us look at training. Backpropagation updates the weights with gradient descent. "
    "Finally, to summarize, the network learns spatial features from pixels."
)


def _prepare(db):
    session = db.session_factory()
    video = session.query(Video).filter(Video.yt_id == "va1").one()
    video.duration_s = 600
    ingest_transcript(
        session,
        "va1",
        segments=[
            {"start_s": 0, "end_s": 25, "text": "A convolutional neural network is a model that learns filters. For example, a kernel slides over the image and detects edges."},
            {"start_s": 25, "end_s": 55, "text": "Next, let us look at training. Backpropagation updates the weights with gradient descent."},
            {"start_s": 55, "end_s": 90, "text": "Finally, to summarize, the network learns spatial features from pixels."},
        ],
        source="creator_captions",
    )
    extract_knowledge(session, "va1")
    ingest_transcript(session, "va2", text=TEXT, source="user_supplied")
    extract_knowledge(session, "va2")
    session.commit()
    return session


def test_generated_chapters_are_labeled_and_editable(seeded_db):
    session = _prepare(db=seeded_db)
    # A creator chapter must never be touched by generation.
    video = session.query(Video).filter(Video.yt_id == "va1").one()
    creator = VideoChapter(video_id=video.id, start_s=0, title="Creator intro", source="creator", position=0)
    session.add(creator)
    session.flush()

    result = generate_chapters(session, "va1")
    assert result["available"] is True
    assert result["authored_by"] == "auto_generated"
    assert result["chapters"]
    for chapter in result["chapters"]:
        assert chapter["authored_by"] == "auto_generated"
        assert chapter["is_creator_chapter"] is False
        assert chapter["start_s"] >= 0
        assert chapter["evidence"]["method"] == "tfidf_boundary"
    listing = list_chapters(session, "va1")
    assert listing["creator_chapters"]
    assert listing["creator_chapters"][0]["title"] == "Creator intro"
    assert listing["generated_chapters"]
    edited = update_chapter(session, listing["generated_chapters"][0]["id"], title="My chapter")
    assert edited["authored_by"] == "user"
    assert edited["title"] == "My chapter"


def test_chapters_degrade_without_timing(seeded_db):
    session = _prepare(db=seeded_db)
    result = generate_chapters(session, "va2")  # untimed transcript
    assert result["available"] is False
    assert "timestamp" in result["reason"].lower() or "timing" in result["reason"].lower()
    assert result["chapters"] == []


def test_transcript_search_has_real_timestamps_and_highlights(seeded_db):
    session = _prepare(db=seeded_db)
    result = search_in_transcript(session, "va1", "convolutional")
    assert result["hits"]
    first = result["hits"][0]
    assert first["start_s"] is not None
    assert first["highlights"]
    assert first["text"][first["highlights"][0][0]:first["highlights"][0][1]].lower() == "convolutional"


def test_timestamp_bookmarks_roundtrip(seeded_db):
    session = _prepare(db=seeded_db)
    bookmark = add_bookmark(session, "va1", timestamp_s=42, label="Kernel example")
    assert list_bookmarks(session, "va1")[0]["label"] == "Kernel example"
    with pytest.raises(ValueError):
        add_bookmark(session, "va1", timestamp_s=99999, label="Outside video")
    delete_bookmark(session, bookmark["id"])
    assert list_bookmarks(session, "va1") == []


def test_perspectives_are_library_only_and_explained(seeded_db):
    session = _prepare(db=seeded_db)
    result = perspectives_for(session, "va1", limit=10)
    assert result["only_subscription_library"] is True
    assert result["items"]
    assert set(result["perspectives"]) >= {"beginner", "advanced", "practical", "alternative"}
    seen_titles = set()
    for item in result["items"]:
        assert item["video_id"] != "va1"
        assert item["reason"]
        assert item["evidence"]["shared_concepts"] or item["evidence"]["embedding_similarity"] > 0
        assert item["title"] not in seen_titles
        seen_titles.add(item["title"])
    # Different channels are preferred for alternative explanations.
    assert any(i["perspective"] == "alternative" for i in result["items"])
