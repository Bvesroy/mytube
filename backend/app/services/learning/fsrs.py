"""Spaced repetition with an FSRS-compatible scheduler (Feature 6).

The algorithm follows the published FSRS-5 formulation (stability, difficulty,
retrievability and the 19 default weights). It is implemented here in ~100 lines of
stdlib Python so the app does not gain a mandatory dependency; the review history is
stored in SQL independently from the source videos, so it survives video removal.

Cards generated from extraction always keep `video_id`, `extract_id` and the source
timestamp/excerpt. Review scheduling state is per card.
"""

from __future__ import annotations

import hashlib
import json
import math
import secrets
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Flashcard,
    FlashcardDeck,
    KnowledgeExtract,
    ReviewLog,
    Video,
    now_ms,
)
from app.services.knowledge.extraction import ExtractionError, extract_knowledge, list_extracts

DAY_MS = 86_400_000
MINUTE_MS = 60_000

# FSRS-5 default weights (open-spaced-repetition/fsrs4anki defaults).
W = (
    0.40255, 1.18385, 3.173, 15.69105, 7.1949, 0.5345, 1.4604, 0.0046,
    1.54575, 0.1192, 1.01925, 1.9395, 0.11, 0.29605, 2.2698, 0.2315,
    2.9898, 0.51655, 0.6621,
)
DECAY = -0.5
FACTOR = 19.0 / 81.0
DEFAULT_RETENTION = 0.9


class FlashcardError(ValueError):
    pass


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def retrievability(elapsed_days: float, stability: float) -> float:
    if stability <= 0:
        return 0.0
    return (1.0 + FACTOR * max(0.0, elapsed_days) / stability) ** DECAY


def interval_days(stability: float, retention: float = DEFAULT_RETENTION) -> float:
    stability = max(0.1, stability)
    retention = _clamp(retention, 0.7, 0.99)
    return max(0.5, stability / FACTOR * (retention ** (1.0 / DECAY) - 1.0))


def init_stability(rating: int) -> float:
    return max(0.1, W[max(0, min(3, rating - 1))])


def init_difficulty(rating: int) -> float:
    return _clamp(W[4] - math.exp(W[5] * (rating - 1)) + 1.0, 1.0, 10.0)


def next_difficulty(difficulty: float, rating: int) -> float:
    delta = -W[6] * (rating - 3)
    value = difficulty + delta
    # Mean reversion toward the difficulty of an Easy first review.
    value = W[7] * init_difficulty(4) + (1.0 - W[7]) * value
    return _clamp(value, 1.0, 10.0)


def next_stability(
    *, difficulty: float, stability: float, recall: float, rating: int
) -> float:
    if rating == 1:
        value = (
            W[11]
            * (difficulty ** -W[12])
            * (((stability + 1.0) ** W[13]) - 1.0)
            * math.exp(W[14] * (1.0 - recall))
        )
    else:
        hard_penalty = W[15] if rating == 2 else 1.0
        easy_bonus = W[16] if rating == 4 else 1.0
        value = stability * (
            1.0
            + math.exp(W[8])
            * (11.0 - difficulty)
            * (stability ** -W[9])
            * (math.exp(W[10] * (1.0 - recall)) - 1.0)
            * hard_penalty
            * easy_bonus
        )
    return max(0.1, value)


def schedule(
    *,
    state: str,
    stability: float,
    difficulty: float,
    rating: int,
    last_review_at: int | None,
    now: int | None = None,
    retention: float = DEFAULT_RETENTION,
) -> dict[str, Any]:
    """Return the next scheduling state. Pure function; no DB access."""
    if rating not in (1, 2, 3, 4):
        raise FlashcardError("rating must be 1 (Again), 2 (Hard), 3 (Good) or 4 (Easy)")
    now = now if now is not None else now_ms()
    elapsed_days = max(0.0, (now - last_review_at) / DAY_MS) if last_review_at else 0.0
    if state == "new" or stability <= 0:
        new_stability = init_stability(rating)
        new_difficulty = init_difficulty(rating)
        recall = 0.0
    else:
        recall = retrievability(elapsed_days, stability)
        new_stability = next_stability(
            difficulty=difficulty or init_difficulty(rating),
            stability=stability,
            recall=recall,
            rating=rating,
        )
        new_difficulty = next_difficulty(difficulty or init_difficulty(rating), rating)

    if rating == 1:
        days = 10.0 / 1440.0  # 10 minute relearning step
        next_state = "relearning"
    else:
        days = interval_days(new_stability, retention)
        next_state = "review"
    return {
        "state": next_state,
        "stability": round(new_stability, 6),
        "difficulty": round(new_difficulty, 6),
        "scheduled_days": round(days, 6),
        "elapsed_days": round(elapsed_days, 6),
        "retrievability": round(recall, 6),
        "due_at": int(now + days * DAY_MS),
    }


