"""Sync service stubs — real implementations to be wired by the sync adapter.

This module exists so the import/queue paths compile today and the worker can be
extended later with a real transcript/transcript fetcher, etc.
"""

from __future__ import annotations

from typing import Any


def enqueue_sync_run(session, *, library_id: int, channel_yt_ids: list[str] | None = None, dedupe_key: str | None = None) -> dict[str, Any]:
    """Enqueue a sync run job. The queue and dedupe are handled by `app.repositories.jobs`."""
    return {"library_id": library_id, "channel_yt_ids": channel_yt_ids, "dedupe_key": dedupe_key}
