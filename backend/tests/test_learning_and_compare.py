"""Features 4, 5 and 6: comparison, learning paths, spaced repetition."""

from __future__ import annotations

import pytest

from app.services.knowledge.compare import compare_videos, get_document, list_documents
from app.services.knowledge.extraction import extract_knowledge
from app.services.knowledge.transcripts import ingest_transcript
from app.services.learning import fsrs
from app.services.learning.paths import (
    LearningPathError,
    generate_path,
    get_path,
    list_paths,
    move_item,
    set_item_status,
    update_path,
)

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
    ingest_transcript(session, "va2", segments=[{"start_s": 0, "end_s": 30, "text": V2}], source="creator_captions")
    extract_knowledge(session, "va1")
    extract_knowledge(session, "va2")
    session.commit()
    return session


# ---------------------------------------------------------------- comparison


def test_comparison_is_evidence_backed_and_saved(seeded_db):
    session = _prepare(seeded_db)
    result = compare_videos(session, ["va1", "va2"], objective="compare convolutional networks")
    assert result["only_subscription_library"] is True
    assert result["document_id"]
    assert result["common_explanations"]
    for claim in result["common_explanations"]:
        assert claim["claim_type"] == "inferred"
        assert claim["rule"]
    for claim in result["agreements"] + result["possible_contradictions"]:
        assert claim["evidence"]
        for evidence in claim["evidence"]:
            assert evidence["video_id"] in {"va1", "va2"}
            assert evidence["claim_type"] == "directly_stated"
    for approach in result["different_approaches"]:
        assert approach["video_id"] in {"va1", "va2"}
    docs = list_documents(session, kind="comparison")
    assert docs and docs[0]["id"] == result["document_id"]
    saved = get_document(session, result["document_id"])
    assert saved and saved["body"]["videos"]


def test_comparison_requires_two_videos(seeded_db):
    session = _prepare(seeded_db)
    with pytest.raises(ValueError):
        compare_videos(session, ["va1"])


# ---------------------------------------------------------------- learning paths


def test_learning_path_uses_only_library_videos(seeded_db):
    session = _prepare(seeded_db)
    path = generate_path(session, "convolutional neural network", level="auto")
    assert path["only_subscription_library"] is True
    assert path["stages"]
    items = [item for stage in path["stages"] for item in stage["items"]]
    assert items
    assert {item["video_id"] for item in items} <= {"va1", "va2"}
    assert path["progress"]["total"] == len(items)
    assert path["next_item"] is not None
    # Completing an item updates progress.
    first = items[0]
    set_item_status(session, first["id"], "done")
    refreshed = get_path(session, path["id"])
    assert refreshed["progress"]["done"] >= 1
    # Manual reorder is persisted.
    if refreshed["stages"][0]["items"]:
        moved = move_item(session, refreshed["stages"][0]["items"][0]["id"], position=0)
        assert moved["position"] == 0
    assert list_paths(session)


def test_learning_path_reports_missing_material(seeded_db):
    session = _prepare(seeded_db)
    with pytest.raises(LearningPathError):
        generate_path(session, "quantum chromodynamics renormalisation")
    path = generate_path(session, "convolutional network")
    with pytest.raises(LearningPathError):
        update_path(session, path["id"], status="nonsense")


# ---------------------------------------------------------------- FSRS


def test_fsrs_intervals_order_again_hard_good_easy():
    results = [
        fsrs.schedule(
            state="new",
            stability=0.0,
            difficulty=0.0,
            rating=rating,
            last_review_at=None,
            now=1_700_000_000_000,
        )
        for rating in (1, 2, 3, 4)
    ]
    assert results[0]["due_at"] < results[1]["due_at"] < results[2]["due_at"] < results[3]["due_at"]
    assert results[0]["state"] == "relearning"
    assert all(r["stability"] > 0 for r in results)
    with pytest.raises(fsrs.FlashcardError):
        fsrs.schedule(state="new", stability=0, difficulty=0, rating=5, last_review_at=None)


def test_flashcards_generate_review_delete_restore_export(seeded_db):
    session = _prepare(seeded_db)
    generated = fsrs.generate_cards_from_video(session, "va1")
    assert generated["created"] > 0
    again = fsrs.generate_cards_from_video(session, "va1")
    assert again["created"] == 0 and again["skipped"] > 0
    due = fsrs.due_cards(session, limit=10)
    assert due and due[0]["video_id"]
    card_id = due[0]["id"]
    result = fsrs.review_card(session, card_id, rating=3)
    assert result["card"]["reps"] == 1
    assert result["card"]["due_at"] > due[0]["due_at"]
    assert fsrs.review_stats(session)["reviews_window"] == 1
    # Deleting hides it; restoring brings it back with its review history intact.
    fsrs.delete_card(session, card_id)
    assert all(c["id"] != card_id for c in fsrs.due_cards(session, limit=200))
    fsrs.restore_card(session, card_id)
    deck_id = generated["deck_id"]
    assert any(c["id"] == card_id for c in fsrs.export_deck(session, deck_id)["cards"])
    exported = fsrs.export_deck(session, deck_id)
    assert exported["format"] == "mytube-flashcards-v1"
    imported = fsrs.import_deck(session, exported)
    assert imported["skipped"] >= 1
    assert fsrs.review_stats(session)["total_cards"] >= generated["created"]
