"""My Tube 3.0: shared intelligence foundation (additive).

Creates the knowledge/learning/discovery tables and adds additive columns to
`transcripts` and `video_chapters` for existing databases. Purely additive:
existing library, history and recommendation data are untouched.
"""

from alembic import op

from app.db.intel_fts import create_intel_fts
from app.db.models import Base

revision = "003_intelligence"
down_revision = "002_watch_history"
branch_labels = None
depends_on = None


def _add_column(table: str, col: str, ddl: str) -> None:
    try:
        op.execute(f'ALTER TABLE "{table}" ADD COLUMN "{col}" {ddl}')
    except Exception:
        pass


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind)
    for col, ddl in [
        ("seq", "INTEGER NOT NULL DEFAULT 0"),
        ("content_hash", "VARCHAR(32) NOT NULL DEFAULT ''"),
        ("normalized_hash", "VARCHAR(32) NOT NULL DEFAULT ''"),
        ("word_count", "INTEGER NOT NULL DEFAULT 0"),
        ("segment_count", "INTEGER NOT NULL DEFAULT 0"),
        ("source_kind", "VARCHAR(24)"),
        ("timing_source", "VARCHAR(16) NOT NULL DEFAULT 'none'"),
        ("provenance_json", "TEXT"),
        ("updated_at", "INTEGER"),
    ]:
        _add_column("transcripts", col, ddl)
    for col, ddl in [
        ("source", "VARCHAR(16) NOT NULL DEFAULT 'creator'"),
        ("confidence", "REAL NOT NULL DEFAULT 0"),
        ("position", "INTEGER NOT NULL DEFAULT 0"),
        ("generated_at", "INTEGER"),
        ("evidence_json", "TEXT"),
    ]:
        _add_column("video_chapters", col, ddl)
    create_intel_fts(bind.engine)


def downgrade() -> None:
    # Non-destructive: knowledge tables hold user-created material. Drop only the
    # derived FTS indexes, which are rebuilt automatically on next startup.
    op.execute("DROP TABLE IF EXISTS transcript_fts")
    op.execute("DROP TABLE IF EXISTS knowledge_fts")
