"""FastAPI dependency-injection layer.

Request-scoped session, settings, and small helpers for pagination cursors.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.engine import Database, make_engine


def _engine_factory(settings: Settings) -> Database:
    db = Database(settings.db_path)
    return db


def get_db(request: Request) -> Session:
    """Return a request-scoped session. Stored on request.state for cleanup."""
    if hasattr(request.state, "db_session") and request.state.db_session is not None:
        return request.state.db_session
    db: Database = request.app.state.db
    session = db.session_factory()
    request.state.db_session = session
    return session



def get_settings_dep() -> Settings:
    return get_settings()


# request-id middleware value
def request_id(request: Request) -> str:
    return request.headers.get("x-request-id", str(uuid.uuid4())[:12])


# pagination helpers
def decode_cursor(cursor: str) -> dict:
    import base64
    import json
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        return json.loads(base64.urlsafe_b64decode(padded.encode()))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("invalid cursor") from exc


def encode_cursor(values: dict) -> str:
    import base64
    import json
    raw = json.dumps(values, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")
