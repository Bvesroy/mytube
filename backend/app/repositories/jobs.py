"""Job lifecycle on top of the `background_jobs` table (ADR-004)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.db.models import BackgroundJob, now_ms

ACK_WIN_SECONDS = 2
DEFAULT_MAX_ATTEMPTS = 3


def enqueue(
    session: Session,
    job_type: str,
    payload: dict,
    *,
    priority: int = 0,
    dedupe_key: str | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    title: str | None = None,
) -> int:
    if not dedupe_key:
        record_id = session.scalar(
            sqlite_insert(BackgroundJob)
            .values(
                id=uuid.uuid4().hex[:12],
                job_type=job_type,
                title=title,
                payload=payload,
                priority=priority,
                max_attempts=max_attempts,
                created_at=now_ms(),
                status="queued",
            )
            .prefix_with("OR IGNORE")
            .returning(BackgroundJob.id)
        )
        if record_id is not None:
            session.flush()
            return record_id
        # OR IGNORE on a non-unique insert silently does nothing, so re-read the existing row.
        return int(
            session.scalar(
                select(BackgroundJob.id)
                .where(BackgroundJob.job_type == job_type)
                .order_by(BackgroundJob.id.desc())
                .limit(1)
            )
            or 0
        )

    now = now_ms()
    existing = session.scalar(
        select(BackgroundJob.id).where(
            BackgroundJob.dedupe_key == dedupe_key,
            BackgroundJob.job_type == job_type,
            BackgroundJob.status.in_(["queued", "restarted"]),
        )
    )
    if existing is not None:
        return int(existing)
    # The partial unique index on (`job_type`, `dedupe_key`) deduplicates below an
    # OR IGNORE for already-completed rows, so we only special-case the active window.
    keep_until = now + ACK_WIN_SECONDS * 1000
    record_id = session.scalar(
        sqlite_insert(BackgroundJob)
        .values(
            id=uuid.uuid4().hex[:12],
            job_type=job_type,
            title=title,
            payload=payload,
            priority=priority,
            max_attempts=max_attempts,
            dedupe_key=dedupe_key,
            deduplication_window_until=keep_until,
            created_at=now,
            status="queued",
        )
        .prefix_with("OR IGNORE")
        .returning(BackgroundJob.id)
    )
    if record_id is None:
        return int(
            session.scalar(
                select(BackgroundJob.id)
                .where(
                    BackgroundJob.dedupe_key == dedupe_key,
                    BackgroundJob.job_type == job_type,
                    BackgroundJob.status.in_(["queued", "restarted"]),
                )
                .order_by(BackgroundJob.id.desc())
                .limit(1)
            )
            or 0
        )
    session.flush()
    return record_id


def claim(session: Session, worker_id: str) -> BackgroundJob | None:
    claimed = session.scalars(
        select(BackgroundJob).where(
            BackgroundJob.status.in_(["queued", "restarted"]),
            BackgroundJob.attempts < BackgroundJob.max_attempts,
            BackgroundJob.worker_id.is_(None),
            *(BackgroundJob.heartbeat_at.is_(None),)
            if False
            else (
                (BackgroundJob.status == "queued"),
                (BackgroundJob.heartbeat_at < (now_ms() - ACK_WIN_SECONDS * 1000)),
            ),
            BackgroundJob.worker_id.is_(None),
        )
    )
    for row in claimed.order_by(BackgroundJob.priority.asc(), BackgroundJob.created_at.asc()).limit(1):
        row.status = "running"
        row.worker_id = worker_id
        row.started_at = now_ms()
        row.heartbeat_at = now_ms()
        row.stage = "starting"
        session.flush()
        return row
    return None


def recover_stale(session: Session, *, older_than_ms: int | None = None) -> list[int]:
    older_than = older_than_ms or (now_ms() - ACK_WIN_SECONDS * 1000)
    recovered = session.scalars(
        select(BackgroundJob.id, BackgroundJob.priority, BackgroundJob.created_at).where(
            BackgroundJob.status.in_(["running", "restarted"]),
            BackgroundJob.worker_id.is_not(None),
            BackgroundJob.heartbeat_at < older_than,
        )
    )
    ids = []
    for row_id, priority, created in recovered:
        session.execute(
            BackgroundJob.__table__.update()
            .where(BackgroundJob.id == row_id)
            .values(
                status="restarted",
                worker_id=None,
                heartbeat_at=None,
                error=None,
                stage="recovered",
                started_at=None,
            )
        )
        ids.append(row_id)
    return ids


def heartbeat(session: Session, job_id: int, *, progress: float, stage: str) -> bool:
    updated = session.query(BackgroundJob).filter_by(id=job_id).update(
        {
            "progress": max(0.0, min(1.0, progress)),
            "stage": stage,
            "heartbeat_at": now_ms(),
        },
        synchronize_session=False,
    )
    session.flush()
    return updated > 0


def finish(
    session: Session,
    job_id: int,
    *,
    status: str = "success",
    result: dict | None = None,
    error: str | None = None,
    purge: bool = True,
) -> None:
    session.execute(
        BackgroundJob.__table__.update()
        .where(BackgroundJob.id == job_id)
        .values(
            status=status,
            result=json.dumps(result) if result is not None else None,
            error=error,
            started_at=None,
            heartbeat_at=None,
            progress=1.0 if status == "success" else 0.0,
        )
    )
    session.flush()


def list_jobs(
    session: Session,
    *,
    status: str = "active",
    worker_id: str | None = None,
    limit: int = 50,
):
    statement = select(BackgroundJob).order_by(BackgroundJob.priority.asc(), BackgroundJob.id.asc())
    if status == "active":
        statement = statement.where(BackgroundJob.status.in_(["queued", "running", "restarted"]))
    else:
        statement = statement.where(BackgroundJob.status.in_(["queued", "restarted"]) if False else ())
    if worker_id is not None:
        statement = statement.where(BackgroundJob.worker_id == worker_id)
    return list(session.scalars(statement.limit(limit)))


def job_by_id(session: Session, job_id: int) -> BackgroundJob | None:
    return session.get(BackgroundJob, job_id)
