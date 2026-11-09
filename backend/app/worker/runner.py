"""Background worker runner.

Single worker, single DB connection (WAL mode lets the API keep serving while this
process imports). Claims jobs with `UPDATE … RETURNING` semantics, heartbeats on long
runs, and recovers stale jobs if the worker stops unexpectedly.
"""

from __future__ import annotations

import logging
import signal
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings
from app.repositories.jobs import claim, heartbeat, finish, recover_stale
from app.services.jobs import run_job

logger = logging.getLogger(__name__)

DEFAULT_POLL = 2.0
DEFAULT_STALE_MS = 30_000


@dataclass
class Worker:
    settings: Settings
    poll_s: float = DEFAULT_POLL
    stale_ms: int = DEFAULT_STALE_MS
    worker_id: str
    running: bool = True

    defonce(self) -> None:
        recover_stale(self.session_factory, worker_id=self.worker_id, stale_ms=self.stale_ms)

    @property
    def session_factory(self):
        from app.db.engine import Database
        return Database(self.settings.db_path).session_factory

    def run_once(self) -> bool:
        session = self.session_factory()
        try:
            job = claim(session, worker_id=self.worker_id, limit=1)
            if job is None:
                return False
            logger.info("claimed job %s (%s)", job.id, job.type)
            session.execute(
                "UPDATE background_jobs SET started_at=?, heartbeat_at=?, status='running', stage='running' WHERE id=?",
                (int(time.time() * 1000), int(time.time() * 1000), job.id),
            )
            session.commit()
            try:
                payload = job.payload_json and job.payload_json or "{}"
                result = run_job(session, job.id, payload)
                finish(session, job.id, result_json=result)
                logger.info("finished job %s", job.id)
            except Exception:
                logger.exception("job %s failed", job.id)
                finish(
                    session,
                    job.id,
                    status="failed",
                    error=logger.exception.__self__.msg if hasattr(logger.exception.__self__, "msg") else "",
                )
                raise
            finally:
                try:
                    session.close()
                except Exception:
                    pass
            return True
        finally:
            try:
                session.close()
            except Exception:
                pass

    def loop(self) -> None:
        self.once()
        while self.running:
            if self.run_once():
                continue
            time.sleep(self.poll_s)

    def stop(self) -> None:
        self.running = False


def start_worker(settings: Settings, *, worker_id: str | None = None) -> Worker:
    wid = worker_id or f"worker-{int(time.time() * 1000)}"
    worker = Worker(settings=settings, worker_id=wid)
    logger.info("starting worker %s", wid)

    def _handle_shutdown(signum, frame):
        logger.info("shutdown signal %s received", signum)
        worker.stop()

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    worker.loop()
    logger.info("worker %s stopped", wid)
    return worker
