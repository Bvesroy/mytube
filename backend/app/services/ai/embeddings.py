"""Deterministic, offline embedding engine.

The default backend is a signed feature-hashing vectoriser over word unigrams,
bigrams and character trigrams. It needs no model file, no network and no GPU, so
semantic retrieval still works on a fresh install. The model id is stored beside
every vector; changing the model or dimension invalidates old rows rather than
silently mixing incompatible vectors.

An optional `sentence-transformers` backend is used only when the package is
already installed *and* the operator opts in with `MYTUBE_EMBEDDING_MODEL`.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import struct
from collections import Counter
from dataclasses import dataclass

DEFAULT_EMBEDDING_MODEL = "mytube-hash-v1"
DEFAULT_DIM = 256

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_+#.\-]{1,40}", re.IGNORECASE)


def tokenize(text: str) -> list[str]:
    if not text:
        return []
    return [t.lower().strip(".-") for t in _TOKEN_RE.findall(text) if len(t.strip(".-")) > 1]


def _features(text: str) -> Counter[str]:
    """Unigrams + bigrams + character trigrams of the title-like head words."""
    tokens = tokenize(text)
    feats: Counter[str] = Counter(tokens)
    for a, b in zip(tokens, tokens[1:]):
        feats[f"{a} {b}"] += 1
    for tok in tokens[:40]:
        padded = f" {tok} "
        for i in range(len(padded) - 3):
            feats[f"#{padded[i:i + 3]}"] += 1
    return feats


def _bucket(feature: str, dim: int) -> tuple[int, float]:
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    return value % dim, 1.0 if (value >> 63) & 1 else -1.0


def embed_text(text: str, *, dim: int = DEFAULT_DIM) -> list[float]:
    """Return an L2-normalised signed-hashing vector (all zeros for empty text)."""
    vec = [0.0] * dim
    feats = _features(text)
    if not feats:
        return vec
    for feature, count in feats.items():
        idx, sign = _bucket(feature, dim)
        vec[idx] += sign * (1.0 + math.log(count))
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return vec
    return [v / norm for v in vec]


def pack_vector(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack_vector(blob: bytes, dim: int | None = None) -> list[float]:
    if dim is None:
        dim = len(blob) // 4
    if dim <= 0 or len(blob) != dim * 4:
        return []
    return list(struct.unpack(f"<{dim}f", blob))


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    # Vectors are normally already normalised; recompute defensively.
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


@dataclass
class EmbeddingEngine:
    model_id: str = DEFAULT_EMBEDDING_MODEL
    dim: int = DEFAULT_DIM

    def embed(self, text: str) -> list[float]:
        return embed_text(text, dim=self.dim)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]

    @property
    def available(self) -> bool:
        return True


def configured_engine() -> EmbeddingEngine:
    """Choose the embedding backend from the environment, with a guaranteed fallback.

    The optional sentence-transformers path is opt-in *and* only used when the
    package is already installed. It is never installed automatically.
    """
    model = os.environ.get("MYTUBE_EMBEDDING_MODEL", "").strip()
    if model:
        try:  # pragma: no cover - depends on optional local install
            from sentence_transformers import SentenceTransformer  # type: ignore

            st = SentenceTransformer(model, device="cpu")
            dim = int(st.get_sentence_embedding_dimension() or 384)

            class _STEngine(EmbeddingEngine):
                def embed(self, text: str) -> list[float]:  # type: ignore[override]
                    vec = st.encode([text], normalize_embeddings=True)[0]
                    return [float(x) for x in vec]

            return _STEngine(model_id=f"st:{model}", dim=dim)
        except Exception:
            pass
    return EmbeddingEngine()
