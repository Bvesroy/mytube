"""FTS5 indexes for transcript segments and knowledge extracts (My Tube 3.0).

Kept in sync by the knowledge services rather than triggers: segments/extracts are
rewritten as a unit, so a delete+insert per video is simpler and always consistent.
The tables are *plain* (not external-content) FTS5 tables so the source row can be
deleted and re-added independently.
"""

from __future__ import annotations

from sqlalchemy import Engine

TRANSCRIPT_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts USING fts5(
    text,
    video_title,
    channel_name,
    tokenize = 'unicode61 remove_diacritics 2'
)
"""

KNOWLEDGE_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    content,
    kind,
    video_title,
    channel_name,
    tokenize = 'unicode61 remove_diacritics 2'
)
"""


def intel_fts_exists(engine: Engine) -> bool:
    with engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('transcript_fts', 'knowledge_fts')"
        ).fetchall()
    return len(rows) == 2


def create_intel_fts(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql(TRANSCRIPT_FTS)
        conn.exec_driver_sql(KNOWLEDGE_FTS)


def rebuild_transcript_fts(engine: Engine) -> int:
    with engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM transcript_fts")
        conn.exec_driver_sql(
            """
            INSERT INTO transcript_fts(rowid, text, video_title, channel_name)
            SELECT s.id, s.text, coalesce(v.title, ''), coalesce(c.name, '')
              FROM transcript_segments s
              JOIN transcripts t ON t.id = s.transcript_id
              JOIN videos v ON v.id = t.video_id
              LEFT JOIN channels c ON c.id = v.channel_id
            """
        )
        return int(conn.exec_driver_sql("SELECT count(*) FROM transcript_fts").scalar_one())


def rebuild_knowledge_fts(engine: Engine) -> int:
    with engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM knowledge_fts")
        conn.exec_driver_sql(
            """
            INSERT INTO knowledge_fts(rowid, content, kind, video_title, channel_name)
            SELECT e.id, e.content, e.kind, coalesce(v.title, ''), coalesce(c.name, '')
              FROM knowledge_extracts e
              JOIN videos v ON v.id = e.video_id
              LEFT JOIN channels c ON c.id = v.channel_id
             WHERE e.rejected = 0
            """
        )
        return int(conn.exec_driver_sql("SELECT count(*) FROM knowledge_fts").scalar_one())
