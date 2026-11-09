"""Automatic video-to-knowledge extraction (Feature 3).

Deterministic first: summaries, concepts, definitions, terminology, examples,
formulas, code, timestamped highlights, prerequisites, research questions and
flashcard seeds are derived from the stored transcript/description with explicit
source offsets. An optional local LLM can add a summary and definitions, but every
LLM item is validated against the source text before it is stored; unvalidated
output is dropped.

Rules:
- Every item keeps its video id and the timestamp (or `None` when the transcript has
  no trustworthy timing).
- Manually edited rows are never overwritten. Rejected rows stay rejected.
- Regeneration only removes non-edited, non-rejected rows it is replacing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models import (
    Channel,
    Concept,
    ConceptRelation,
    KnowledgeExtract,
    ProcessingProvenance,
    Video,
    VideoConcept,
    now_ms,
)
from app.services.ai.provider import AIUnavailable, LocalLLM
from app.services.knowledge.text import (
    Sentence,
    best_sentences,
    content_hash,
    difficulty_score,
    keywords,
    level_for_score,
    normalize_text,
    sentences_with_timing,
    slugify,
    split_sentences,
    title_terms,
    words,
)
from app.services.knowledge.transcripts import get_transcript

MODEL_ID = "deterministic-v1"

_DEF_RE = re.compile(
    r"^(?P<subject>[A-Za-z0-9][A-Za-z0-9\s\-+#/.()]{1,60}?)\s+"
    r"(?:is|are|refers to|means|describes|denotes|represents)\s+(?P<body>.{20,400})$",
    re.IGNORECASE,
)
_PREREQ_RE = re.compile(
    r"(?:prerequisites?|before you (?:learn|start|begin|dive)|you (?:should|need to) "
    r"(?:know|understand)|requires? (?:knowledge|understanding)|assumes?|familiar with|basics of)"
    r"\s*(?:of|with|about|:)?\s*(?P<phrase>[A-Za-z0-9][^.!?]{2,80})",
    re.IGNORECASE,
)
_EXAMPLE_RE = re.compile(r"\b(for example|for instance|e\.g\.|such as|consider|imagine|let's say)\b", re.I)
_FORMULA_RE = re.compile(r"(?:[A-Za-z][A-Za-z0-9_]*\s*=\s*[^.\n]{2,60})|(?:[^.\n]{0,40}[∑∫√≈≤≥πΔλμ][^.\n]{0,40})")
_CODE_FENCE_RE = re.compile(r"```(?:[A-Za-z0-9_+-]*)\n(.*?)```", re.S)
_CODE_LINE_RE = re.compile(
    r"^\s*(?:def |class |function |const |let |var |import |from |#include|return |if \(|for \(|while \(|console\.log|print\(|\}\s*$)",
    re.M,
)
_ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,7}\b")
_QUOTED_RE = re.compile(r"[\"“]([A-Za-z][A-Za-z0-9 \-+#.]{2,48})[\"”]")
_PRONOUN_SUBJECT = {"it", "this", "that", "these", "those", "there", "here", "which", "they", "he", "she"}
_CONCEPT_STOPWORDS = {
    "finally", "next", "then", "unlike", "fully", "learns", "detects", "extracting",
    "information", "field", "bank", "seen", "given", "taken", "called", "using",
    "used", "comes", "goes", "means", "shows", "looks", "works", "makes", "gets",
    "wants", "needs", "knows", "says", "said", "lets", "puts", "keeps", "finds",
}


class ExtractionError(ValueError):
    pass


@dataclass
class Candidate:
    kind: str
    content: str
    detail: str | None = None
    start_s: float | None = None
    end_s: float | None = None
    confidence: float = 0.5
    source: str = "deterministic"
    model_id: str = MODEL_ID
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def digest(self) -> str:
        return content_hash(f"{self.kind}:{normalize_text(self.content)}:{normalize_text(self.detail or '')}")


# ------------------------------------------------------------------ extraction building blocks


def _is_concept_name(name: str) -> bool:
    name = name.strip(" .,:;()[]{}\"'")
    low = name.lower()
    if not (2 < len(name) <= 60):
        return False
    if low in _CONCEPT_STOPWORDS:
        return False
    if low in _PRONOUN_SUBJECT:
        return False
    toks = words(name)
    if not toks:
        return False
    if toks[0] in {"the", "a", "an", "and", "but", "so", "then", "now", "here", "there"}:
        return False
    if all(t in {"the", "a", "an", "of", "in", "on", "to", "for", "and", "with"} for t in toks):
        return False
    return any(len(t) > 2 for t in toks)


def concept_candidates(text: str, *, limit: int = 16) -> list[tuple[str, float]]:
    """Concept names present in the source, with a relative salience score."""
    scores: dict[str, float] = {}
    lower_blob = text.lower()
    for term in keywords(text, limit=limit * 2):
        if len(term) > 2:
            scores[term] = scores.get(term, 0.0) + 1.0
    for phrase in re.findall(r"\b(?:[A-Z][a-zA-Z0-9+#.\-]+(?:\s+(?:of|for|in|and)\s+)?){1,4}", text):
        phrase = phrase.strip()
        if _is_concept_name(phrase) and phrase.lower() not in {w for w in words(phrase) if len(w) < 3}:
            if len(phrase.split()) <= 4 and len(phrase) > 3:
                scores[phrase] = scores.get(phrase, 0.0) + 1.4
    for acronym in _ACRONYM_RE.findall(text):
        if acronym.lower() not in {"i", "a", "ok", "ai", "us", "uk", "id", "tv"} or acronym in {"AI", "ML", "GPU", "CPU", "API"}:
            scores[acronym] = scores.get(acronym, 0.0) + 1.2
    for quoted in _QUOTED_RE.findall(text):
        if _is_concept_name(quoted):
            scores[quoted] = scores.get(quoted, 0.0) + 1.1
    # Definitions contribute their subject strongly.
    for sent in split_sentences(text):
        match = _DEF_RE.match(sent.text)
        if match:
            subject = _clean_subject(match.group("subject"))
            if subject and subject.lower() in lower_blob:
                scores[subject] = scores.get(subject, 0.0) + 2.0
    cleaned: dict[str, float] = {}
    for name, value in scores.items():
        if not _is_concept_name(name):
            continue
        display = _display_name(name)
        key = display.lower()
        cleaned[key] = max(cleaned.get(key, 0.0), value)
    # Drop a bare token when the same word is part of a stronger multi-word concept.
    multiword_tokens = {tok for key in cleaned if " " in key for tok in key.split()}
    for key in list(cleaned):
        if " " not in key and key in multiword_tokens:
            del cleaned[key]
    ranked = sorted(cleaned.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [(_display_name(k), v) for k, v in ranked]


def _clean_subject(subject: str) -> str:
    subject = re.sub(r"^(?:the|a|an)\s+", "", subject.strip(), flags=re.I)
    subject = re.sub(r"\s+(?:is|are)$", "", subject, flags=re.I).strip(" .,:;")
    return subject if _is_concept_name(subject) else ""


def _display_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip(" .,:;")
    if name.islower():
        # Keep known acronyms, title-case plain lowercase phrases.
        small = {"of", "for", "and", "in", "on", "to", "with"}
        parts = name.split()
        return " ".join(p if p in small and i else p.capitalize() for i, p in enumerate(parts))
    return name


def find_definitions(sentences: list[Sentence]) -> list[tuple[str, Sentence]]:
    out: list[tuple[str, Sentence]] = []
    seen: set[str] = set()
    for sent in sentences:
        match = _DEF_RE.match(sent.text)
        if not match:
            continue
        subject = _clean_subject(match.group("subject"))
        if not subject:
            continue
        key = subject.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append((subject, sent))
    return out


def find_prerequisites(sentences: list[Sentence]) -> list[tuple[str, Sentence]]:
    out: list[tuple[str, Sentence]] = []
    seen: set[str] = set()
    for sent in sentences:
        for match in _PREREQ_RE.finditer(sent.text):
            phrase = match.group("phrase").strip(" .,:;")
            phrase = re.split(r"\b(?:and|but|so|which|that)\b", phrase)[0].strip(" .,:;")
            if not _is_concept_name(phrase):
                continue
            key = phrase.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append((phrase, sent))
    return out


def _code_blocks(text: str) -> list[str]:
    blocks = [normalize_text(b) for b in _CODE_FENCE_RE.findall(text) if normalize_text(b)]
    if blocks:
        return blocks[:6]
    lines = text.splitlines()
    chunks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if _CODE_LINE_RE.search(line) or (line.startswith(("  ", "\t")) and current):
            current.append(line)
        elif current:
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)
    out = []
    for chunk in chunks:
        body = "\n".join(chunk).strip()
        if body and len(body) > 15 and any(tok in body for tok in ("(", "=", "{", "def ", "class ")):
            out.append(body)
    return out[:6]


def _formulas(text: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for match in _FORMULA_RE.finditer(text):
        value = match.group(0).strip()
        if len(value) < 5 or value.lower() in seen:
            continue
        if not any(ch in value for ch in "=≈≤≥∑∫√πΔλμ"):
            continue
        seen.add(value.lower())
        out.append(value)
    return out[:8]


def _flashcard_seeds(
    definitions: list[tuple[str, Sentence]],
    concepts: list[tuple[str, float]],
    formulas: list[str],
    examples: list[tuple[str, Sentence]],
    timing_ok: bool,
) -> list[Candidate]:
    seeds: list[Candidate] = []
    for subject, sent in definitions[:6]:
        back = sent.text
        start = sent.start_s if timing_ok else None
        seeds.append(
            Candidate(
                kind="flashcard_seed",
                content=f"Define: {subject}",
                detail=back,
                start_s=start,
                confidence=0.7,
                provenance={"generated": True, "card_type": "definition", "concept": subject},
            )
        )
        # Cloze card from the definition sentence.
        cloze = re.sub(re.escape(subject), "[…]", sent.text, count=1, flags=re.I)
        if "[…]" in cloze:
            seeds.append(
                Candidate(
                    kind="flashcard_seed",
                    content=cloze,
                    detail=subject,
                    start_s=start,
                    confidence=0.6,
                    provenance={"generated": True, "card_type": "cloze", "concept": subject},
                )
            )
    for concept, score in concepts[:4]:
        sentence = _best_sentence_about(concept, examples) or None
        if sentence:
            seeds.append(
                Candidate(
                    kind="flashcard_seed",
                    content=f"What does this video say about {concept}?",
                    detail=sentence.text,
                    start_s=sentence.start_s if timing_ok else None,
                    confidence=0.45,
                    provenance={"generated": True, "card_type": "question_answer", "concept": concept},
                )
            )
    for formula in formulas[:3]:
        seeds.append(
            Candidate(
                kind="flashcard_seed",
                content="Recall the formula shown in the source.",
                detail=formula,
                confidence=0.55,
                provenance={"generated": True, "card_type": "formula"},
            )
        )
    return seeds


def _best_sentence_about(term: str, pool: list[tuple[str, Sentence]]) -> Sentence | None:
    low = term.lower()
    for _kind, sent in pool:
        if low in sent.text.lower():
            return sent
    return None


def _validate_llm_text(candidate_text: str, source: str, *, threshold: float = 0.5) -> bool:
    """An LLM item is only kept when its content words largely occur in the source."""
    src = set(words(source))
    if not src:
        return False
    toks = [w for w in words(candidate_text) if len(w) > 2]
    if not toks:
        return False
    overlap = sum(1 for w in toks if w in src) / len(toks)
    return overlap >= threshold


def _llm_candidates(video: Video, source: str, llm: LocalLLM) -> list[Candidate]:
    prompt = (
        "Extract study notes from the transcript below. Answer with JSON only, shaped as "
        '{"summary": "...", "definitions": [{"term": "...", "definition": "..."}]}. '
        "Use only facts stated in the transcript. Do not add outside knowledge. "
        "Keep the summary under 120 words.\n\nTRANSCRIPT:\n" + source[:12000]
    )
    try:
        raw = llm.generate(prompt, max_tokens=600, temperature=0.0)
    except AIUnavailable:
        return []
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return []
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    out: list[Candidate] = []
    summary = str(payload.get("summary") or "").strip()
    if summary and _validate_llm_text(summary, source, threshold=0.45):
        out.append(
            Candidate(
                kind="summary_short",
                content=summary,
                confidence=0.55,
                source="llm",
                model_id=llm.model or "local",
                provenance={"generated": True, "validated_against_source": True},
            )
        )
    for item in payload.get("definitions") or []:
        if not isinstance(item, dict):
            continue
        term = str(item.get("term") or "").strip()
        definition = str(item.get("definition") or "").strip()
        if not term or not definition:
            continue
        if term.lower() not in source.lower():
            continue
        if not _validate_llm_text(definition, source, threshold=0.4):
            continue
        out.append(
            Candidate(
                kind="definition",
                content=term,
                detail=definition,
                confidence=0.5,
                source="llm",
                model_id=llm.model or "local",
                provenance={"generated": True, "validated_against_source": True},
            )
        )
    return out


# ------------------------------------------------------------------ main entrypoints


def build_candidates(
    video: Video,
    *,
    transcript_text: str,
    transcript_segments: list[tuple[float | None, float | None, str]],
    timing_ok: bool,
    allow_llm: bool = False,
    llm: LocalLLM | None = None,
) -> list[Candidate]:
    source = normalize_text(transcript_text or video.description or "")
    if not source:
        return []
    if transcript_segments:
        sentences = sentences_with_timing(source, transcript_segments)
    else:
        sentences = split_sentences(source)
        if not timing_ok:
            sentences = [Sentence(s.text, None, None) for s in sentences]
    if not sentences:
        sentences = [Sentence(source[:400])]

    concepts = concept_candidates(source)
    definitions = find_definitions(sentences)
    prereqs = find_prerequisites(sentences)
    examples = [("example", s) for s in sentences if _EXAMPLE_RE.search(s.text)]
    formulas = _formulas(source)
    code = _code_blocks(source)

    cands: list[Candidate] = []
    short = best_sentences(sentences, count=3, max_chars=420)
    detailed = best_sentences(sentences, count=8, max_chars=1600)
    if short:
        cands.append(
            Candidate(
                kind="summary_short",
                content=" ".join(s.text for s in short)[:480],
                confidence=0.55,
                provenance={"method": "extractive_lead", "generated": True},
            )
        )
    if detailed:
        cands.append(
            Candidate(
                kind="summary_detailed",
                content=" ".join(s.text for s in detailed)[:1800],
                confidence=0.5,
                provenance={"method": "extractive_ranked", "generated": True},
            )
        )

    for name, score in concepts:
        cands.append(
            Candidate(
                kind="concept",
                content=name,
                confidence=min(0.85, 0.4 + score / 6.0),
                provenance={"method": "keyword_salience", "score": round(score, 3)},
            )
        )

    for subject, sent in definitions[:10]:
        cands.append(
            Candidate(
                kind="definition",
                content=subject,
                detail=sent.text,
                start_s=sent.start_s if timing_ok else None,
                end_s=sent.end_s if timing_ok else None,
                confidence=0.7,
                provenance={"method": "definition_pattern"},
            )
        )

    for acronym in list(dict.fromkeys(_ACRONYM_RE.findall(source)))[:10]:
        if _is_concept_name(acronym):
            cands.append(
                Candidate(
                    kind="term",
                    content=acronym,
                    confidence=0.45,
                    provenance={"method": "acronym"},
                )
            )

    for _kind, sent in examples[:6]:
        cands.append(
            Candidate(
                kind="example",
                content=sent.text,
                start_s=sent.start_s if timing_ok else None,
                end_s=sent.end_s if timing_ok else None,
                confidence=0.55,
                provenance={"method": "example_marker"},
            )
        )

    for formula in formulas:
        cands.append(
            Candidate(
                kind="formula",
                content=formula,
                confidence=0.6,
                provenance={"method": "math_pattern"},
            )
        )

    for block in code:
        cands.append(
            Candidate(
                kind="code",
                content=block[:800],
                confidence=0.6,
                provenance={"method": "code_block"},
            )
        )

    if timing_ok:
        for sent in best_sentences([s for s in sentences if s.start_s is not None], count=8, max_chars=1000):
            cands.append(
                Candidate(
                    kind="timestamp",
                    content=sent.text,
                    start_s=sent.start_s,
                    end_s=sent.end_s,
                    confidence=0.5,
                    provenance={"method": "salient_sentence"},
                )
            )

    for phrase, sent in prereqs[:8]:
        cands.append(
            Candidate(
                kind="prerequisite",
                content=phrase,
                detail=sent.text,
                start_s=sent.start_s if timing_ok else None,
                confidence=0.55,
                provenance={"method": "prerequisite_pattern"},
            )
        )

    for concept, _score in concepts[:4]:
        cands.append(
            Candidate(
                kind="question",
                content=f"How does {concept} work, according to this video?",
                confidence=0.3,
                provenance={
                    "method": "generated_question",
                    "generated": True,
                    "note": "generated prompt, not a factual claim",
                },
            )
        )

    cands.extend(_flashcard_seeds(definitions, concepts, formulas, examples, timing_ok))

    if allow_llm and llm is not None and llm.available:
        cands.extend(_llm_candidates(video, source, llm))

    # Deduplicate by (kind, digest) while keeping the highest confidence.
    deduped: dict[tuple[str, str], Candidate] = {}
    for cand in cands:
        cand.content = normalize_text(cand.content)
        if not cand.content:
            continue
        key = (cand.kind, cand.digest)
        prior = deduped.get(key)
        if prior is None or cand.confidence > prior.confidence:
            deduped[key] = cand
    return list(deduped.values())


def extract_knowledge(
    session: Session,
    video_yt_id: str,
    *,
    force: bool = False,
    allow_llm: bool = False,
    llm: LocalLLM | None = None,
) -> dict[str, Any]:
    video = session.scalar(select(Video).where(Video.yt_id == video_yt_id))
    if video is None:
        raise ExtractionError(f"video not found: {video_yt_id}")
    view = get_transcript(session, video_yt_id)
    if view is not None:
        timing_ok = view.timing_available
        segs = [(s.start_s, s.end_s, s.text) for s in view.segments]
        transcript_text = view.transcript.text
        source_kind = "transcript"
    else:
        timing_ok = False
        segs = []
        transcript_text = video.description or ""
        source_kind = "description"
    if not transcript_text.strip():
        raise ExtractionError("no transcript or description available for extraction")

    if not force:
        existing_any = session.scalar(
            select(KnowledgeExtract.id).where(
                KnowledgeExtract.video_id == video.id, KnowledgeExtract.rejected == 0
            ).limit(1)
        )
        if existing_any is not None:
            # Still safe to run idempotently; candidate hashes decide what changes.
            pass

    cands = build_candidates(
        video,
        transcript_text=transcript_text,
        transcript_segments=segs,
        timing_ok=timing_ok,
        allow_llm=allow_llm,
        llm=llm,
    )
    if not cands:
        raise ExtractionError("no knowledge could be extracted from the available text")

    existing = list(
        session.scalars(select(KnowledgeExtract).where(KnowledgeExtract.video_id == video.id)).all()
    )
    by_key = {(row.kind, row.content_hash): row for row in existing}
    wanted_keys = set()
    created = updated = skipped = 0
    for cand in cands:
        key = (cand.kind, cand.digest)
        wanted_keys.add(key)
        row = by_key.get(key)
        if row is not None:
            if int(row.edited or 0) or int(row.rejected or 0):
                skipped += 1
                continue
            row.confidence = cand.confidence
            row.detail = cand.detail
            row.start_s = cand.start_s
            row.end_s = cand.end_s
            row.source = cand.source
            row.model_id = cand.model_id
            row.provenance_json = json.dumps(cand.provenance, sort_keys=True)
            row.updated_at = now_ms()
            updated += 1
            continue
        session.add(
            KnowledgeExtract(
                video_id=video.id,
                kind=cand.kind,
                content=cand.content,
                detail=cand.detail,
                start_s=cand.start_s,
                end_s=cand.end_s,
                confidence=cand.confidence,
                source=cand.source,
                model_id=cand.model_id,
                content_hash=cand.digest,
                provenance_json=json.dumps(cand.provenance, sort_keys=True),
            )
        )
        created += 1
    session.flush()

    # Remove regenerable rows that the newest pass no longer produces.
    stale = [
        row
        for row in existing
        if (row.kind, row.content_hash) not in wanted_keys
        and not int(row.edited or 0)
        and not int(row.rejected or 0)
    ]
    if stale:
        stale_ids = [row.id for row in stale]
        _delete_knowledge_fts(session, stale_ids)
        for row in stale:
            session.delete(row)
        session.flush()

    concept_rows = _persist_concepts(session, video, cands, transcript_text)
    _persist_relations(session, video, concept_rows)
    _reindex_knowledge_fts(session, video)

    session.add(
        ProcessingProvenance(
            entity_type="video_knowledge",
            entity_id=video.yt_id,
            step="knowledge_extract",
            model_id=MODEL_ID,
            content_hash=content_hash(transcript_text),
            status="ok",
            detail_json=json.dumps(
                {
                    "source_kind": source_kind,
                    "timing_available": timing_ok,
                    "candidates": len(cands),
                    "created": created,
                    "updated": updated,
                    "skipped_locked": skipped,
                    "stale_removed": len(stale),
                    "concepts": len(concept_rows),
                }
            ),
        )
    )
    session.flush()
    return {
        "video_id": video.yt_id,
        "source_kind": source_kind,
        "timing_available": timing_ok,
        "created": created,
        "updated": updated,
        "skipped_locked": skipped,
        "stale_removed": len(stale),
        "concepts": len(concept_rows),
        "extracts": list_extracts(session, video.yt_id),
    }


def _upsert_concept(session: Session, name: str, *, kind: str = "concept", description: str | None = None) -> Concept:
    slug = slugify(name)
    row = session.scalar(select(Concept).where(Concept.slug == slug))
    if row is None:
        row = Concept(slug=slug, name=name, kind=kind, description=description, origin="extracted")
        session.add(row)
        session.flush()
    else:
        row.updated_at = now_ms()
        if description and not row.description:
            row.description = description
    return row


def _persist_concepts(
    session: Session, video: Video, cands: list[Candidate], source: str
) -> list[tuple[Concept, float]]:
    concept_names: dict[str, float] = {}
    for cand in cands:
        if cand.kind == "concept":
            concept_names[cand.content] = max(concept_names.get(cand.content, 0.0), cand.confidence)
        elif cand.kind == "definition" and _is_concept_name(cand.content):
            concept_names[cand.content] = max(concept_names.get(cand.content, 0.0), cand.confidence)
        elif cand.kind == "prerequisite":
            concept_names[cand.content] = max(concept_names.get(cand.content, 0.0), cand.confidence * 0.9)
    out: list[tuple[Concept, float]] = []
    for name, confidence in concept_names.items():
        concept = _upsert_concept(session, name)
        link = session.get(VideoConcept, (video.id, concept.id))
        if link is None:
            session.add(
                VideoConcept(
                    video_id=video.id,
                    concept_id=concept.id,
                    weight=float(confidence),
                    provenance="extracted",
                    confidence=float(confidence),
                )
            )
        else:
            link.weight = max(float(link.weight or 0), float(confidence))
            link.confidence = max(float(link.confidence or 0), float(confidence))
        out.append((concept, float(confidence)))
    session.flush()
    return out


def _persist_relations(
    session: Session, video: Video, concept_rows: list[tuple[Concept, float]]
) -> None:
    # Co-occurrence only: both concepts are evidenced by the same stored video.
    for i, (a, wa) in enumerate(concept_rows):
        for b, wb in concept_rows[i + 1 :]:
            if a.id == b.id:
                continue
            confidence = round(min(0.9, 0.3 + 0.1 * min(wa, wb) + 0.05), 3)
            for src, dst in ((a, b), (b, a)):
                row = session.get(ConceptRelation, (src.id, dst.id, "related"))
                if row is None:
                    session.add(
                        ConceptRelation(
                            src_concept_id=src.id,
                            dst_concept_id=dst.id,
                            relation="related",
                            confidence=confidence,
                            evidence_video_id=video.id,
                            provenance_json=json.dumps(
                                {"method": "co_occurrence", "video_id": video.yt_id},
                                sort_keys=True,
                            ),
                        )
                    )
    # Explicit prerequisite statements connect the video's own concepts to the
    # named prerequisite when both names occur.
    names = {c.name.lower(): c for c, _ in concept_rows}
    prereq_rows = session.scalars(
        select(KnowledgeExtract).where(
            KnowledgeExtract.video_id == video.id,
            KnowledgeExtract.kind == "prerequisite",
            KnowledgeExtract.rejected == 0,
        )
    ).all()
    for pre in prereq_rows:
        target = names.get(pre.content.lower())
        if target is None:
            continue
        for concept, _w in concept_rows:
            if concept.id == target.id:
                continue
            row = session.get(ConceptRelation, (concept.id, target.id, "prerequisite"))
            if row is None:
                session.add(
                    ConceptRelation(
                        src_concept_id=concept.id,
                        dst_concept_id=target.id,
                        relation="prerequisite",
                        confidence=0.5,
                        evidence_video_id=video.id,
                        provenance_json=json.dumps(
                            {"method": "explicit_statement", "video_id": video.yt_id},
                            sort_keys=True,
                        ),
                    )
                )
    session.flush()


def _delete_knowledge_fts(session: Session, extract_ids: list[int]) -> None:
    for start in range(0, len(extract_ids), 400):
        chunk = extract_ids[start : start + 400]
        placeholders = ",".join(f":id{i}" for i in range(len(chunk)))
        session.execute(
            text(f"DELETE FROM knowledge_fts WHERE rowid IN ({placeholders})"),
            {f"id{i}": value for i, value in enumerate(chunk)},
        )


def _reindex_knowledge_fts(session: Session, video: Video) -> None:
    rows = session.execute(
        select(KnowledgeExtract.id, KnowledgeExtract.content, KnowledgeExtract.kind).where(
            KnowledgeExtract.video_id == video.id, KnowledgeExtract.rejected == 0
        )
    ).all()
    ids = [int(r[0]) for r in rows]
    if ids:
        _delete_knowledge_fts(session, ids)
    channel = session.get(Channel, video.channel_id) if video.channel_id else None
    for row_id, content, kind in rows:
        session.execute(
            text(
                "INSERT INTO knowledge_fts(rowid, content, kind, video_title, channel_name) "
                "VALUES (:id, :content, :kind, :title, :channel)"
            ),
            {
                "id": row_id,
                "content": content,
                "kind": kind,
                "title": video.title or "",
                "channel": channel.name if channel else "",
            },
        )


# ------------------------------------------------------------------ reads / edits


def list_extracts(
    session: Session, video_yt_id: str, *, include_rejected: bool = False, kind: str | None = None
) -> list[dict[str, Any]]:
    video = session.scalar(select(Video).where(Video.yt_id == video_yt_id))
    if video is None:
        return []
    q = select(KnowledgeExtract).where(KnowledgeExtract.video_id == video.id)
    if not include_rejected:
        q = q.where(KnowledgeExtract.rejected == 0)
    if kind:
        q = q.where(KnowledgeExtract.kind == kind)
    rows = session.scalars(q.order_by(KnowledgeExtract.kind, KnowledgeExtract.start_s, KnowledgeExtract.id)).all()
    return [_extract_out(row) for row in rows]


def _extract_out(row: KnowledgeExtract) -> dict[str, Any]:
    try:
        provenance = json.loads(row.provenance_json or "{}")
    except json.JSONDecodeError:
        provenance = {}
    return {
        "id": row.id,
        "video_id": row.video_id,
        "kind": row.kind,
        "content": row.content,
        "detail": row.detail,
        "start_s": row.start_s,
        "end_s": row.end_s,
        "confidence": round(float(row.confidence or 0), 3),
        "source": row.source,
        "model_id": row.model_id,
        "edited": bool(row.edited),
        "rejected": bool(row.rejected),
        "provenance": provenance,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def update_extract(
    session: Session,
    extract_id: int,
    *,
    content: str | None = None,
    detail: str | None = None,
    rejected: bool | None = None,
) -> dict[str, Any]:
    row = session.get(KnowledgeExtract, extract_id)
    if row is None:
        raise ExtractionError("extract not found")
    if content is not None:
        row.content = normalize_text(content) or row.content
    if detail is not None:
        row.detail = normalize_text(detail)
    if rejected is not None:
        row.rejected = 1 if rejected else 0
    row.edited = 1
    row.updated_at = now_ms()
    session.flush()
    _reindex_knowledge_fts(session, session.get(Video, row.video_id))
    session.flush()
    return _extract_out(row)


def regenerate_extract(session: Session, extract_id: int) -> dict[str, Any]:
    """Re-run extraction for the row's video, clearing the manual edit on that row.

    Explicit user action only; normal regeneration never touches edited rows.
    """
    row = session.get(KnowledgeExtract, extract_id)
    if row is None:
        raise ExtractionError("extract not found")
    video = session.get(Video, row.video_id)
    if video is None:
        raise ExtractionError("video not found")
    row.edited = 0
    row.rejected = 0
    session.flush()
    extract_knowledge(session, video.yt_id, force=True)
    refreshed = session.get(KnowledgeExtract, extract_id)
    return _extract_out(refreshed) if refreshed else {"id": extract_id, "deleted": True}


def knowledge_summary(session: Session, video_yt_id: str) -> dict[str, Any]:
    video = session.scalar(select(Video).where(Video.yt_id == video_yt_id))
    if video is None:
        raise ExtractionError("video not found")
    rows = list_extracts(session, video_yt_id)
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_kind.setdefault(row["kind"], []).append(row)
    concepts = [
        {"name": c.name, "slug": c.slug, "confidence": link.confidence}
        for c, link in session.execute(
            select(Concept, VideoConcept)
            .join(VideoConcept, VideoConcept.concept_id == Concept.id)
            .where(VideoConcept.video_id == video.id)
            .order_by(VideoConcept.weight.desc())
        ).all()
    ]
    return {
        "video": {
            "id": video.yt_id,
            "title": video.title,
            "duration_s": video.duration_s,
            "channel_id": video.channel_id,
        },
        "estimated_difficulty": round(
            difficulty_score(video.title, " ".join(r["content"] for r in rows), video.duration_s), 3
        ),
        "estimated_level": level_for_score(
            difficulty_score(video.title, " ".join(r["content"] for r in rows), video.duration_s)
        ),
        "counts": {kind: len(items) for kind, items in by_kind.items()},
        "extracts": rows,
        "concepts": concepts,
        "top_terms": title_terms(" ".join(r["content"] for r in rows), limit=8),
    }
