"""Sync/import job handlers (Phase 5 foundation).

Provides a testable, incremental (idempotent upsert) sync/import path that works
without network/yt-dlp by accepting channel payloads. A real yt-dlp fetch adapter
can be layered behind the same job contract later; it must not use undocumented
YouTube APIs or collect passwords/cookies.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.db.models import BackgroundJob
from app.services.jobs import JobHandler, register
from app.services.jobs_service import JobService


def _canceled(session: Session, job: BackgroundJob) -> bool:
    session.refresh(job)
    return bool(int(job.cancel_requested or 0)) or job.status == "canceled"


def run_sync_import(session: Session, job: BackgroundJob, payload: dict[str, Any]) -> dict[str, Any]:
    """Incremental import/sync from supplied channel payloads (no network).

    Payload: {library_name?, channels?: [...], source_path?: "local.json"}
    Idempotent via importer upserts/metadata hashes.
    """
    svc = JobService(session)
    svc.heartbeat(job.id, progress=0.05, stage="loading")
    session.commit()

    channels = payload.get("channels")
    source_path = payload.get("source_path")
    library_name = payload.get("library_name") or "main"
    if channels is None and source_path:
        p = Path(str(source_path))
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            channels = data if isinstance(data, list) else [data]
        else:
            return {"ok": False, "error": f"source_path not found: {p}", "channels_upserted": 0, "videos_upserted": 0}
    channels = channels or []
    if not isinstance(channels, list):
        channels = [channels]

    if _canceled(session, job):
        return {"ok": False, "canceled": True}
    svc.heartbeat(job.id, progress=0.15, stage="importing")
    session.commit()

    # Reuse the importer (idempotent/incremental upserts). It manages its own session.
    from app.services.importer import import_channels

    sm = sessionmaker(bind=session.get_bind(), future=True)
    result = import_channels(sm, channels, library_name=library_name, source_path=str(source_path) if source_path else None)

    # Phase 5: best-effort ChannelSyncState/IngestionRun tracking (incremental sync observability).
    sync_state_touched = 0
    try:
        from sqlalchemy import select
        from app.db.models import Channel, ChannelSyncState, IngestionRun, Library, now_ms
        now = now_ms()
        lib = session.scalar(select(Library).where(Library.name == library_name))
        if lib:
            run = IngestionRun(library_id=lib.id, kind="import", status="canceled" if _canceled(session, job) else "success",
                               job_id=job.id, finished_at=now,
                               channels_ok=getattr(result, "channels_upserted", 0),
                               videos_seen=getattr(result, "videos_upserted", 0))
            session.add(run)
        for raw in channels:
            yt = (raw or {}).get("id") or (raw or {}).get("channelId") or (raw or {}).get("channel_id")
            if not yt: continue
            ch = session.scalar(select(Channel).where(Channel.yt_id == str(yt)))
            if not ch: continue
            st = session.get(ChannelSyncState, ch.id)
            if st is None:
                st = ChannelSyncState(channel_id=ch.id); session.add(st)
            st.status = "ok"; st.last_attempt_at = now; st.last_success_at = now
            st.last_error = None; st.last_error_stage = None; st.consecutive_failures = 0
            sync_state_touched += 1
        session.commit()
    except Exception:
        session.rollback()  # tracking is best-effort; do not fail a successful import

    if _canceled(session, job):
        return {"ok": False, "canceled": True, "channels_upserted": result.channels_upserted, "videos_upserted": result.videos_upserted, "sync_state_touched": sync_state_touched}
    svc.heartbeat(job.id, progress=0.95, stage="finalizing")
    session.commit()
    return {
        "ok": True,
        "library_name": library_name,
        "channels_read": result.channels_read,
        "channels_upserted": result.channels_upserted,
        "videos_upserted": result.videos_upserted,
        "sync_state_touched": sync_state_touched,
        "incremental": True,
        "note": "Idempotent upsert sync from supplied payload; no undocumented YouTube APIs used.",
    }


def run_sync_ytdlp(session: Session, job: BackgroundJob, payload: dict[str, Any]) -> dict[str, Any]:
    """Optional yt-dlp-backed sync adapter hook.

    Truthful behavior: if yt-dlp is unavailable or fetching is not configured,
    returns a clear non-destructive result. Never collects passwords/cookies or
    uses undocumented YouTube APIs.
    """
    svc = JobService(session)
    svc.heartbeat(job.id, progress=0.05, stage="checking_adapter")
    session.commit()
    if not shutil.which("yt-dlp"):
        return {
            "ok": False,
            "adapter": "yt-dlp",
            "available": False,
            "error": "yt-dlp not found on PATH; install/configure adapter to run network sync.",
            "channels": payload.get("channel_ids") or payload.get("channels") or [],
        }
    # Safety/minimalism: do not execute arbitrary network sync in this local-first
    # test context without explicit configuration. Provide truthful placeholder.
    return {
        "ok": False,
        "adapter": "yt-dlp",
        "available": True,
        "error": "yt-dlp adapter present but network fetch execution is not enabled in this build context; use sync_import with supplied payloads for verified incremental import.",
    }


register("sync_import", JobHandler(run=run_sync_import))
register("sync_ytdlp", JobHandler(run=run_sync_ytdlp))
register("noop", JobHandler(run=lambda s, j, p: {"ok": True, "noop": True}))
