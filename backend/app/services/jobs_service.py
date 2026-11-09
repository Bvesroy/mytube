"""DB-backed background job service aligned to `BackgroundJob` model.

Supports: enqueue, claim, heartbeat/progress, cancel, finish (with retryable
failure), stale recovery, get/list. Local-first, single-process friendly.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import BackgroundJob, now_ms


def _new_id() -> str:
    return secrets.token_hex(16)[:32]


class JobService:
    def __init__(self, session: Session):
        self.session = session

    def enqueue(
        self,
        job_type: str,
        payload: dict[str, Any],
        *,
        dedupe_key: str | None = None,
        priority: int = 5,
        max_attempts: int = 3,
    ) -> BackgroundJob:
        row = BackgroundJob(
            id=_new_id(),
            type=job_type,
            status="queued",
            priority=priority,
            created_at=now_ms(),
            payload_json=json.dumps(payload, sort_keys=True),
            dedupe_key=dedupe_key,
            attempts=0,
            max_attempts=max_attempts,
            cancel_requested=0,
            progress=0.0,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get(self, job_id: str) -> BackgroundJob | None:
        return self.session.get(BackgroundJob, job_id)

    def claim(self, worker_id: str) -> BackgroundJob | None:
        # Claim highest-priority (lowest number) queued job not canceled.
        rows = (
            self.session.execute(
                select(BackgroundJob)
                .where(BackgroundJob.status == "queued", BackgroundJob.cancel_requested == 0)
                .order_by(BackgroundJob.priority.asc(), BackgroundJob.created_at.asc())
                .limit(1)
            )
            .scalars()
            .all()
        )
        if not rows:
            return None
        row = rows[0]
        row.status = "running"
        row.worker_id = worker_id
        row.started_at = now_ms()
        row.heartbeat_at = now_ms()
        row.attempts = int(row.attempts or 0) + 1
        row.stage = "starting"
        row.progress = max(float(row.progress or 0), 0.01)
        self.session.flush()
        return row

    def heartbeat(self, job_id: str, *, progress: float | None = None, stage: str | None = None) -> bool:
        row = self.get(job_id)
        if not row or row.status != "running":
            return False
        row.heartbeat_at = now_ms()
        if progress is not None:
            row.progress = max(0.0, min(1.0, float(progress)))
        if stage is not None:
            row.stage = stage
        self.session.flush()
        return True

    def is_cancel_requested(self, job_id: str) -> bool:
        row = self.get(job_id)
        return bool(row and int(row.cancel_requested or 0))

    def request_cancel(self, job_id: str) -> BackgroundJob | None:
        row = self.get(job_id)
        if not row:
            return None
        row.cancel_requested = 1
        if row.status == "queued":
            row.status = "canceled"
            row.finished_at = now_ms()
            row.stage = "canceled"
        # if running, handler should observe and finish as canceled
        self.session.flush()
        return row

    def finish(
        self,
        job_id: str,
        *,
        status: str = "done",
        progress: float | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> BackgroundJob | None:
        row = self.get(job_id)
        if not row:
            return None
        row.status = status
        row.finished_at = now_ms()
        row.heartbeat_at = now_ms()
        if progress is not None:
            row.progress = max(0.0, min(1.0, float(progress)))
        elif status == "done":
            row.progress = 1.0
        row.result_json = json.dumps(result, sort_keys=True) if result is not None else row.result_json
        row.error = error
        if status in ("done", "failed", "canceled"):
            row.stage = status
        self.session.flush()
        return row

    def fail_or_retry(self, job_id: str, *, error: str) -> BackgroundJob | None:
        row = self.get(job_id)
        if not row:
            return None
        if int(row.attempts or 0) < int(row.max_attempts or 1) and not int(row.cancel_requested or 0):
            row.status = "queued"
            row.worker_id = None
            row.started_at = None
            row.heartbeat_at = now_ms()
            row.error = error
            row.stage = "retry_queued"
            # keep progress as-is (informational)
        else:
            row.status = "failed"
            row.finished_at = now_ms()
            row.error = error
            row.stage = "failed"
        self.session.flush()
        return row

    def recover_stale(self, *, stale_ms: int = 60_000) -> int:
        cutoff = now_ms() - stale_ms
        rows = (
            self.session.execute(
                select(BackgroundJob).where(
                    BackgroundJob.status == "running",
                    (BackgroundJob.heartbeat_at.is_(None)) | (BackgroundJob.heartbeat_at < cutoff),
                )
            )
            .scalars()
            .all()
        )
        n = 0
        for row in rows:
            if int(row.cancel_requested or 0):
                row.status = "canceled"
                row.finished_at = now_ms()
                row.stage = "canceled_stale"
            elif int(row.attempts or 0) < int(row.max_attempts or 1):
                row.status = "queued"
                row.stage = "recovered_retry"
            else:
                row.status = "failed"
                row.error = row.error or "stale worker recovery"
                row.finished_at = now_ms()
                row.stage = "recovered_failed"
            row.worker_id = None
            row.heartbeat_at = now_ms()
            n += 1
        self.session.flush()
        return n

    def list_jobs(self, *, status: str | None = None, limit: int = 100, offset: int = 0) -> list[BackgroundJob]:
        q = select(BackgroundJob)
        if status:
            q = q.where(BackgroundJob.status == status)
        return list(
            self.session.execute(q.order_by(BackgroundJob.created_at.desc()).limit(limit).offset(offset))
            .scalars()
            .all()
        )
