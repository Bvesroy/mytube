"""Shared intelligence foundation: embeddings, transcript ingestion, extraction."""

from __future__ import annotations

import json

from sqlalchemy import select

from app.db.models import Concept, KnowledgeExtract, ProcessingProvenance, VideoConcept
from app.services.ai.embeddings import (
    EmbeddingEngine,
    cosine,
    embed_text,
    pack_vector,
    unpack_vector,
)
from app.services.knowledge.extraction import extract_knowledge, list_extracts, update_extract
from app.services.knowledge.transcripts import (
    TranscriptError,
    get_transcript,
    ingest_transcript,
    search_transcript,
)

SAMPLE = (
    "Computer vision is the field of extracting information from images. "
    "A convolutional neural network is a model that learns spatial filters from pixels. "
    "For example, a convolution slides a kernel across the image to detect edges. "
    "The convolution operation is defined as (f * g)(x) = integral f(t) g(x - t) dt. "
    "Before you learn computer vision, you should understand linear algebra and calculus. "
    "So the convolution layer is the basic building block of a CNN. "
    "In practice, we normalize the image and then apply the filter. "
    "This pipeline is used in production systems for object detection."
)


def test_embedding_is_deterministic_normalised_and_packable():
    a = embed_text("transformer architecture attention")
    b = embed_text("transformer architecture attention")
    c = embed_text("sourdough bread baking")
    assert a == b
    assert abs(sum(x * x for x in a) - 1.0) < 1e-5
    assert cosine(a, b) > 0.99
    assert cosine(a, c) < 0.5
    import pytest
    assert unpack_vector(pack_vector(a)) == pytest.approx(a, abs=1e-6)
    engine = EmbeddingEngine()
    assert engine.available and len(engine.embed("hello world")) == engine.dim


def test_transcript_ingest_is_idempotent_and_timed_citations_are_real(seeded_db):
    session = seeded_db.session_factory()
    segments = [
        {"start_s": 0, "end_s": 6, "text": "Computer vision is the field of extracting information from images."},
        {"start_s": 6, "end_s": 14, "text": "A convolutional neural network learns spatial filters from pixels."},
    ]
    first = ingest_transcript(session, "va1", segments=segments, source="creator_captions")
    second = ingest_transcript(session, "va1", segments=segments, source="creator_captions")
    assert first.transcript.id == second.transcript.id
    assert first.transcript.segment_count == 2
    assert first.timing_available is True
    hits = search_transcript(session, "va1", "convolutional spatial filters")
    assert hits and hits[0].start_s == 6.0

    # Replacing with untimed text must not keep stale timestamps.
    third = ingest_transcript(
        session, "va1", text=SAMPLE, source="user_supplied", force=True
    )
    assert third.timing_available is False
    session.commit()
    prov = session.scalars(select(ProcessingProvenance)).all()
    assert any(p.step == "transcript_ingest" for p in prov)


def test_transcript_rejects_invalid_timestamps(seeded_db):
    session = seeded_db.session_factory()
    try:
        ingest_transcript(
            session,
            "va1",
            segments=[
                {"start_s": 10, "end_s": 12, "text": "Second first is out of order."},
                {"start_s": 5, "end_s": 6, "text": "Then an earlier segment."},
            ],
        )
    except TranscriptError:
        pass
    else:  # pragma: no cover
        raise AssertionError("non-monotonic timestamps must be rejected")


def test_extraction_builds_concepts_definitions_prereqs_and_keeps_sources(seeded_db):
    session = seeded_db.session_factory()
    ingest_transcript(session, "va1", text=SAMPLE, source="user_supplied")
    result = extract_knowledge(session, "va1")
    assert result["created"] > 0
    kinds = {row["kind"] for row in result["extracts"]}
    for required in {"summary_short", "concept", "definition", "prerequisite", "flashcard_seed"}:
        assert required in kinds
    concepts = session.scalars(select(Concept)).all()
    assert concepts
    links = session.scalars(select(VideoConcept)).all()
    assert links and all(link.video_id for link in links)
    # Untimed source -> no invented timestamps anywhere.
    assert all(row["start_s"] is None for row in result["extracts"])

    # Re-running is idempotent (no duplicate rows).
    before = len(result["extracts"])
    again = extract_knowledge(session, "va1")
    assert len(again["extracts"]) == before


def test_manual_edit_survives_regeneration(seeded_db):
    session = seeded_db.session_factory()
    ingest_transcript(session, "va1", text=SAMPLE, source="user_supplied")
    extract_knowledge(session, "va1")
    definition = next(r for r in list_extracts(session, "va1") if r["kind"] == "definition")
    edited = update_extract(session, definition["id"], content="My own definition", detail="Edited by hand")
    assert edited["edited"] is True
    extract_knowledge(session, "va1", force=True)
    rows = list_extracts(session, "va1")
    kept = next(r for r in rows if r["id"] == definition["id"])
    assert kept["content"] == "My own definition"
    assert kept["detail"] == "Edited by hand"
    # Rejected rows are hidden but retained.
    update_extract(session, definition["id"], rejected=True)
    assert all(r["id"] != definition["id"] for r in list_extracts(session, "va1"))
    assert any(r["id"] == definition["id"] for r in list_extracts(session, "va1", include_rejected=True))


def test_extraction_requires_source_text(seeded_db):
    session = seeded_db.session_factory()
    from app.services.knowledge.extraction import ExtractionError

    try:
        extract_knowledge(session, "vb1")
    except ExtractionError:
        pass
    else:  # pragma: no cover
        raise AssertionError("extraction without transcript/description must fail")
