"""Background-job registry and runner entrypoint."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import BackgroundJob

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobHandler:
    run: Callable[[Session, BackgroundJob, dict[str, Any]], dict[str, Any]]


_REGISTRY: dict[str, JobHandler] = {}


def register(job_type: str, handler: JobHandler) -> None:
    _REGISTRY[job_type] = handler


def resolve(job_type: str) -> JobHandler:
    try:
        return _REGISTRY[job_type]
    except KeyError:
        raise ValueError(f"unknown job type: {job_type!r}") from None


def run_job(session: Session, job: BackgroundJob) -> dict[str, Any]:
    handler = resolve(job.type)
    payload = json.loads(job.payload_json or "{}")
    return handler.run(session, job, payload)


def ensure_default_handlers_registered() -> None:
    # Import registers handlers (idempotent enough for our simple registry).
    from app.services import sync  # noqa: F401
    from app.services import intelligence_jobs  # noqa: F401