# ------------------------------------------------------------------ decks & cards


def ensure_deck(session: Session, name: str = "My Tube Knowledge", deck_id: str | None = None) -> FlashcardDeck:
    if deck_id:
        deck = session.get(FlashcardDeck, deck_id)
        if deck is None:
            raise FlashcardError("deck not found")
        return deck
    deck = session.scalar(select(FlashcardDeck).where(FlashcardDeck.name == name))
    if deck is None:
        deck = FlashcardDeck(id="deck_" + secrets.token_hex(8), name=name, created_at=now_ms(), updated_at=now_ms())
        session.add(deck)
        session.flush()
    return deck


def list_decks(session: Session) -> list[dict[str, Any]]:
    decks = session.scalars(select(FlashcardDeck).where(FlashcardDeck.archived == 0)).all()
    out = []
    for deck in decks:
        total = int(
            session.scalar(
                select(func.count(Flashcard.id)).where(
                    Flashcard.deck_id == deck.id, Flashcard.status == "active"
                )
            )
            or 0
        )
        due = int(
            session.scalar(
                select(func.count(Flashcard.id)).where(
                    Flashcard.deck_id == deck.id,
                    Flashcard.status == "active",
                    Flashcard.due_at <= now_ms(),
                )
            )
            or 0
        )
        out.append(
            {
                "id": deck.id,
                "name": deck.name,
                "description": deck.description,
                "total": total,
                "due": due,
                "created_at": deck.created_at,
            }
        )
    return out


def create_deck(session: Session, name: str, description: str | None = None) -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise FlashcardError("deck name required")
    deck = FlashcardDeck(
        id="deck_" + secrets.token_hex(8),
        name=name[:200],
        description=description,
        created_at=now_ms(),
        updated_at=now_ms(),
    )
    session.add(deck)
    session.flush()
    return {"id": deck.id, "name": deck.name, "description": deck.description}


def _card_hash(front: str, back: str) -> str:
    return hashlib.sha256(f"{front.strip()}\n{back.strip()}".encode("utf-8")).hexdigest()[:32]


def _provenance(row: KnowledgeExtract) -> dict[str, Any]:
    try:
        return json.loads(row.provenance_json or "{}")
    except json.JSONDecodeError:
        return {}


def generate_cards_from_video(
    session: Session,
    video_yt_id: str,
    *,
    deck_id: str | None = None,
    limit: int = 40,
) -> dict[str, Any]:
    video = session.scalar(select(Video).where(Video.yt_id == video_yt_id))
    if video is None:
        raise FlashcardError("video not found")
    if not list_extracts(session, video_yt_id):
        try:
            extract_knowledge(session, video_yt_id)
        except ExtractionError as exc:
            raise FlashcardError(str(exc)) from exc
    deck = ensure_deck(session, deck_id=deck_id)
    seeds = [
        row
        for row in list_extracts(session, video_yt_id, kind="flashcard_seed")
        if row["detail"] or row["content"]
    ][:limit]
    created = skipped = 0
    out: list[dict[str, Any]] = []
    for seed in seeds:
        prov = seed.get("provenance") or {}
        card_type = str(prov.get("card_type") or "question_answer")
        front = seed["content"]
        back = seed["detail"] or ""
        if not back:
            continue
        digest = _card_hash(front, back)
        existing = session.scalar(
            select(Flashcard).where(Flashcard.deck_id == deck.id, Flashcard.content_hash == digest)
        )
        if existing is not None:
            skipped += 1
            out.append(_card_out(existing))
            continue
        card = Flashcard(
            id="card_" + secrets.token_hex(10),
            deck_id=deck.id,
            video_id=video.id,
            extract_id=seed["id"],
            card_type=card_type,
            front=front,
            back=back,
            source_start_s=seed.get("start_s"),
            source_excerpt=seed.get("detail"),
            origin="auto",
            status="active",
            state="new",
            stability=0.0,
            difficulty=0.0,
            due_at=now_ms(),
            content_hash=digest,
            created_at=now_ms(),
            updated_at=now_ms(),
        )
        session.add(card)
        created += 1
        out.append(_card_out(card))
    session.flush()
    return {"deck_id": deck.id, "created": created, "skipped": skipped, "cards": out}


