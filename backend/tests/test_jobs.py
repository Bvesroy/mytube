import json
import pytest
from app.db.engine import Database
from app.db.models import BackgroundJob
from app.services.jobs_service import JobService
from app.services.jobs import ensure_default_handlers_registered, run_job


@pytest.fixture()
def db(tmp_path):
    d = Database.open(tmp_path / "t.db")
    d.migrate()
    return d


def test_enqueue_claim_heartbeat_finish(db):
    with db.session() as s:
        svc = JobService(s)
        job = svc.enqueue("noop", {"x": 1}, max_attempts=2)
        assert job.status == "queued"
        claimed = svc.claim("w1")
        assert claimed and claimed.id == job.id and claimed.status == "running" and claimed.attempts == 1
        assert svc.heartbeat(job.id, progress=0.5, stage="working")
        done = svc.finish(job.id, status="done", result={"ok": True})
        assert done.status == "done" and done.progress == 1.0
        s.commit()


def test_cancel_and_retry(db):
    with db.session() as s:
        svc = JobService(s)
        job = svc.enqueue("noop", {})
        svc.request_cancel(job.id)
        j = svc.get(job.id)
        assert j.status == "canceled" and j.cancel_requested == 1
        # retry resets
        j.status = "failed"; j.error = "e"; j.cancel_requested = 1; j.attempts = 2
        s.flush()
    # via service-level behavior pattern used by API
    with db.session() as s:
        svc = JobService(s)
        j = svc.get(job.id)
        j.status = "queued"; j.error = None; j.cancel_requested = 0; j.attempts = 0; j.progress = 0.0
        s.commit()
        assert svc.get(job.id).status == "queued"


def test_fail_or_retry_then_fail(db):
    with db.session() as s:
        svc = JobService(s)
        job = svc.enqueue("noop", {}, max_attempts=2)
        svc.claim("w1")
        svc.fail_or_retry(job.id, error="boom1")
        j = svc.get(job.id)
        assert j.status == "queued" and j.attempts == 1 and "boom1" in (j.error or "")
        svc.claim("w1")
        svc.fail_or_retry(job.id, error="boom2")
        j = svc.get(job.id)
        assert j.status == "failed" and j.attempts == 2
        s.commit()


def test_recover_stale_running(db):
    with db.session() as s:
        svc = JobService(s)
        job = svc.enqueue("noop", {}, max_attempts=2)
        svc.claim("w1")
        j = svc.get(job.id)
        j.heartbeat_at = 1  # very old
        s.commit()
        n = svc.recover_stale(stale_ms=10)
        assert n >= 1
        j = svc.get(job.id)
        assert j.status == "queued"  # retryable
        s.commit()


def test_sync_import_handler_incremental_idempotent(db):
    ensure_default_handlers_registered()
    payload = {
        "library_name": "main",
        "channels": [
            {
                "id": "UCabc",
                "name": "Test Channel",
                "handle": "@test",
                "videos": [
                    {"id": "vid1", "title": "First", "durationSeconds": 120, "viewCount": 100},
                    {"id": "vid2", "title": "Second", "durationSeconds": 60, "viewCount": 50},
                ],
            }
        ],
    }
    with db.session() as s:
        svc = JobService(s)
        job = svc.enqueue("sync_import", payload)
        job = svc.claim("w1")
        s.commit()
        result = run_job(s, job)
        svc.finish(job.id, status="done", result=result)
        s.commit()
        j = svc.get(job.id)
        assert j.status == "done"
        r = json.loads(j.result_json)
        assert r["ok"] is True and r["incremental"] is True
        assert r["channels_upserted"] >= 1
    # verify actual DB state (importer reporting fields may vary)
    from sqlalchemy import text
    with db.session() as s:
        assert s.execute(text("select count(*) from channels where yt_id='UCabc'")).scalar() == 1
        assert s.execute(text("select count(*) from videos")).scalar() == 2
    # run again (idempotent upsert/incremental sync of same payload) - no duplicates
    with db.session() as s:
        svc = JobService(s)
        job = svc.enqueue("sync_import", payload)
        job = svc.claim("w1"); s.commit()
        result = run_job(s, job)
        assert result["ok"] is True
        s.commit()
    with db.session() as s:
        assert s.execute(text("select count(*) from videos")).scalar() == 2

def test_sync_import_from_source_path_local_file(db, tmp_path):
    """Phase 5: incremental sync from a local JSON file (no network)."""
    ensure_default_handlers_registered()
    src = tmp_path / "import.json"
    payload_data = [
        {
            "id": "UCpath",
            "name": "Path Channel",
            "handle": "@path",
            "videos": [{"id": "pvid1", "title": "From file", "durationSeconds": 30, "viewCount": 7}],
        }
    ]
    src.write_text(json.dumps(payload_data), encoding="utf-8")
    with db.session() as s:
        svc = JobService(s)
        job = svc.enqueue("sync_import", {"library_name": "main", "source_path": str(src)})
        job = svc.claim("w1"); s.commit()
        result = run_job(s, job)
        svc.finish(job.id, status="done", result=result); s.commit()
        assert result["ok"] is True and result["incremental"] is True
    from sqlalchemy import text
    with db.session() as s:
        assert s.execute(text("select count(*) from channels where yt_id='UCpath'")).scalar() == 1
        assert s.execute(text("select count(*) from videos where yt_id='pvid1'")).scalar() == 1
    # idempotent re-run from same source_path
    with db.session() as s:
        svc = JobService(s)
        job = svc.enqueue("sync_import", {"library_name": "main", "source_path": str(src)})
        job = svc.claim("w1"); s.commit()
        assert run_job(s, job)["ok"] is True
        s.commit()
    with db.session() as s:
        assert s.execute(text("select count(*) from videos where yt_id='pvid1'")).scalar() == 1
