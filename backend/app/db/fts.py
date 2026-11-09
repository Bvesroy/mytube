"""FTS5 index over video title + channel name + description.

A plain (not external-content) FTS5 table kept in sync by triggers, because a channel
rename must re-index all of that channel's videos whether it happens through the API,
the importer or a manual `UPDATE` in sqlite3.
"""

from __future__ import annotations

from sqlalchemy import Engine

FTS_TABLE = """
CREATE VIRTUAL TABLE IF NOT EXISTS video_fts USING fts5(
    title,
    channel_name,
    description,
    tokenize = 'unicode61 remove_diacritics 2'
)
"""

TRIGGERS = (
    """
    CREATE TRIGGER IF NOT EXISTS videos_ai AFTER INSERT ON videos BEGIN
        INSERT INTO video_fts(rowid, title, channel_name, description)
        VALUES (
            new.id,
            new.title,
            coalesce((SELECT name FROM channels WHERE id = new.channel_id), ''),
            coalesce(new.description, '')
        );
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS videos_au AFTER UPDATE OF title, description, channel_id ON videos
    BEGIN
        DELETE FROM video_fts WHERE rowid = old.id;
        INSERT INTO video_fts(rowid, title, channel_name, description)
        VALUES (
            new.id,
            new.title,
            coalesce((SELECT name FROM channels WHERE id = new.channel_id), ''),
            coalesce(new.description, '')
        );
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS channels_au_name AFTER UPDATE OF name ON channels BEGIN
        DELETE FROM video_fts WHERE rowid IN (SELECT id FROM videos WHERE channel_id = old.id);
        INSERT INTO video_fts(rowid, title, channel_name, description)
        SELECT v.id, v.title, new.name, coalesce(v.description, '')
        FROM videos v WHERE v.channel_id = old.id;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS videos_ad AFTER DELETE ON videos BEGIN
        DELETE FROM video_fts WHERE rowid = old.id;
    END
    """,
)


def fts_exists(engine: Engine) -> bool:
    with engine.connect() as conn:
        row = conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'video_fts'"
        ).first()
    return row is not None


def create_fts(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql(FTS_TABLE)
        for trigger in TRIGGERS:
            conn.exec_driver_sql(trigger)


def rebuild_fts(engine: Engine) -> int:
    """Repopulate from `videos`. Returns the number of indexed rows."""
    with engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM video_fts")
        conn.exec_driver_sql(
            """
            INSERT INTO video_fts(rowid, title, channel_name, description)
            SELECT v.id, v.title, coalesce(c.name, ''), coalesce(v.description, '')
            FROM videos v LEFT JOIN channels c ON c.id = v.channel_id
            """
        )
        return int(conn.exec_driver_sql("SELECT count(*) FROM video_fts").scalar_one())