def create_card(
    session: Session,
    *,
    front: str,
    back: str,
    deck_id: str | None = None,
    video_yt_id: str | None = None,
    card_type: str = "manual",
) -> dict[str, Any]:
    front = (front or "").strip()
    back = (back or "").strip()
    if not front or not back:
        raise FlashcardError("front and back are required")
    deck = ensure_deck(session, deck_id=deck_id)
    video = session.scalar(select(Video).where(Video.yt_id == video_yt_id)) if video_yt_id else None
    digest = _card_hash(front, back)
    if session.scalar(
        select(Flashcard).where(Flashcard.deck_id == deck.id, Flashcard.content_hash == digest)
    ):
        raise FlashcardError("an identical card already exists in this deck")
    card = Flashcard(
        id="card_" + secrets.token_hex(10),
        deck_id=deck.id,
        video_id=video.id if video else None,
        card_type=card_type,
        front=front,
        back=back,
        origin="manual",
        status="active",
        state="new",
        due_at=now_ms(),
        content_hash=digest,
        created_at=now_ms(),
        updated_at=now_ms(),
    )
    session.add(card)
    session.flush()
    return _card_out(card)


def _card_out(card: Flashcard) -> dict[str, Any]:
    return {
        "id": card.id,
        "deck_id": card.deck_id,
        "video_id": card.video_id,
        "extract_id": card.extract_id,
        "card_type": card.card_type,
        "front": card.front,
        "back": card.back,
        "source_start_s": card.source_start_s,
        "source_excerpt": card.source_excerpt,
        "origin": card.origin,
        "status": card.status,
        "state": card.state,
        "stability": round(float(card.stability or 0), 4),
        "difficulty": round(float(card.difficulty or 0), 4),
        "due_at": card.due_at,
        "reps": card.reps,
        "lapses": card.lapses,
        "last_review_at": card.last_review_at,
    }


def due_cards(
    session: Session, *, deck_id: str | None = None, limit: int = 50, now: int | None = None
) -> list[dict[str, Any]]:
    now = now if now is not None else now_ms()
    q = select(Flashcard).where(Flashcard.status == "active", Flashcard.due_at <= now)
    if deck_id:
        q = q.where(Flashcard.deck_id == deck_id)
    rows = session.scalars(q.order_by(Flashcard.due_at, Flashcard.created_at).limit(limit)).all()
    out = []
    for card in rows:
        data = _card_out(card)
        video = session.get(Video, card.video_id) if card.video_id else None
        data["video_title"] = video.title if video else None
        out.append(data)
    return out


def review_card(
    session: Session, card_id: str, rating: int, *, now: int | None = None
) -> dict[str, Any]:
    card = session.get(Flashcard, card_id)
    if card is None or card.status != "active":
        raise FlashcardError("card not found")
    now = now if now is not None else now_ms()
    result = schedule(
        state=card.state,
        stability=float(card.stability or 0),
        difficulty=float(card.difficulty or 0),
        rating=rating,
        last_review_at=card.last_review_at,
        now=now,
    )
    before = card.state
    card.state = result["state"]
    card.stability = result["stability"]
    card.difficulty = result["difficulty"]
    card.due_at = result["due_at"]
    card.reps = int(card.reps or 0) + 1
    if rating == 1:
        card.lapses = int(card.lapses or 0) + 1
    card.last_review_at = now
    card.updated_at = now
    session.add(
        ReviewLog(
            card_id=card.id,
            rating=rating,
            state_before=before,
            state_after=result["state"],
            stability=result["stability"],
            difficulty=result["difficulty"],
            scheduled_days=result["scheduled_days"],
            elapsed_days=result["elapsed_days"],
            retrievability=result["retrievability"],
            due_at=result["due_at"],
            reviewed_at=now,
        )
    )
    session.flush()
    return {"card": _card_out(card), "review": result}


def update_card(
    session: Session,
    card_id: str,
    *,
    front: str | None = None,
    back: str | None = None,
    card_type: str | None = None,
) -> dict[str, Any]:
    card = session.get(Flashcard, card_id)
    if card is None:
        raise FlashcardError("card not found")
    if front is not None:
        card.front = front.strip() or card.front
    if back is not None:
        card.back = back.strip() or card.back
    if card_type is not None:
        card.card_type = card_type
    card.content_hash = _card_hash(card.front, card.back)
    card.updated_at = now_ms()
    session.flush()
    return _card_out(card)


def delete_card(session: Session, card_id: str) -> dict[str, Any]:
    card = session.get(Flashcard, card_id)
    if card is None:
        raise FlashcardError("card not found")
    card.status = "deleted"
    card.updated_at = now_ms()
    session.flush()
    return {"id": card.id, "status": card.status}


def restore_card(session: Session, card_id: str) -> dict[str, Any]:
    card = session.get(Flashcard, card_id)
    if card is None:
        raise FlashcardError("card not found")
    card.status = "active"
    card.updated_at = now_ms()
    session.flush()
    return _card_out(card)


