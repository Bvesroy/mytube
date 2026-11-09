from alembic import op
import sqlalchemy as sa

revision = '001_initial'
down_revision = None
branch_labels = None
depends_on = None



def upgrade():
    op.execute("PRAGMA journal_mode=WAL")
    op.execute("PRAGMA foreign_keys=ON")

    op.execute('CREATE TABLE IF NOT EXISTS libraries (id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR(255) NOT NULL UNIQUE, source_path VARCHAR(1024), created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL, per_channel INTEGER NOT NULL DEFAULT 50, is_active BOOLEAN NOT NULL DEFAULT 1)')
    op.execute('CREATE UNIQUE INDEX IF NOT EXISTS ix_libraries_name ON libraries (name)')

    op.execute('CREATE TABLE IF NOT EXISTS categories (id INTEGER PRIMARY KEY AUTOINCREMENT, library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE, slug VARCHAR(120) NOT NULL, name VARCHAR(255) NOT NULL, position INTEGER NOT NULL DEFAULT 100, created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL)')
    op.execute('CREATE UNIQUE INDEX IF NOT EXISTS ix_categories_slug_lib ON categories (library_id, slug)')

    op.execute('CREATE TABLE IF NOT EXISTS channels (id INTEGER PRIMARY KEY AUTOINCREMENT, library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE, yt_id VARCHAR(32) NOT NULL UNIQUE, name VARCHAR(300) NOT NULL, url VARCHAR(1024), handle VARCHAR(128), avatar_url VARCHAR(1024), description TEXT, subscriber_count BIGINT, category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL, first_seen_at BIGINT NOT NULL, last_seen_at BIGINT NOT NULL, is_available BOOLEAN NOT NULL DEFAULT 1)')
    op.execute('CREATE UNIQUE INDEX IF NOT EXISTS ix_channels_yt_id ON channels (yt_id)')
    op.execute('CREATE INDEX IF NOT EXISTS ix_channels_library_id ON channels (library_id)')
    op.execute('CREATE INDEX IF NOT EXISTS ix_channels_library_name ON channels (library_id, name)')
    op.execute('CREATE INDEX IF NOT EXISTS ix_channels_category ON channels (category_id)')

    op.execute('CREATE TABLE IF NOT EXISTS videos (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE, yt_id VARCHAR(32) NOT NULL UNIQUE, title VARCHAR(600) NOT NULL, duration_s BIGINT, view_count BIGINT, published_at BIGINT, thumbnail_url VARCHAR(1024), description TEXT, metadata_hash VARCHAR(64) NOT NULL, first_seen_at BIGINT NOT NULL, last_seen_at BIGINT NOT NULL, is_available BOOLEAN NOT NULL DEFAULT 1, extra_json TEXT)')
    op.execute('CREATE UNIQUE INDEX IF NOT EXISTS ix_videos_yt_id ON videos (yt_id)')
    op.execute('CREATE INDEX IF NOT EXISTS ix_videos_channel_id ON videos (channel_id)')
    op.execute('CREATE INDEX IF NOT EXISTS ix_videos_duration ON videos (duration_s)')
    op.execute('CREATE INDEX IF NOT EXISTS ix_videos_view_count ON videos (view_count)')
    op.execute('CREATE INDEX IF NOT EXISTS ix_videos_published ON videos (published_at)')
    op.execute('CREATE INDEX IF NOT EXISTS ix_videos_library ON videos (channel_id, is_available)')
    op.execute('CREATE INDEX IF NOT EXISTS ix_videos_channel_recent ON videos (channel_id, published_at, id)')
    op.execute('CREATE INDEX IF NOT EXISTS ix_videos_recent ON videos (published_at, id)')

    op.execute('CREATE TABLE IF NOT EXISTS watch_history (video_id INTEGER PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE, watched_at BIGINT NOT NULL, engagement FLOAT NOT NULL DEFAULT 1.0, position_s FLOAT, completed BOOLEAN NOT NULL DEFAULT 0)')
    op.execute('CREATE INDEX IF NOT EXISTS ix_watch_history_at ON watch_history (watched_at)')

    op.execute('CREATE TABLE IF NOT EXISTS user_preferences (key VARCHAR(120) PRIMARY KEY, value_json TEXT NOT NULL DEFAULT "null", updated_at BIGINT NOT NULL)')

    op.execute('CREATE TABLE IF NOT EXISTS channel_ratings (channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE, rating SMALLINT NOT NULL, updated_at BIGINT NOT NULL, PRIMARY KEY (channel_id))')
    pass  # rating range enforced by app logic

    op.execute('CREATE TABLE IF NOT EXISTS hidden_channels (channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE, hidden_at BIGINT NOT NULL, PRIMARY KEY (channel_id))')
    op.execute('CREATE TABLE IF NOT EXISTS hidden_videos (video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE, hidden_at BIGINT NOT NULL, PRIMARY KEY (video_id))')
    op.execute('CREATE TABLE IF NOT EXISTS hidden_categories (category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE, hidden_at BIGINT NOT NULL, PRIMARY KEY (category_id))')

    op.execute('CREATE TABLE IF NOT EXISTS channel_category_overrides (channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE, category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE, updated_at BIGINT NOT NULL, PRIMARY KEY (channel_id, category_id))')

    op.execute('CREATE TABLE IF NOT EXISTS courses (id VARCHAR(12) PRIMARY KEY, name VARCHAR(200) NOT NULL, created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL)')
    op.execute('CREATE TABLE IF NOT EXISTS course_videos (course_id VARCHAR(12) NOT NULL REFERENCES courses(id) ON DELETE CASCADE, video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE, position INTEGER NOT NULL DEFAULT 0, added_at BIGINT NOT NULL, PRIMARY KEY (course_id, video_id))')
    op.execute('CREATE INDEX IF NOT EXISTS ix_course_videos_video ON course_videos (video_id)')

    op.execute('CREATE TABLE IF NOT EXISTS collections (id VARCHAR(40) PRIMARY KEY, name VARCHAR(200) NOT NULL, kind VARCHAR(24) NOT NULL DEFAULT "collection", created_at BIGINT NOT NULL)')
    op.execute('CREATE TABLE IF NOT EXISTS collection_videos (collection_id VARCHAR(40) NOT NULL REFERENCES collections(id) ON DELETE CASCADE, video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE, position INTEGER NOT NULL DEFAULT 0, added_at BIGINT NOT NULL, PRIMARY KEY (collection_id, video_id))')

    op.execute('CREATE TABLE IF NOT EXISTS bookmarks (video_id INTEGER PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE, created_at BIGINT NOT NULL DEFAULT 0)')
    op.execute('CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, video_id INTEGER REFERENCES videos(id) ON DELETE SET NULL, course_id VARCHAR(12) REFERENCES courses(id) ON DELETE SET NULL, body TEXT NOT NULL, timestamp_s BIGINT, created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL)')
    op.execute('CREATE INDEX IF NOT EXISTS ix_notes_video ON notes (video_id)')
    op.execute('CREATE INDEX IF NOT EXISTS ix_notes_course ON notes (course_id)')

    op.execute('CREATE TABLE IF NOT EXISTS tags (id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR(80) NOT NULL UNIQUE)')
    op.execute('CREATE TABLE IF NOT EXISTS video_tags (video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE, tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE, PRIMARY KEY (video_id, tag_id))')

    op.execute('CREATE TABLE IF NOT EXISTS background_jobs (id VARCHAR(12) PRIMARY KEY, type VARCHAR(64) NOT NULL, status VARCHAR(32) NOT NULL DEFAULT "queued", priority INTEGER NOT NULL DEFAULT 5, created_at BIGINT NOT NULL, started_at BIGINT, finished_at BIGINT, progress REAL NOT NULL DEFAULT 0, stage VARCHAR(64), payload_json TEXT NOT NULL DEFAULT "{}", result_json TEXT, error TEXT, attempts SMALLINT NOT NULL DEFAULT 0, max_attempts SMALLINT NOT NULL DEFAULT 3, cancel_requested BOOLEAN NOT NULL DEFAULT 0, worker_id VARCHAR(64), heartbeat_at BIGINT, dedupe_key VARCHAR(255), title VARCHAR(255))')
    op.execute('CREATE INDEX IF NOT EXISTS ix_jobs_status ON background_jobs (status)')
    op.execute('CREATE INDEX IF NOT EXISTS ix_jobs_type_status ON background_jobs (type, status)')
    op.execute('CREATE INDEX IF NOT EXISTS ix_jobs_worker ON background_jobs (worker_id)')
    op.execute('CREATE INDEX IF NOT EXISTS ix_jobs_claim ON background_jobs (status, priority, created_at)')
    op.execute('CREATE INDEX IF NOT EXISTS ix_jobs_stale ON background_jobs (status, heartbeat_at)')
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_active_dedupe ON background_jobs (type, dedupe_key) WHERE status IN ('queued','running') AND dedupe_key IS NOT NULL")

    op.execute('CREATE TABLE IF NOT EXISTS recommendation_events (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id VARCHAR(120) NOT NULL, request_id VARCHAR(12) NOT NULL, model VARCHAR(64) NOT NULL, items TEXT NOT NULL, at BIGINT NOT NULL, elapsed_ms INTEGER)')
    op.execute('CREATE INDEX IF NOT EXISTS ix_re_events_user ON recommendation_events (user_id, at)')
    op.execute('CREATE INDEX IF NOT EXISTS ix_re_events_at ON recommendation_events (at)')

    op.execute('CREATE TABLE IF NOT EXISTS recommendation_models (id INTEGER PRIMARY KEY AUTOINCREMENT, kind VARCHAR(24) NOT NULL, version INTEGER NOT NULL, weights_json TEXT NOT NULL, config_json TEXT, metrics_json TEXT, trained_at BIGINT NOT NULL, is_active BOOLEAN NOT NULL DEFAULT 0)')
    op.execute('CREATE UNIQUE INDEX IF NOT EXISTS uq_models_kind_version ON recommendation_models (kind, version)')

    op.execute('CREATE TABLE IF NOT EXISTS recommendation_experiments (id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR(120) NOT NULL, config_json TEXT NOT NULL, results_json TEXT, status VARCHAR(16) NOT NULL DEFAULT "running", started_at BIGINT NOT NULL, finished_at BIGINT)')
    op.execute('CREATE TABLE IF NOT EXISTS shelf_statistics (id INTEGER PRIMARY KEY AUTOINCREMENT, shelf VARCHAR(40) NOT NULL, day VARCHAR(10) NOT NULL, impressions INTEGER NOT NULL DEFAULT 0, clicks INTEGER NOT NULL DEFAULT 0)')
    op.execute('CREATE UNIQUE INDEX IF NOT EXISTS uq_shelf_day ON shelf_statistics (shelf, day)')

    op.execute('CREATE TABLE IF NOT EXISTS topic_vectors (video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE, model_id VARCHAR(40) NOT NULL, dim INTEGER NOT NULL, vector BLOB NOT NULL, norm REAL NOT NULL DEFAULT 1.0, PRIMARY KEY (video_id, model_id))')
    op.execute('CREATE TABLE IF NOT EXISTS channel_vectors (channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE, model_id VARCHAR(40) NOT NULL, dim INTEGER NOT NULL, vector BLOB NOT NULL, norm REAL NOT NULL DEFAULT 1.0, PRIMARY KEY (channel_id, model_id))')

    op.execute('CREATE TABLE IF NOT EXISTS transcripts (id INTEGER PRIMARY KEY AUTOINCREMENT, video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE UNIQUE, language VARCHAR(16), source VARCHAR(24), text TEXT NOT NULL, fetched_at BIGINT NOT NULL DEFAULT 0)')
    op.execute('CREATE TABLE IF NOT EXISTS transcript_segments (id INTEGER PRIMARY KEY AUTOINCREMENT, transcript_id INTEGER NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE, start_s REAL NOT NULL, end_s REAL NOT NULL, text TEXT NOT NULL)')
    op.execute('CREATE INDEX IF NOT EXISTS ix_segments_transcript ON transcript_segments (transcript_id, start_s)')

    op.execute('CREATE TABLE IF NOT EXISTS video_chapters (id INTEGER PRIMARY KEY AUTOINCREMENT, video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE, start_s INTEGER NOT NULL, title VARCHAR(300) NOT NULL)')
    op.execute('CREATE INDEX IF NOT EXISTS ix_chapters_video ON video_chapters (video_id, start_s)')

    op.execute('CREATE TABLE IF NOT EXISTS channel_sync_state (channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE, status VARCHAR(16) NOT NULL DEFAULT "never", last_attempt_at BIGINT, last_success_at BIGINT, last_error_stage VARCHAR(40), last_error TEXT, consecutive_failures INTEGER NOT NULL DEFAULT 0, next_attempt_at BIGINT, PRIMARY KEY (channel_id))')
    op.execute('CREATE INDEX IF NOT EXISTS ix_sync_due ON channel_sync_state (status, next_attempt_at)')

    op.execute('CREATE TABLE IF NOT EXISTS ingestion_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE, kind VARCHAR(24) NOT NULL, status VARCHAR(16) NOT NULL DEFAULT "running", started_at BIGINT NOT NULL, finished_at BIGINT, channels_ok INTEGER NOT NULL DEFAULT 0, channels_failed INTEGER NOT NULL DEFAULT 0, videos_seen INTEGER NOT NULL DEFAULT 0, videos_new INTEGER NOT NULL DEFAULT 0, videos_updated INTEGER NOT NULL DEFAULT 0, job_id VARCHAR(32), notes TEXT)')
    op.execute('CREATE INDEX IF NOT EXISTS ix_runs_started ON ingestion_runs (started_at)')

    op.execute('CREATE TABLE IF NOT EXISTS ingestion_errors (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL REFERENCES ingestion_runs(id) ON DELETE CASCADE, channel_yt_id VARCHAR(24), stage VARCHAR(40), message TEXT NOT NULL, retryable INTEGER NOT NULL DEFAULT 1, created_at BIGINT NOT NULL DEFAULT 0)')
    op.execute('CREATE INDEX IF NOT EXISTS ix_errors_run ON ingestion_errors (run_id)')

    op.execute('CREATE TABLE IF NOT EXISTS video_metadata_versions (id INTEGER PRIMARY KEY AUTOINCREMENT, video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE, captured_at BIGINT NOT NULL, metadata_hash VARCHAR(32) NOT NULL, title VARCHAR(500), duration_s BIGINT, view_count BIGINT, changed_fields VARCHAR(200))')
    op.execute('CREATE INDEX IF NOT EXISTS ix_vmv_video ON video_metadata_versions (video_id, captured_at)')

    op.execute('CREATE TABLE IF NOT EXISTS backups (id INTEGER PRIMARY KEY AUTOINCREMENT, kind VARCHAR(16) NOT NULL, path VARCHAR(600) NOT NULL, bytes INTEGER NOT NULL, sha256 VARCHAR(64) NOT NULL, created_at BIGINT NOT NULL, verified_at BIGINT, status VARCHAR(16) NOT NULL DEFAULT "ok")')

    op.execute('CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at BIGINT NOT NULL)')
    op.execute("INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (1, strftime('%s','now') * 1000)")

    # FTS5 virtual table + triggers (idempotent)
    op.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS video_fts USING fts5(
            title,
            description,
            tags,
            transcripts,
            content='videos',
            content_rowid='id',
            tokenize='unicode61 remove_diacritics 2'
        )
    """)
    op.execute("""
        CREATE TRIGGER IF NOT EXISTS videos_ai AFTER INSERT ON videos BEGIN
            INSERT INTO video_fts(rowid, title, description, tags, transcripts)
            VALUES (new.id, new.title, new.description, '', '');
        END
    """)
    op.execute("""
        CREATE TRIGGER IF NOT EXISTS videos_au AFTER UPDATE ON videos BEGIN
            INSERT INTO video_fts(rowid, title, description, tags, transcripts)
            VALUES (new.id, new.title, new.description, '', '');
        END
    """)
    op.execute("""
        CREATE TRIGGER IF NOT EXISTS videos_ad AFTER DELETE ON videos BEGIN
            INSERT INTO video_fts(video_fts, rowid, title, description, tags, transcripts)
            VALUES ('delete', old.id, old.title, old.description, '', '');
        END
    """)

    op.execute("DROP TABLE IF EXISTS recommendation_events")
    op.execute("DROP TABLE IF EXISTS background_jobs")
    op.execute("DROP TABLE IF EXISTS notes")
    op.execute("DROP TABLE IF EXISTS course_videos")
    op.execute("DROP TABLE IF EXISTS courses")
    op.execute("DROP TABLE IF EXISTS channel_category_overrides")
    op.execute("DROP TABLE IF EXISTS hidden_categories")
    op.execute("DROP TABLE IF EXISTS hidden_videos")
    op.execute("DROP TABLE IF EXISTS hidden_channels")
    op.execute("DROP TABLE IF EXISTS channel_ratings")
    op.execute("DROP TABLE IF EXISTS user_preferences")
    op.execute("DROP TABLE IF EXISTS watch_history")
    op.execute("DROP TABLE IF EXISTS videos")
    op.execute("DROP TABLE IF EXISTS channels")
    op.execute("DROP TABLE IF EXISTS categories")
    op.execute("DROP TABLE IF EXISTS libraries")
    op.execute("DROP TABLE IF EXISTS schema_version")
    # FTS5 virtual tables/triggers are created in upgrade(); drop real tables only.
    op.execute("DROP TABLE IF EXISTS schema_version")
    op.execute("DROP TABLE IF EXISTS video_metadata_versions")
    op.execute("DROP TABLE IF EXISTS ingestion_errors")
    op.execute("DROP TABLE IF EXISTS ingestion_runs")
    op.execute("DROP TABLE IF EXISTS channel_sync_state")
    op.execute("DROP TABLE IF EXISTS video_chapters")
    op.execute("DROP TABLE IF EXISTS transcript_segments")
    op.execute("DROP TABLE IF EXISTS transcripts")
    op.execute("DROP TABLE IF EXISTS channel_vectors")
    op.execute("DROP TABLE IF EXISTS topic_vectors")
    op.execute("DROP TABLE IF EXISTS shelf_statistics")
    op.execute("DROP TABLE IF EXISTS recommendation_experiments")
    op.execute("DROP TABLE IF EXISTS recommendation_models")
    op.execute("DROP TABLE IF EXISTS recommendation_events")
    op.execute("DROP TABLE IF EXISTS background_jobs")
    op.execute("DROP TABLE IF EXISTS video_tags")
    op.execute("DROP TABLE IF EXISTS tags")
    op.execute("DROP TABLE IF EXISTS bookmarks")
    op.execute("DROP TABLE IF EXISTS notes")
    op.execute("DROP TABLE IF EXISTS collection_videos")
    op.execute("DROP TABLE IF EXISTS collections")
    op.execute("DROP TABLE IF EXISTS course_videos")
    op.execute("DROP TABLE IF EXISTS courses")
    op.execute("DROP TABLE IF EXISTS channel_category_overrides")
    op.execute("DROP TABLE IF EXISTS hidden_categories")
    op.execute("DROP TABLE IF EXISTS hidden_videos")
    op.execute("DROP TABLE IF EXISTS hidden_channels")
    op.execute("DROP TABLE IF EXISTS channel_ratings")
    op.execute("DROP TABLE IF EXISTS user_preferences")
    op.execute("DROP TABLE IF EXISTS watch_history")
    op.execute("DROP TABLE IF EXISTS videos")
    op.execute("DROP TABLE IF EXISTS channels")
    op.execute("DROP TABLE IF EXISTS categories")
    op.execute("DROP TABLE IF EXISTS libraries")