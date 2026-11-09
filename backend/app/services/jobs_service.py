"""Job service: enqueue, claim, heartbeat, finish, recover stale, list jobs.

Two different "jobs" environments are covered here: the queue-backed
`BackgroundJob` table used by the upgrade path, and a lighter in-memory
placeholder used only when running the test worker in isolation.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import BackgroundJob, now_ms

logger = logging.getLogger(__name__)

STATUS_CLAIMED = "claimed"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


@dataclass
class Job:
    id: str
    type: str
    status: str
    priority: int
    created_at: int
    started_at: int | None = None
    finished_at: int | None = None
    progress: float = 0.0
    stage: str | None = None
    payload_json: str = "{}"
    result_json: str | None = None
    error: str | None = None
    attempts: int = 0
    max_attempts: int = 3
    cancel_requested: int = 0
    worker_id: str | None = None
    heartbeat_at: int | None = None
    dedupe_key: str | None = None
    title: str | None = None


class JobService:
    def __init__(self, session: Session):
        self.session = session

    def enqueue(
        self,
        job_type: str,
        payload: dict[str, Any],
        *,
        dedupe_key: str | None = None,
        deduplication_window_until: int | None = None,
        priority: int = 5,
        title: str | None = None,
    ) -> Job:
        id_ = f"job-{secrets.token_hex(4)}"
        row = BackgroundJob(
            id=id_,
            type=job_type,
            status="queued",
            priority=priority,
            created_at=now_ms(),
            payload_json=json.dumps(payload, sort_keys=True),
            title=title,
            dedupe_key=dedupe_key,
            deduplication_window_until=deduplication_window_until,
            max_attempts=3,
        )
        self.session.add(row)
        self.session.flush()
        return Job(
            id=row.id,
            type=row.type,
            status=row.status,
            priority=row.priority,
            created_at=row.created_at,
            dedupe_key=row.dedupe_key,
            title=row.title,
            payload_json=row.payload_json,
        )

    def claim(self, worker_id: str, *, limit: int = 1) -> Job | None:
        rows = (
            self.session.execute(
                select(BackgroundJob)
                .where(BackgroundJob.status == "queued")
                .order_by(BackgroundJob.priority.desc(), BackgroundJob.created_at)
                .limit(limit)
            )
            .scalars()
            .all()
        )
        for row in rows[:limit]:
            row.status = "running"
            row.worker_id = worker_id
            row.started_at = now_ms()
            row.heartbeat_at = now_ms()
            self.session.add(row)
            self.session.flush()
            return Job(
                id=row.id,
                type=row.type,
                status=row.status,
                priority=row.priority,
                created_at=row.created_at,
                started_at=row.started_at,
                heartbeat_at=row.heartbeat_at,
                worker_id=row.worker_id,
                dedupe_key=row.dedupe_key,
                title=row.title,
                payload_json=row.payload_json,
            )
        return None

    def heartbeat(self, job_id: str) -> None:
        self.session.execute(
            BackgroundJob.__table__.update()
            .where(BackgroundJob.id == job_id)
            .where(BackgroundJob.status == "running")
            .values(heartbeat_at=now_ms())
        )

    def finish(
        self,
        job_id: str,
        *,
        status: str = "done",
        progress: float = 1.0,
        result_json: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        self.session.execute(
            BackgroundJob.__table__.update()
            .where(BackgroundJob.id == job_id)
            .values(
                status=status,
                progress=progress,
                finished_at=now_ms(),
                result_json=json.dumps(result_json, sort_keys=True) if result_json else None,
                error=error,
            )
        )

    def recover_stale(self, worker_id: str, *, stale_ms: int = 30_000) -> int:
        cutoff = now_ms() - stale_ms
        rows = (
            self.session.execute(
                select(BackgroundJob)
                .where(BackgroundJob.status == "running")
                .where(BackgroundJob.worker_id == worker_id)
                .where(BackgroundJob.heartbeat_at < cutoff)
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.status = "queued"
            row.worker_id = None
            row.started_at = None
            row.heartbeat_at = None
            self.session.add(row)
        return len(rows)

    def list_jobs(
        self,
        *,
        status: str | None = None,
        job_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Job]:
        q = select(BackgroundJob)
        if status:
            q = q.where(BackgroundJob.status == status)
        if job_type:
            q = q.where(BackgroundJob.type == job_type)
        rows = (
            self.session.execute(q.order_by(BackgroundJob.created_at.desc()).limit(limit).offset(offset))
            .scalars()
            .all()
        )
        return [
            Job(
                id=r.id,
                type=r.type,
                status=r.status,
                priority=r.priority,
                created_at=r.created_at,
                started_at=r.started_at,
                finished_at=r.finished_at,
                progress=r.progress,
                stage=r.stage,
                payload_json=r.payload_json,
                result_json=r.result_json,
                error=r.error,
                attempts=r.attempts,
                max_attempts=r.max_attempts,
                cancel_requested=r.cancel_requested,
                worker_id=r.worker_id,
                heartbeat_at=r.heartbeat_at,
                dedupe_key=r.dedupe_key,
                title=r.title,
            )
            for r in rows
        ]
