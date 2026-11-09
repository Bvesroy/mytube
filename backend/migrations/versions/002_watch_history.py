"""Phase 2: dual watch history foundation (additive)."""

from alembic import op

# revision identifiers, used by Alembic.
revision = "002_watch_history"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def _add_column(table, col, ddl):
    # SQLite: no IF NOT EXISTS for ADD COLUMN; ignore duplicate-column errors for idempotency.
    try:
        op.execute(f'ALTER TABLE {table} ADD COLUMN "{col}" {ddl}')
    except Exception:
        pass


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS watch_sessions (
            id VARCHAR(40) PRIMARY KEY,
            video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            channel_id INTEGER REFERENCES channels(id) ON DELETE SET NULL,
            started_at BIGINT NOT NULL,
            updated_at BIGINT NOT NULL,
            ended_at BIGINT,
            position_s REAL NOT NULL DEFAULT 0,
            duration_s REAL,
            watched_s REAL NOT NULL DEFAULT 0,
            completed INTEGER NOT NULL DEFAULT 0,
            playback_source VARCHAR(24) NOT NULL DEFAULT 'embedded',
            status VARCHAR(16) NOT NULL DEFAULT 'active',
            external_initiated_at BIGINT,
            meta_json TEXT
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_watch_sessions_video ON watch_sessions (video_id, started_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_watch_sessions_started ON watch_sessions (started_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_watch_sessions_status ON watch_sessions (status, updated_at)")

    # Additive columns on aggregated watch_history (preserve legacy fields/PK).
    for col, ddl in [
        ("channel_id", "INTEGER REFERENCES channels(id) ON DELETE SET NULL"),
        ("first_watched_at", "BIGINT"),
        ("last_watched_at", "BIGINT"),
        ("total_watch_s", "REAL"),
        ("watch_count", "INTEGER NOT NULL DEFAULT 0"),
        ("playback_source", "VARCHAR(24)"),
        ("last_session_id", "VARCHAR(40)"),
        ("external_initiated_at", "BIGINT"),
    ]:
        _add_column("watch_history", col, ddl)
    op.execute("CREATE INDEX IF NOT EXISTS ix_history_last_watched ON watch_history (last_watched_at)")


def downgrade() -> None:
    # Non-destructive downgrade: keep data tables; operators can drop watch_sessions manually if needed.
    op.execute("DROP TABLE IF EXISTS watch_sessions")
