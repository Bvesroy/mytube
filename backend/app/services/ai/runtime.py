"""Runtime selection for the optional local LLM.

Environment variables remain the default. A `model_configs` row for role `llm`
can override provider/model/endpoint and enable/disable the runtime from the UI.
Saving a config never installs or downloads a model; `status()` reports what is
actually reachable on loopback.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ModelConfig, now_ms
from app.services.ai.embeddings import configured_engine
from app.services.ai.provider import LocalLLM

ROLE = "llm"


def get_llm_config(session: Session) -> ModelConfig | None:
    return session.scalar(
        select(ModelConfig)
        .where(ModelConfig.role == ROLE)
        .order_by(ModelConfig.updated_at.desc())
        .limit(1)
    )


def save_llm_config(
    session: Session,
    *,
    enabled: bool,
    provider: str,
    model: str,
    endpoint: str | None = None,
    extra: dict[str, Any] | None = None,
) -> ModelConfig:
    provider = (provider or "ollama").strip().lower()
    if provider not in {"ollama", "llamacpp", "llama.cpp", "none"}:
        raise ValueError("provider must be ollama, llamacpp or none")
    model = (model or "").strip()
    if provider != "none" and not model:
        raise ValueError("model name required")
    row = get_llm_config(session)
    if row is None:
        row = ModelConfig(
            id="mc_" + secrets.token_hex(8),
            role=ROLE,
            provider=provider,
            model=model or "none",
            endpoint=endpoint,
            enabled=1 if enabled else 0,
            config_json=json.dumps(extra or {}, sort_keys=True),
            updated_at=now_ms(),
        )
        session.add(row)
    else:
        row.provider = provider
        row.model = model or "none"
        row.endpoint = endpoint
        row.enabled = 1 if enabled else 0
        row.config_json = json.dumps(extra or {}, sort_keys=True)
        row.updated_at = now_ms()
    session.flush()
    return row


def resolve_llm(session: Session | None = None, *, timeout: float = 1.5) -> LocalLLM:
    row = get_llm_config(session) if session is not None else None
    if row is None:
        return LocalLLM.detect(timeout=timeout)
    if row.provider == "none" or not int(row.enabled or 0):
        return LocalLLM(provider="none", model=row.model, error="AI disabled in My Tube settings")
    return LocalLLM.detect(
        timeout=timeout,
        enabled=True,
        provider=row.provider,
        model=row.model,
        endpoint=row.endpoint,
    )


def ai_status(session: Session) -> dict[str, Any]:
    llm = resolve_llm(session)
    engine = configured_engine()
    row = get_llm_config(session)
    return {
        "enabled": bool(int(row.enabled)) if row is not None else llm.status()["enabled"],
        "llm": llm.status(),
        "embedding": {
            "model_id": engine.model_id,
            "dim": engine.dim,
            "available": engine.available,
            "local": True,
            "note": (
                "Deterministic hashed TF vectors are the default. Set "
                "MYTUBE_EMBEDDING_MODEL to opt into an installed sentence-transformers model."
            ),
        },
        "transcription": {
            "available": False,
            "note": (
                "No local ASR model is bundled. Supply creator captions or a local "
                "whisper.cpp transcript through the ingest endpoint; nothing is downloaded."
            ),
        },
        "selected_config": (
            {
                "provider": row.provider,
                "model": row.model,
                "endpoint": row.endpoint,
                "enabled": bool(row.enabled),
            }
            if row
            else None
        ),
        "policy": {
            "local_only": True,
            "requires_cloud": False,
            "downloads_models": False,
            "works_without_ai": True,
        },
    }
