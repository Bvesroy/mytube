"""Feature 1 (knowledge graph) and Feature 2 (Ask My Tube)."""

from __future__ import annotations

from app.services.knowledge.ask import ask
from app.services.knowledge.extraction import extract_knowledge
from app.services.knowledge.graph import build_graph, graph_node, search_nodes
from app.services.knowledge.index import index_library, related_videos
from app.services.knowledge.transcripts import ingest_transcript

V1 = (
    "A convolutional neural network is a model that learns spatial filters from pixels. "
    "The convolution layer applies a kernel across the image. For example, it detects edges. "
    "Before you learn convolutional networks, you should understand linear algebra. "
    "Training uses backpropagation and gradient descent to update the weights."
)
V2 = (
    "Computer vision is the field of extracting information from images. "
    "A convolutional network is also called a CNN. "
    "The convolution operation slides a filter over the input. "
    "Unlike a fully connected layer, it shares weights and preserves spatial structure. "
    "You need linear algebra to understand the filter bank."
)


def _prepare(db):
    session = db.session_factory()
    ingest_transcript(
        session,
        "va1",
        segments=[
            {"start_s": 0, "end_s": 12, "text": V1.split(" Before ")[0]},
            {"start_s": 12, "end_s": 20, "text": "Before you learn convolutional networks, you should understand linear algebra."},
        ],
        source="creator_captions",
    )
    ingest_transcript(
        session,
        "va2",
        segments=[{"start_s": 0, "end_s": 30, "text": V2}],
        source="creator_captions",
    )
    extract_knowledge(session, "va1")
    extract_knowledge(session, "va2")
    counts = index_library(session, video_yt_ids=["va1", "va2"])
    session.commit()
    return session, counts


def test_graph_has_evidence_backed_nodes_and_edges(seeded_db):
    session, counts = _prepare(seeded_db)
    assert counts["videos"] == 2
    graph = build_graph(session, include_videos=True, min_confidence=0.0)
    assert graph["stats"]["concepts"] >= 2
    assert graph["stats"]["videos"] == 2
    assert graph["stats"]["edges"] > 0
    for edge in graph["edges"]:
        assert edge["provenance"].get("method")
        assert 0.0 <= edge["confidence"] <= 1.0
        assert edge["id"] in {e["id"] for e in graph["edges"]}
    relations = {e["relation"] for e in graph["edges"]}
    assert "discusses" in relations
    assert "specializes" in relations
    # Filtering by watched state must only keep watched videos.
    watched = build_graph(session, include_videos=True, watched=False)
    assert all(not n.get("watched", False) for n in watched["nodes"] if n["type"] == "video")
    found = search_nodes(session, "convolution")
    assert found and found[0]["id"].startswith("concept:")
    detail = graph_node(session, found[0]["id"])
    assert detail and detail["videos"]


def test_related_videos_use_stored_embeddings(seeded_db):
    session, _ = _prepare(seeded_db)
    related = related_videos(session, "va1", limit=5)
    assert related
    assert related[0]["video_id"] == "va2"
    assert related[0]["provenance"]["method"] == "embedding_cosine"


def test_ask_returns_real_citations_and_timestamps(seeded_db):
    session, _ = _prepare(seeded_db)
    result = ask(session, "What is a convolutional neural network?", limit=6)
    assert result["mode"] == "extractive"
    assert result["sufficient_evidence"] is True
    assert result["citations"]
    for cite in result["citations"]:
        assert cite["video_id"] in {"va1", "va2"}
        assert cite["title"]
        assert cite["timestamp_s"] is None or cite["timestamp_s"] >= 0
        assert cite["excerpt"]
    assert "[1]" in result["answer"]
    # The transcript really has timing, so at least one citation should show it.
    assert any(c["timestamp_s"] is not None for c in result["citations"])


def test_ask_says_insufficient_without_fabricating(seeded_db):
    session, _ = _prepare(seeded_db)
    result = ask(session, "quantum chromodynamics lattice renormalisation", limit=6)
    assert result["citations"] == []
    assert result["sufficient_evidence"] is False
    assert "couldn't find evidence" in result["answer"]


class _FakeLLM:
    available = True
    provider = "ollama"
    model = "fake"

    def __init__(self, text):
        self.text = text

    def generate(self, prompt, **kwargs):
        return self.text


def test_llm_answers_require_valid_citations(seeded_db):
    session, _ = _prepare(seeded_db)
    good = ask(session, "What is a convolutional network?", use_llm=True, llm=_FakeLLM("It learns filters [1]."))
    assert good["mode"] == "llm"
    assert good["answer"] == "It learns filters [1]."
    bad = ask(session, "What is a convolutional network?", use_llm=True, llm=_FakeLLM("It is obviously magic."))
    assert bad["mode"] == "extractive"
    assert any("rejected" in note for note in bad["notes"])
    out_of_range = ask(session, "What is a convolutional network?", use_llm=True, llm=_FakeLLM("It learns filters [99]."))
    assert out_of_range["mode"] == "extractive"