def review_stats(session: Session, *, days: int = 30) -> dict[str, Any]:
    now = now_ms()
    since = now - days * DAY_MS
    total = int(session.scalar(select(func.count(Flashcard.id)).where(Flashcard.status == "active")) or 0)
    due = int(
        session.scalar(
            select(func.count(Flashcard.id)).where(
                Flashcard.status == "active", Flashcard.due_at <= now
            )
        )
        or 0
    )
    new = int(
        session.scalar(
            select(func.count(Flashcard.id)).where(
                Flashcard.status == "active", Flashcard.reps == 0
            )
        )
        or 0
    )
    logs = session.scalars(select(ReviewLog).where(ReviewLog.reviewed_at >= since)).all()
    ratings = {1: 0, 2: 0, 3: 0, 4: 0}
    daily: dict[str, int] = {}
    for log in logs:
        ratings[int(log.rating)] = ratings.get(int(log.rating), 0) + 1
        day = datetime.fromtimestamp(log.reviewed_at / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        daily[day] = daily.get(day, 0) + 1
    reviewed = sum(ratings.values())
    retention = (ratings[3] + ratings[4]) / reviewed if reviewed else None
    all_days = sorted(
        {
            datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            for row in session.execute(select(ReviewLog.reviewed_at)).all()
        }
    )
    streak = 0
    if all_days:
        today = datetime.now(timezone.utc).date()
        try:
            last = datetime.strptime(all_days[-1], "%Y-%m-%d").date()
        except ValueError:
            last = None
        if last and (today - last).days <= 1:
            streak = 1
            for i in range(len(all_days) - 2, -1, -1):
                prev = datetime.strptime(all_days[i], "%Y-%m-%d").date()
                cur = datetime.strptime(all_days[i + 1], "%Y-%m-%d").date()
                if (cur - prev).days == 1:
                    streak += 1
                else:
                    break
    return {
        "total_cards": total,
        "due_now": due,
        "new_cards": new,
        "reviews_window": reviewed,
        "window_days": days,
        "rating_counts": {str(k): v for k, v in ratings.items()},
        "retention_estimate": round(retention, 3) if retention is not None else None,
        "daily_reviews": dict(sorted(daily.items())),
        "streak_days": streak,
        "note": "Retention is the share of Good/Easy reviews in the window, not a mastery claim.",
    }


def export_deck(session: Session, deck_id: str, *, include_history: bool = True) -> dict[str, Any]:
    deck = session.get(FlashcardDeck, deck_id)
    if deck is None:
        raise FlashcardError("deck not found")
    cards = session.scalars(select(Flashcard).where(Flashcard.deck_id == deck_id)).all()
    payload: dict[str, Any] = {
        "format": "mytube-flashcards-v1",
        "deck": {"name": deck.name, "description": deck.description},
        "cards": [
            {
                **_card_out(card),
                "content_hash": card.content_hash,
            }
            for card in cards
        ],
    }
    if include_history:
        ids = [card.id for card in cards]
        logs = session.scalars(select(ReviewLog).where(ReviewLog.card_id.in_(ids))).all() if ids else []
        payload["reviews"] = [
            {
                "card_id": log.card_id,
                "rating": log.rating,
                "reviewed_at": log.reviewed_at,
                "scheduled_days": log.scheduled_days,
                "state_after": log.state_after,
            }
            for log in logs
        ]
    return payload


def import_deck(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("format") != "mytube-flashcards-v1":
        raise FlashcardError("unsupported flashcard export format")
    deck_meta = payload.get("deck") or {}
    deck = ensure_deck(session, name=str(deck_meta.get("name") or "Imported deck"))
    created = skipped = 0
    for item in payload.get("cards") or []:
        if not isinstance(item, dict):
            continue
        front = str(item.get("front") or "").strip()
        back = str(item.get("back") or "").strip()
        if not front or not back:
            continue
        digest = _card_hash(front, back)
        if session.scalar(
            select(Flashcard).where(Flashcard.deck_id == deck.id, Flashcard.content_hash == digest)
        ):
            skipped += 1
            continue
        stability = max(0.0, min(3650.0, float(item.get("stability") or 0)))
        difficulty = max(0.0, min(10.0, float(item.get("difficulty") or 0)))
        session.add(
            Flashcard(
                id="card_" + secrets.token_hex(10),
                deck_id=deck.id,
                card_type=str(item.get("card_type") or "imported")[:24],
                front=front,
                back=back,
                source_start_s=item.get("source_start_s"),
                source_excerpt=item.get("source_excerpt"),
                origin="imported",
                status="active",
                state=str(item.get("state") or "new")[:12],
                stability=stability,
                difficulty=difficulty,
                due_at=int(item.get("due_at") or now_ms()),
                reps=int(item.get("reps") or 0),
                lapses=int(item.get("lapses") or 0),
                last_review_at=item.get("last_review_at"),
                content_hash=digest,
                created_at=now_ms(),
                updated_at=now_ms(),
            )
        )
        created += 1
    session.flush()
    return {"deck_id": deck.id, "created": created, "skipped": skipped}
