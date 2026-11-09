"""Simple local background worker runner (single process)."""

from __future__ import annotations

import logging
import signal
import time

from app.config import Settings
from app.db.engine import Database
from app.services.jobs_service import JobService
from app.services.jobs import run_job, ensure_default_handlers_registered

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, settings: Settings, *, worker_id: str, poll_s: float = 1.0):
        self.settings = settings
        self.worker_id = worker_id
        self.poll_s = poll_s
        self.running = True
        self.db = Database.open(settings.db_path)

    def run_once(self) -> bool:
        ensure_default_handlers_registered()
        with self.db.session() as session:
            svc = JobService(session)
            svc.recover_stale()
            job = svc.claim(self.worker_id)
            if not job:
                session.commit()
                return False
            job_id = job.id
            session.commit()
        # Execute outside claim transaction scope per step for simplicity/visibility.
        with self.db.session() as session:
            svc = JobService(session)
            job = svc.get(job_id)
            if not job:
                return True
            try:
                if svc.is_cancel_requested(job_id):
                    svc.finish(job_id, status="canceled")
                    session.commit()
                    return True
                result = run_job(session, job)
                # Handler may have committed progress; reload status.
                job = svc.get(job_id)
                if job and svc.is_cancel_requested(job_id):
                    svc.finish(job_id, status="canceled", result=result)
                elif job and job.status == "running":
                    svc.finish(job_id, status="done", result=result)
                session.commit()
            except Exception as exc:
                logger.exception("job %s failed", job_id)
                session.rollback()
                with self.db.session() as s2:
                    JobService(s2).fail_or_retry(job_id, error=f"{type(exc).__name__}: {exc}")
                    s2.commit()
        return True

    def loop(self) -> None:
        while self.running:
            if not self.run_once():
                time.sleep(self.poll_s)

    def stop(self) -> None:
        self.running = False


def start_worker(settings: Settings, *, worker_id: str | None = None) -> Worker:
    wid = worker_id or f"worker-{int(time.time()*1000)}"
    worker = Worker(settings, worker_id=wid)

    def _handle(_s, _f):
        worker.stop()

    try:
        signal.signal(signal.SIGTERM, _handle)
        signal.signal(signal.SIGINT, _handle)
    except Exception:
        pass
    worker.loop()
    return worker
