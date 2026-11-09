"""End-to-end API contracts for all ten intelligence features.

The suite uses the real FastAPI app and a temporary SQLite database. No network,
no model download and no external AI service is involved.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.config import settings_for
from app.main import make_app
from app.services.importer import import_channels

SAMPLE = [
    {
        "id": "UC_INT_A",
        "name": "Intelligence Alpha",
        "description": "Computer vision and machine learning",
        "handle": "@intalpha",
        "videos": [
            {"id": "ia1", "title": "Convolutional neural networks explained", "duration_s": 600, "view_count": 1000},
            {"id": "ia2", "title": "Computer vision with Python", "duration_s": 900, "view_count": 800},
        ],
    },
    {
        "id": "UC_INT_B",
        "name": "Intelligence Beta",
        "description": "Practical machine learning",
        "handle": "@intbeta",
        "videos": [
            {"id": "ib1", "title": "Build a CNN from scratch", "duration_s": 720, "view_count": 600},
        ],
    },
]

SEGMENTS_A = [
    {"start_s": 0, "end_s": 15, "text": "A convolutional neural network is a model that learns filters from images."},
    {"start_s": 15, "end_s": 30, "text": "The convolution layer slides a kernel and detects edges and textures."},
    {"start_s": 30, "end_s": 45, "text": "Each filter learns a different pattern from the input image."},
    {"start_s": 45, "end_s": 60, "text": "Before you learn this, you should understand linear algebra and calculus."},
    {"start_s": 60, "end_s": 75, "text": "Next, training uses backpropagation to compute gradients."},
    {"start_s": 75, "end_s": 90, "text": "Gradient descent updates the weights to reduce the loss."},
    {"start_s": 90, "end_s": 105, "text": "Finally, pooling reduces the size of the feature maps."},
    {"start_s": 105, "end_s": 120, "text": "In summary, a CNN learns features and then classifies the image."},
]
SEGMENTS_C = [
    {"start_s": 0, "end_s": 15, "text": "Computer vision is a field that teaches machines to understand images."},
    {"start_s": 15, "end_s": 30, "text": "We use Python and OpenCV to load and transform pictures."},
    {"start_s": 30, "end_s": 45, "text": "Next, a convolutional neural network classifies the image."},
    {"start_s": 45, "end_s": 60, "text": "Finally, evaluate accuracy on a held out test set."},
]
SEGMENTS_B = [
    {"start_s": 0, "end_s": 8, "text": "We build a convolutional neural network in Python from scratch."},
    {"start_s": 8, "end_s": 16, "text": "The kernel detects edges; pooling reduces the feature map size."},
    {"start_s": 16, "end_s": 24, "text": "Backpropagation and gradient descent train the network."},
]


@pytest.fixture(scope="module")
def client():
    with tempfile.TemporaryDirectory() as td:
        settings = settings_for(Path(td))
        app = make_app(settings)
        from fastapi.testclient import TestClient

        with TestClient(app) as c:
            db = app.state.db
            db.migrate()
            import_channels(db.session_factory, SAMPLE, library_name="intel", source_path="intel.json")
            # Give the recommendations seed a watch row so perspectives has a source.
            session = db.session_factory()
            from app.db.models import Video, WatchHistory, now_ms

            video = session.query(Video).filter(Video.yt_id == "ia1").one()
            history = session.query(WatchHistory).filter(WatchHistory.video_id == video.id).one_or_none()
            if history is None:
                history = WatchHistory(video_id=video.id, channel_id=video.channel_id)
                session.add(history)
            history.last_watched_at = now_ms()
            history.total_watch_s = 600
            history.completed = 1
            history.watch_count = 1
            session.commit()
            session.close()
            yield c


def _ingest(client, video_id, segments):
    r = client.post(
        "/api/v1/transcripts/ingest",
        json={"video_id": video_id, "segments": segments, "source": "creator_captions"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_transcript_ingest_search_and_export(client):
    a = _ingest(client, "ia1", SEGMENTS_A)
    b = _ingest(client, "ib1", SEGMENTS_B)
    c = _ingest(client, "ia2", SEGMENTS_C)
    assert a["timing_available"] is True and a["segment_count"] == 8
    assert b["segment_count"] == 3 and c["segment_count"] == 4

    r = client.get("/api/v1/transcripts/ia1")
    assert r.status_code == 200 and r.json()["word_count"] > 10
    r = client.get("/api/v1/transcripts/ia1/search", params={"q": "kernel"})
    assert r.status_code == 200 and r.json()["hits"]
    assert r.json()["hits"][0]["start_s"] is not None

    for video_id in ("ia1", "ib1", "ia2"):
        r = client.post("/api/v1/knowledge/extract", json={"video_id": video_id})
        assert r.status_code == 200, r.text
        assert r.json()["concepts"]
    summary = client.get("/api/v1/knowledge/videos/ia1").json()
    assert summary["extracts"]
    exported = client.get("/api/v1/knowledge/videos/ia1/export").json()
    assert exported["format"] == "mytube-knowledge-v1"


def test_graph_ask_and_research_comparison(client):
    indexed = client.post("/api/v1/embeddings/index", params={"video_ids": ["ia1", "ib1", "ia2"]})
    assert indexed.status_code == 200, indexed.text

    graph = client.get("/api/v1/graph", params={"limit": 100}).json()
    assert graph["nodes"] and graph["edges"]
    assert any(edge["provenance"] for edge in graph["edges"])

    asked = client.post("/api/v1/ask", json={"question": "How does a convolutional neural network detect edges?", "limit": 5})
    assert asked.status_code == 200, asked.text
    answer = asked.json()
    assert answer["citations"]
    for citation in answer["citations"]:
        assert citation["video_id"] in {"ia1", "ib1", "ia2"}

    compared = client.post("/api/v1/research/compare", json={"video_ids": ["ia1", "ib1"]})
    assert compared.status_code == 200, compared.text
    comparison = compared.json()
    assert comparison["document_id"]
    assert comparison["common_explanations"]
    docs = client.get("/api/v1/research/documents").json()["documents"]
    assert any(doc["id"] == comparison["document_id"] for doc in docs)
    detail = client.get(f"/api/v1/research/documents/{comparison['document_id']}")
    assert detail.status_code == 200 and detail.json()["kind"] == "comparison"

    saved = client.post(
        "/api/v1/research/ask/save",
        json={"question": "What is a CNN?", "answer": "A model [1].", "citations": answer["citations"][:1]},
    )
    assert saved.status_code == 200 and saved.json()["id"]


def test_learning_paths_and_flashcards(client):
    path = client.post("/api/v1/learning/paths", json={"objective": "computer vision", "max_concepts": 4})
    assert path.status_code == 200, path.text
    data = path.json()
    assert data["stages"] or data["missing"]
    if data["stages"]:
        item = data["stages"][0]["items"][0]
        moved = client.post(
            f"/api/v1/learning/paths/{data['id']}/items/{item['id']}/status",
            json={"status": "done"},
        )
        assert moved.status_code == 200
        assert moved.json()["progress"]["done"] >= 1
        nxt = client.get(f"/api/v1/learning/paths/{data['id']}/next")
        assert nxt.status_code == 200

    deck = client.post("/api/v1/flashcards/generate", json={"video_id": "ia1"}).json()
    # Generation is idempotent: a second call reports skips rather than duplicates.
    assert deck["created"] + deck["skipped"] >= 1
    assert deck["cards"]
    cards = client.get("/api/v1/flashcards/due", params={"deck_id": deck["deck_id"]}).json()["cards"]
    assert cards
    review = client.post(f"/api/v1/flashcards/{cards[0]['id']}/review", json={"rating": 3})
    assert review.status_code == 200, review.text
    assert review.json()["card"]["due_at"]
    stats = client.get("/api/v1/flashcards/stats").json()
    assert stats["reviews_window"] >= 1
    exported = client.get(f"/api/v1/flashcards/export/{deck['deck_id']}").json()
    assert exported["cards"]
    manual = client.post(
        "/api/v1/flashcards",
        json={"front": "What is a kernel?", "back": "A small learnable filter.", "deck_id": deck["deck_id"]},
    )
    assert manual.status_code == 200
    deleted = client.delete(f"/api/v1/flashcards/{manual.json()['id']}")
    assert deleted.status_code == 200
    restored = client.post(f"/api/v1/flashcards/{manual.json()['id']}/restore")
    assert restored.status_code == 200


def test_chapters_navigation_perspectives_analytics_radar(client):
    generated = client.post("/api/v1/videos/ia1/chapters/generate", json={"min_chapter_s": 15})
    assert generated.status_code == 200, generated.text
    chapters = generated.json()
    assert chapters["available"] is True
    assert chapters["chapters"]
    assert chapters["chapters"][0]["authored_by"] == "auto_generated"
    first = chapters["chapters"][0]
    patched = client.patch(f"/api/v1/chapters/{first['id']}", json={"title": "My chapter"})
    assert patched.status_code == 200 and patched.json()["title"] == "My chapter"

    search = client.get("/api/v1/videos/ia1/transcript/search", params={"q": "backpropagation"})
    assert search.status_code == 200 and search.json()["hits"]
    bookmark = client.post("/api/v1/videos/ia1/bookmarks", json={"timestamp_s": 12.5, "label": "key idea"})
    assert bookmark.status_code == 200
    listed = client.get("/api/v1/videos/ia1/bookmarks").json()["bookmarks"]
    assert listed and listed[0]["timestamp_s"] == 12.5
    assert client.delete(f"/api/v1/bookmarks/{listed[0]['id']}").status_code == 200

    perspectives = client.get("/api/v1/videos/ia1/perspectives")
    assert perspectives.status_code == 200, perspectives.text
    assert "perspectives" in perspectives.json()

    timeline = client.get("/api/v1/analytics/timeline", params={"period": "month", "buckets": 6})
    assert timeline.status_code == 200
    assert len(timeline.json()["buckets"]) == 6
    summary = client.get("/api/v1/analytics/summary", params={"period": "month", "buckets": 6}).json()
    assert "emerging_interests" in summary["summary"]

    scanned = client.post("/api/v1/radar/scan", json={"period": "manual"})
    assert scanned.status_code == 200, scanned.text
    report = scanned.json()
    assert report["id"]
    latest = client.get("/api/v1/radar/report").json()["report"]
    assert latest["id"] == report["id"]
    config = client.put("/api/v1/radar/config", json={"enabled": False}).json()["config"]
    assert config["enabled"] is False
    blocked = client.post("/api/v1/radar/scan", json={"period": "manual"}).json()
    assert blocked["enabled"] is False
    client.put("/api/v1/radar/config", json={"enabled": True})


def test_ai_status_and_pipeline(client):
    status = client.get("/api/v1/ai/status")
    assert status.status_code == 200
    body = status.json()
    assert body["policy"]["local_only"] is True
    assert body["embedding"]["available"] is True
    assert client.get("/api/v1/ai/models").status_code == 200

    config = client.put(
        "/api/v1/ai/config",
        json={"enabled": False, "provider": "ollama", "model": "llama3.2:3b"},
    )
    assert config.status_code == 200
    assert config.json()["selected"]["enabled"] is False

    pipeline_config = client.put(
        "/api/v1/pipeline/config", json={"steps": ["extract", "embed"], "max_videos_per_run": 5}
    )
    assert pipeline_config.status_code == 200

    result = client.post(
        "/api/v1/pipeline/run",
        json={"video_ids": ["ia1", "ib1"], "steps": ["extract", "embed", "chapters"], "run_now": True},
    )
    assert result.status_code == 200, result.text
    payload = result.json()
    assert payload["result"]["videos"] == 2
    assert not any(item["errors"] for item in payload["result"]["results"])
