"""Background-job registry: maps job types to handlers and provides the runner entrypoint.

The queue itself lives in `app.repositories.jobs`. This module is the adapter layer between
the queue and the actual work: importers, sync workers, FTS rebuilds, model training, etc.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobHandler:
    run: Callable[[Session, dict[str, Any]], dict[str, Any]]


_REGISTRY: dict[str, JobHandler] = {}


def register(job_type: str, handler: JobHandler) -> None:
    _REGISTRY[job_type] = handler


def resolve(job_type: str) -> JobHandler:
    try:
        return _REGISTRY[job_type]
    except KeyError:
        raise ValueError(f"unknown job type: {job_type!r}") from None


def run_job(session: Session, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    handler = resolve(payload["type"])
    return handler.run(session, payload)
