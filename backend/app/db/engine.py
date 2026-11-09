"""SQLite engine, PRAGMAs and session handling.

One writer, many readers. WAL lets the API keep reading while the worker imports.
"""

from __future__ import annotations

import contextlib
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.db.fts import create_fts, fts_exists

PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA temp_store=MEMORY",
    "PRAGMA mmap_size=268435456",
)


def make_engine(url: str | Path, *, echo: bool = False) -> Engine:
    if isinstance(url, Path):
        url = str(url)
    if isinstance(url, str) and not url.startswith("sqlite:///") and not url.startswith("sqlite:"):
        url = f"sqlite:///{url}"
    engine = create_engine(
        url,
        echo=echo,
        future=True,
        connect_args={"check_same_thread": False, "timeout": 5.0},
    )

    @event.listens_for(engine, "connect")
    def _apply_pragmas(dbapi_connection, _record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        for pragma in PRAGMAS:
            try:
                cursor.execute(pragma)
            except sqlite3.DatabaseError:  # pragma: no cover - memory DB rejects WAL
                pass
        cursor.close()

    return engine


@dataclass
class Database:
    engine: Engine
    session_factory: sessionmaker[Session]



    @classmethod
    def open(cls, path: Path | str, *, echo: bool = False) -> Database:
        if isinstance(path, Path):
            path.parent.mkdir(parents=True, exist_ok=True)
        engine = make_engine(path, echo=echo)
        instance = cls(engine=engine, session_factory=sessionmaker(bind=engine, expire_on_commit=False))
        return instance

    def migrate(self) -> None:
        from app.db.models import Base

        Base.metadata.create_all(self.engine)
        _ensure_watch_history_phase2(self.engine)
        if not fts_exists(self.engine):
            create_fts(self.engine)
        from app.db.models import SchemaVersion
        with self.engine.begin() as conn:
            conn.execute(
                SchemaVersion.__table__.insert().prefix_with("OR IGNORE").values(version=1, applied_at=int(time.time() * 1000))
            )

    @contextlib.contextmanager
    def session(self) -> Iterator[Session]:
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def create_schema(self) -> None:
        from app.db.models import Base

        Base.metadata.create_all(self.engine)
        if not fts_exists(self.engine):
            create_fts(self.engine)
        from app.db.models import SchemaVersion
        with self.engine.begin() as conn:
            conn.execute(
                SchemaVersion.__table__.insert().prefix_with("OR IGNORE").values(version=1, applied_at=int(time.time() * 1000))
            )

    def close(self) -> None:
        self.engine.dispose()

    def integrity_check(self) -> str:
        with self.engine.connect() as conn:
            return str(conn.exec_driver_sql("PRAGMA integrity_check").scalar_one())

    def schema_version(self) -> int:
        try:
            with self.engine.connect() as conn:
                value = conn.exec_driver_sql("SELECT max(version) FROM schema_version").scalar_one()
            return int(value or 0)
        except OperationalError:
            return 0


def write_transaction(session: Session):
    """Acquire SQLite's write lock up front instead of failing mid-transaction."""
    return session.begin_nested()


def execute_with_retry(session: Session, fn, *, attempts: int = 5, base_delay: float = 0.05):
    """Retry a short write when another process holds the lock."""
    import time

    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            last = exc
            time.sleep(base_delay * (2**attempt))
    raise last  # type: ignore[misc]


def table_names(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
        return {row[0] for row in rows}


def count_rows(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return int(conn.exec_driver_sql(f'SELECT count(*) FROM "{table}"').scalar_one())


def optimize(engine: Engine) -> None:
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA optimize")
        conn.exec_driver_sql("INSERT INTO video_fts(video_fts) VALUES('optimize')")


def _ensure_watch_history_phase2(engine: Engine) -> None:
    """Additive Phase 2 dual-history columns for existing watch_history tables."""
    wanted = {
        "channel_id": "INTEGER REFERENCES channels(id) ON DELETE SET NULL",
        "first_watched_at": "INTEGER",
        "last_watched_at": "INTEGER",
        "total_watch_s": "REAL",
        "watch_count": "INTEGER NOT NULL DEFAULT 0",
        "playback_source": "VARCHAR(24)",
        "last_session_id": "VARCHAR(40)",
        "external_initiated_at": "INTEGER",
    }
    with engine.begin() as conn:
        try:
            cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(watch_history)")}
        except Exception:
            return
        for name, ddl in wanted.items():
            if name not in cols:
                conn.exec_driver_sql(f'ALTER TABLE watch_history ADD COLUMN "{name}" {ddl}')
        # indexes (idempotent)
        conn.exec_driver_sql('CREATE INDEX IF NOT EXISTS ix_history_last_watched ON watch_history (last_watched_at)')
        conn.exec_driver_sql('CREATE INDEX IF NOT EXISTS ix_watch_sessions_video ON watch_sessions (video_id, started_at)')
        conn.exec_driver_sql('CREATE INDEX IF NOT EXISTS ix_watch_sessions_started ON watch_sessions (started_at)')
        conn.exec_driver_sql('CREATE INDEX IF NOT EXISTS ix_watch_sessions_status ON watch_sessions (status, updated_at)')
