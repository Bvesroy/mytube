"""Shared, dependency-free text utilities for the knowledge pipeline.

Everything here is deterministic and offline. No tokenizer model, no network.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

STOPWORDS = {
    "a", "about", "above", "after", "again", "against", "all", "also", "am", "an", "and",
    "any", "are", "as", "at", "be", "because", "been", "before", "being", "below", "between",
    "both", "but", "by", "can", "could", "did", "do", "does", "doing", "don", "down", "during",
    "each", "few", "for", "from", "further", "get", "got", "had", "has", "have", "having",
    "he", "her", "here", "hers", "herself", "him", "himself", "his", "how", "i", "if", "in",
    "into", "is", "it", "its", "itself", "just", "know", "let", "like", "make", "me", "more",
    "most", "much", "my", "myself", "need", "no", "nor", "not", "now", "of", "off", "on",
    "once", "only", "or", "other", "our", "ours", "ourselves", "out", "over", "own", "really",
    "right", "said", "same", "say", "see", "she", "should", "so", "some", "something", "such",
    "than", "that", "the", "their", "theirs", "them", "themselves", "then", "there", "these",
    "they", "thing", "things", "this", "those", "through", "to", "too", "under", "until",
    "up", "very", "want", "was", "way", "we", "well", "were", "what", "when", "where",
    "which", "while", "who", "whom", "why", "will", "with", "would", "you", "your", "yours",
    "yourself", "yourselves", "going", "gonna", "okay", "ok", "yeah", "yes", "actually",
    "basically", "kind", "sort", "lot", "bit", "one", "two", "first", "second", "next",
    "last", "thing", "stuff", "look", "looks", "use", "used", "using", "work", "works",
    "working", "call", "called", "different", "example", "maybe", "probably", "whole",
    "part", "point", "today", "video", "watching", "watch",
}

_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_+#.\-/]{1,48}")
_ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,8}\b")
_CAP_PHRASE_RE = re.compile(r"\b(?:[A-Z][a-zA-Z0-9+#.\-]+(?:\s+[A-Z][a-zA-Z0-9+#.\-]+){0,3})\b")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'“(])")

ABBREVIATIONS = {"e.g.", "i.e.", "etc.", "vs.", "mr.", "mrs.", "dr.", "prof.", "fig.", "eq.", "no."}


def normalize_text(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u00a0", " ")
    text = text.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    text = text.replace("—", " - ").replace("–", " - ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return text.strip()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^a-zA-Z0-9+#]+", "-", value.lower()).strip("-")
    return value[:190] or "concept"


def words(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text or "")]


def keyword_scores(text: str, *, limit: int = 20) -> list[tuple[str, float]]:
    """Unigram + bigram salience without a corpus.

    Score = sublinear term frequency × length prior, with stopwords removed. The
    absolute value is not meaningful; ordering is used for summaries and titles.
    """
    toks = [w for w in words(text) if w not in STOPWORDS and len(w) > 2 and not w.isdigit()]
    counts = Counter(toks)
    bigrams = Counter(
        f"{a} {b}"
        for a, b in zip(toks, toks[1:])
        if a != b
    )
    scores: dict[str, float] = {}
    for term, n in counts.items():
        scores[term] = (1.0 + math.log(n)) * (1.0 + min(len(term), 12) / 20.0)
    for term, n in bigrams.items():
        if n >= 2:
            scores[term] = (1.0 + math.log(n)) * 1.25
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return ranked[:limit]


def keywords(text: str, *, limit: int = 10) -> list[str]:
    return [term for term, _score in keyword_scores(text, limit=limit) if " " not in term]


def title_terms(text: str, *, limit: int = 6) -> list[str]:
    ranked = keyword_scores(text, limit=limit * 3)
    out: list[str] = []
    for term, _ in ranked:
        label = " ".join(p.capitalize() if p.islower() else p for p in term.split())
        if label.lower() not in {o.lower() for o in out}:
            out.append(label)
        if len(out) >= limit:
            break
    return out


@dataclass(frozen=True)
class Sentence:
    text: str
    start_s: float | None = None
    end_s: float | None = None


def split_sentences(text: str, *, start_s: float | None = None, end_s: float | None = None) -> list[Sentence]:
    """Split into sentences, preserving an optional timestamp for the whole block."""
    text = normalize_text(text)
    if not text:
        return []
    parts = _SENTENCE_END.split(text)
    out: list[Sentence] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # Re-join a trailing abbreviation split ("e.g." + " this").
        if out and out[-1].text.lower().endswith(tuple(ABBREVIATIONS)):
            merged = out.pop()
            part = merged.text + " " + part
        out.append(Sentence(text=part, start_s=start_s, end_s=end_s))
    return [s for s in out if len(s.text) >= 12]


def sentences_with_timing(text: str, segments: list[tuple[float | None, float | None, str]]) -> list[Sentence]:
    out: list[Sentence] = []
    for start, end, seg_text in segments:
        out.extend(split_sentences(seg_text, start_s=start, end_s=end))
    return out


def sentence_score(sentence: str) -> float:
    toks = [w for w in words(sentence) if w not in STOPWORDS]
    if not toks:
        return 0.0
    unique = len(set(toks))
    return unique * 0.6 + min(len(sentence), 240) / 120.0


def best_sentences(sentences: list[Sentence], *, count: int, max_chars: int) -> list[Sentence]:
    ranked = sorted(
        sentences,
        key=lambda s: (-sentence_score(s.text), len(s.text)),
    )
    chosen: list[Sentence] = []
    used = 0
    for sent in ranked:
        if used + len(sent.text) > max_chars and chosen:
            continue
        chosen.append(sent)
        used += len(sent.text) + 1
        if len(chosen) >= count:
            break
    # Present in transcript order.
    order = {id(s): i for i, s in enumerate(sentences)}
    return sorted(chosen, key=lambda s: order.get(id(s), 0))


def excerpt(text: str, term: str, *, width: int = 180) -> str:
    text = normalize_text(text)
    if not term:
        return text[:width]
    idx = text.lower().find(term.lower())
    if idx < 0:
        return text[:width]
    start = max(0, idx - width // 3)
    end = min(len(text), start + width)
    prefix = "…" if start else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end]}{suffix}"


def difficulty_score(title: str = "", text: str = "", duration_s: int | None = None) -> float:
    """Heuristic 0..1 difficulty from surface cues only.

    This is deliberately not a mastery judgement; it only orders learning-path
    stages. The UI labels it an estimate.
    """
    blob = f"{title} {text}".lower()
    score = 0.45
    beginner = ("beginner", "intro", "introduction", "basics", "from scratch", "explained simply", "for beginners")
    advanced = ("advanced", "deep dive", "internals", "in depth", "expert", "optimization", "performance")
    research = ("proof", "theorem", "paper", "research", "derivation", "math", "formal")
    if any(term in blob for term in beginner):
        score -= 0.25
    if any(term in blob for term in advanced):
        score += 0.2
    if any(term in blob for term in research):
        score += 0.2
    if duration_s and duration_s > 3600:
        score += 0.1
    return max(0.0, min(1.0, score))


def level_for_score(score: float) -> str:
    if score < 0.35:
        return "beginner"
    if score < 0.55:
        return "intermediate"
    if score < 0.75:
        return "advanced"
    return "research"


def fts_query(query: str) -> str:
    """Build a conservative FTS5 MATCH expression from free text.

    Terms are quoted; quotes are doubled. No user text is ever interpolated as SQL.
    """
    terms: list[str] = []
    for raw in re.findall(r"[A-Za-z0-9_+#.\-]{2,40}", query or ""):
        token = raw.replace('"', '""')
        if token.lower() in STOPWORDS:
            continue
        terms.append(f'"{token}"')
    return " OR ".join(terms)
