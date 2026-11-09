"""Local-first AI helpers (My Tube 3.0).

Nothing here downloads a model, calls a cloud API, or requires a GPU. The default
embedding engine is a deterministic hashed TF vectoriser that always works offline;
an optional local LLM (Ollama or llama.cpp server) is detected on loopback only and
is never required for any feature. Every consumer must degrade gracefully when
`available` is false.
"""

from app.services.ai.embeddings import (
    DEFAULT_EMBEDDING_MODEL,
    EmbeddingEngine,
    cosine,
    embed_text,
    pack_vector,
    unpack_vector,
)
from app.services.ai.provider import AIUnavailable, LocalLLM, provider_status

__all__ = [
    "AIUnavailable",
    "DEFAULT_EMBEDDING_MODEL",
    "EmbeddingEngine",
    "LocalLLM",
    "cosine",
    "embed_text",
    "pack_vector",
    "provider_status",
    "unpack_vector",
]
