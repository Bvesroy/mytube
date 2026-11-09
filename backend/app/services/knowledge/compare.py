"""Cross-video intelligence (Feature 4).

Compares stored knowledge from two or more library videos. Every statement in the
result is either:

- `directly_stated` - an excerpt from a stored transcript/extract, with video and
  timestamp;
- `inferred` - a deterministic relationship (shared concept, textual divergence,
  coverage gap) with the rule that produced it;
- `synthesis` - only ever produced by an explicitly requested local LLM, and kept
  separate.

The service never invents a claim, quote or timestamp. Contradiction detection is a
heuristic and is labelled as such.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Channel,
    Concept,
    KnowledgeExtract,
    ResearchDocument,
    Video,
    VideoComparison,
    VideoConcept,
    now_ms,
)
from app.services.ai.embeddings import configured_engine, cosine
from app.services.knowledge.extraction import ExtractionError, extract_knowledge, list_extracts
from app.services.knowledge.text import difficulty_score, level_for_score

NEGATIONS = (" not ", " never ", " no ", " cannot ", " doesn't ", " isn't ", " aren't ", " unlike ", " without ")


class ComparisonError(ValueError):
    pass


def _video(session: Session, yt_id: str) -> Video:
    video = session.scalar(select(Video).where(Video.yt_id == yt_id))
    if video is None:
        raise ComparisonError(f"video not found: {yt_id}")
    return video


def _video_packet(session: Session, video: Video) -> dict[str, Any]:
    if not list_extracts(session, video.yt_id):
        try:
            extract_knowledge(session, video.yt_id)
        except ExtractionError:
            pass
    rows = list_extracts(session, video.yt_id)
    channel = session.get(Channel, video.channel_id) if video.channel_id else None
    concepts = [
        {"name": concept.name, "slug": concept.slug, "confidence": round(float(link.confidence or 0), 3)}
        for concept, link in session.execute(
            select(Concept, VideoConcept)
            .join(VideoConcept, VideoConcept.concept_id == Concept.id)
            .where(VideoConcept.video_id == video.id)
            .order_by(VideoConcept.weight.desc())
        ).all()
    ]
    definitions = [
        {
            "term": row["content"],
            "text": row["detail"] or "",
            "start_s": row["start_s"],
            "confidence": row["confidence"],
        }
        for row in rows
        if row["kind"] == "definition"
    ]
    summaries = [row for row in rows if row["kind"].startswith("summary_")]
    return {
        "video_id": video.yt_id,
        "title": video.title,
        "channel": channel.name if channel else "",
        "duration_s": video.duration_s,
        "concepts": concepts,
        "definitions": definitions,
        "summary": summaries[0]["content"] if summaries else "",
        "difficulty": round(difficulty_score(video.title, (summaries[0]["content"] if summaries else ""), video.duration_s), 3),
        "level": level_for_score(difficulty_score(video.title, (summaries[0]["content"] if summaries else ""), video.duration_s)),
        "extract_count": len(rows),
        "extracts": rows,
    }


def _evidence(packet: dict[str, Any], *, extract: dict[str, Any] | None = None, note: str = "") -> dict[str, Any]:
    return {
        "video_id": packet["video_id"],
        "title": packet["title"],
        "channel": packet["channel"],
        "timestamp_s": (extract or {}).get("start_s"),
        "excerpt": ((extract or {}).get("detail") or (extract or {}).get("content") or "")[:400],
        "extract_id": (extract or {}).get("id"),
        "note": note,
        "claim_type": "directly_stated",
    }


def _definition_similarity(a: str, b: str) -> float:
    engine = configured_engine()
    return cosine(engine.embed(a), engine.embed(b))


def compare_videos(
    session: Session,
    video_yt_ids: list[str],
    *,
    objective: str | None = None,
    persist: bool = True,
    title: str | None = None,
) -> dict[str, Any]:
    ids = list(dict.fromkeys(video_yt_ids))
    if len(ids) < 2:
        raise ComparisonError("select at least two videos to compare")
    packets = [_video_packet(session, _video(session, yt_id)) for yt_id in ids]

    concept_sets = {p["video_id"]: {c["name"].lower(): c for c in p["concepts"]} for p in packets}
    all_names = set().union(*[set(s) for s in concept_sets.values()])
    common = []
    unique: dict[str, list[dict[str, Any]]] = {p["video_id"]: [] for p in packets}
    missing: dict[str, list[dict[str, Any]]] = {p["video_id"]: [] for p in packets}
    for name in sorted(all_names):
        present = [p["video_id"] for p in packets if name in concept_sets[p["video_id"]]]
        if len(present) == len(packets):
            common.append(
                {
                    "concept": concept_sets[present[0]][name]["name"],
                    "videos": present,
                    "confidence": round(
                        sum(concept_sets[vid][name]["confidence"] for vid in present) / len(present), 3
                    ),
                    "claim_type": "inferred",
                    "rule": "concept linked to every selected video by extraction",
                }
            )
        else:
            for packet in packets:
                if packet["video_id"] in present:
                    unique[packet["video_id"]].append(concept_sets[packet["video_id"]][name])
                else:
                    missing[packet["video_id"]].append(
                        {
                            "concept": concept_sets[present[0]][name]["name"],
                            "covered_by": present,
                            "claim_type": "inferred",
                            "rule": "concept absent from this video's stored extraction",
                        }
                    )

    # Definitions: agreement vs divergence, always with both excerpts.
    agreements: list[dict[str, Any]] = []
    contradictions: list[dict[str, Any]] = []
    approaches: list[dict[str, Any]] = []
    terms = {p["video_id"]: {d["term"].lower(): d for d in p["definitions"]} for p in packets}
    shared_terms = set.intersection(*[set(t) for t in terms.values()]) if terms else set()
    for term in sorted(shared_terms):
        defs = [(p, terms[p["video_id"]][term]) for p in packets]
        base = defs[0][1]["text"]
        sims = [(p, d, _definition_similarity(base, d["text"])) for p, d in defs[1:]]
        avg = sum(s for _p, _d, s in sims) / max(1, len(sims))
        evidence = [_evidence(p, extract=d, note=f"definition of {term}") for p, d in defs]
        if avg >= 0.55:
            agreements.append(
                {
                    "topic": term,
                    "summary": "The sources define this term in similar terms.",
                    "similarity": round(avg, 3),
                    "evidence": evidence,
                    "claim_type": "inferred",
                    "rule": "embedding cosine of stored definitions >= 0.55",
                }
            )
        else:
            negative = [
                p for p, d in defs if any(token in f" {d['text'].lower()} " for token in NEGATIONS)
            ]
            contradictions.append(
                {
                    "topic": term,
                    "summary": (
                        "Possible contradiction: the definitions diverge"
                        + (" and at least one uses a negation." if negative else ".")
                    ),
                    "similarity": round(avg, 3),
                    "evidence": evidence,
                    "claim_type": "inferred",
                    "rule": "embedding cosine of stored definitions < 0.55 (heuristic, not a fact check)",
                }
            )
    for packet in packets:
        code = [r for r in packet["extracts"] if r["kind"] == "code"]
        formulas = [r for r in packet["extracts"] if r["kind"] == "formula"]
        examples = [r for r in packet["extracts"] if r["kind"] == "example"]
        approaches.append(
            {
                "video_id": packet["video_id"],
                "title": packet["title"],
                "has_code": bool(code),
                "has_formula": bool(formulas),
                "example_count": len(examples),
                "definition_count": len(packet["definitions"]),
                "difficulty": packet["difficulty"],
                "level": packet["level"],
                "claim_type": "inferred",
                "rule": "counts of stored extraction kinds",
            }
        )

    # Complementary videos from the same subscription library.
    common_slugs = {
        concept_sets[packets[0]["video_id"]][name]["slug"]
        for name in concept_sets[packets[0]["video_id"]]
        if all(name in concept_sets[p["video_id"]] for p in packets)
    }
    complementary: list[dict[str, Any]] = []
    if common_slugs:
        selected = set(ids)
        rows = session.execute(
            select(Video, Concept, VideoConcept)
            .join(VideoConcept, VideoConcept.video_id == Video.id)
            .join(Concept, Concept.id == VideoConcept.concept_id)
            .where(Concept.slug.in_(list(common_slugs)), Video.is_available == 1)
        ).all()
        agg: dict[int, dict[str, Any]] = {}
        for video, concept, link in rows:
            if video.yt_id in selected:
                continue
            entry = agg.setdefault(
                video.id,
                {"video_id": video.yt_id, "title": video.title, "shared": [], "score": 0.0},
            )
            entry["shared"].append(concept.name)
            entry["score"] += float(link.confidence or 0)
        complementary = sorted(agg.values(), key=lambda e: -e["score"])[:8]
        for entry in complementary:
            entry["score"] = round(entry["score"], 3)
            entry["claim_type"] = "inferred"
            entry["rule"] = "shares a concept with every selected video"

    result = {
        "objective": objective,
        "videos": [
            {
                "video_id": p["video_id"],
                "title": p["title"],
                "channel": p["channel"],
                "level": p["level"],
                "difficulty": p["difficulty"],
                "concept_count": len(p["concepts"]),
                "summary": p["summary"],
            }
            for p in packets
        ],
        "common_explanations": common,
        "unique_concepts": unique,
        "agreements": agreements,
        "possible_contradictions": contradictions,
        "different_approaches": approaches,
        "missing_explanations": missing,
        "complementary_videos": complementary,
        "claim_types": {
            "directly_stated": "Excerpt stored from the video's transcript/description.",
            "inferred": "Deterministic relationship computed from stored extraction; rule shown.",
            "synthesis": "Only present when an explicitly requested local LLM produced it.",
            "unverified": "Not used; the service does not emit unverified claims.",
        },
        "only_subscription_library": True,
    }

    if persist:
        doc_id = "rd_" + secrets.token_hex(10)
        doc_title = title or f"Comparison: " + " vs ".join(p["title"][:60] for p in packets[:3])
        doc = ResearchDocument(
            id=doc_id,
            kind="comparison",
            title=doc_title,
            body_json=json.dumps(result, sort_keys=True),
            created_at=now_ms(),
            updated_at=now_ms(),
        )
        session.add(doc)
        session.flush()
        session.add(
            VideoComparison(
                id="vc_" + secrets.token_hex(10),
                document_id=doc_id,
                video_ids_json=json.dumps(ids),
                objective=objective,
            )
        )
        session.flush()
        result["document_id"] = doc_id
        result["title"] = doc_title
    return result


def list_documents(session: Session, *, kind: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    q = select(ResearchDocument).where(ResearchDocument.archived == 0)
    if kind:
        q = q.where(ResearchDocument.kind == kind)
    rows = session.scalars(q.order_by(ResearchDocument.updated_at.desc()).limit(limit)).all()
    return [
        {
            "id": row.id,
            "kind": row.kind,
            "title": row.title,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
        for row in rows
    ]


def get_document(session: Session, document_id: str) -> dict[str, Any] | None:
    row = session.get(ResearchDocument, document_id)
    if row is None:
        return None
    try:
        body = json.loads(row.body_json)
    except json.JSONDecodeError:
        body = {}
    return {
        "id": row.id,
        "kind": row.kind,
        "title": row.title,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "body": body,
    }


def save_answer_document(
    session: Session, *, question: str, answer: str, citations: list[dict[str, Any]]
) -> dict[str, Any]:
    doc = ResearchDocument(
        id="rd_" + secrets.token_hex(10),
        kind="answer",
        title=question[:280],
        body_json=json.dumps(
            {"question": question, "answer": answer, "citations": citations}, sort_keys=True
        ),
        created_at=now_ms(),
        updated_at=now_ms(),
    )
    session.add(doc)
    session.flush()
    return {"id": doc.id, "kind": doc.kind, "title": doc.title}
