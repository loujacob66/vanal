import sqlite3
import os
from contextlib import contextmanager
from pathlib import Path

DATABASE_URL = os.getenv("DATABASE_URL", "data/vanal.db")


def _db_path() -> Path:
    path = Path(DATABASE_URL)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def get_conn():
    conn = sqlite3.connect(str(_db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def migrate():
    # Use individual execute() calls — executescript() issues an implicit COMMIT
    # before running which can corrupt WAL state on a fresh or crashed database.
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                email       TEXT UNIQUE NOT NULL,
                name        TEXT,
                picture_url TEXT,
                is_admin    INTEGER DEFAULT 0,
                created_at  TEXT DEFAULT (datetime('now')),
                updated_at  TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS clips (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                filename        TEXT NOT NULL,
                filepath        TEXT NOT NULL,
                file_hash       TEXT NOT NULL,
                duration        REAL,
                width           INTEGER,
                height          INTEGER,
                codec           TEXT,
                fps             REAL,
                has_audio       INTEGER DEFAULT 0,
                metadata_json   TEXT,
                synopsis        TEXT,
                raw_frames_json TEXT,
                transcript      TEXT,
                tags            TEXT DEFAULT '',
                notes           TEXT DEFAULT '',
                position        INTEGER,
                ai_rationale    TEXT,
                status          TEXT DEFAULT 'pending',
                error_msg       TEXT,
                created_at      TEXT DEFAULT (datetime('now')),
                updated_at      TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_clips_status   ON clips(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_clips_position ON clips(position)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_clips_hash     ON clips(file_hash)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_clips_owner   ON clips(owner_id)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_order_suggestions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at      TEXT DEFAULT (datetime('now')),
                suggestion_json TEXT NOT NULL,
                applied         INTEGER DEFAULT 0
            )
        """)
        try:
            # Standalone FTS5 table (stores its own text copy).
            # Avoids content-table sync issues that cause shadow table corruption
            # when writes are interrupted mid-transaction.
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS clips_fts USING fts5(
                    filename,
                    synopsis,
                    transcript,
                    tags,
                    notes,
                    tokenize='porter ascii'
                )
            """)
        except sqlite3.OperationalError:
            pass  # Already exists

        conn.execute("""
            CREATE TABLE IF NOT EXISTS clip_shares (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                clip_id     INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
                shared_by   INTEGER NOT NULL REFERENCES users(id),
                shared_with INTEGER NOT NULL REFERENCES users(id),
                created_at  TEXT DEFAULT (datetime('now')),
                UNIQUE(clip_id, shared_with)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_clip_shares_with ON clip_shares(shared_with)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_clip_shares_clip ON clip_shares(clip_id)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id),
                type        TEXT NOT NULL,
                message     TEXT NOT NULL,
                clip_id     INTEGER REFERENCES clips(id) ON DELETE CASCADE,
                is_read     INTEGER DEFAULT 0,
                created_at  TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, is_read)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS montages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                filename    TEXT NOT NULL,
                filepath    TEXT NOT NULL,
                owner_id    INTEGER NOT NULL REFERENCES users(id),
                size_mb     REAL,
                created_at  TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_montages_owner ON montages(owner_id)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS montage_clips (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                montage_id  INTEGER NOT NULL REFERENCES montages(id) ON DELETE CASCADE,
                clip_id     INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
                position    INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_montage_clips_montage ON montage_clips(montage_id)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS montage_shares (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                montage_id  INTEGER NOT NULL REFERENCES montages(id) ON DELETE CASCADE,
                shared_by   INTEGER NOT NULL REFERENCES users(id),
                shared_with INTEGER NOT NULL REFERENCES users(id),
                created_at  TEXT DEFAULT (datetime('now')),
                UNIQUE(montage_id, shared_with)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_montage_shares_with ON montage_shares(shared_with)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_montage_shares_montage ON montage_shares(montage_id)")

        # Additive migrations — safe to run repeatedly
        for sql in [
            "ALTER TABLE clips ADD COLUMN thumbnail_frame TEXT DEFAULT 'frame_0001.jpg'",
            "ALTER TABLE clips ADD COLUMN owner_id INTEGER REFERENCES users(id)",
            "ALTER TABLE clips ADD COLUMN processing_stage TEXT",
            "ALTER TABLE clips ADD COLUMN title TEXT",
            "ALTER TABLE ai_order_suggestions ADD COLUMN owner_id INTEGER REFERENCES users(id)",
            "ALTER TABLE notifications ADD COLUMN montage_id INTEGER REFERENCES montages(id) ON DELETE CASCADE",
        ]:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # Column already exists

        # Migration: remove UNIQUE constraint on file_hash (allow same video per user)
        # Check if the old unique index still exists on file_hash alone
        schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='clips'"
        ).fetchone()["sql"]
        if "file_hash       TEXT NOT NULL UNIQUE" in schema:
            conn.execute("""
                CREATE TABLE clips_new (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename        TEXT NOT NULL,
                    filepath        TEXT NOT NULL,
                    file_hash       TEXT NOT NULL,
                    duration        REAL,
                    width           INTEGER,
                    height          INTEGER,
                    codec           TEXT,
                    fps             REAL,
                    has_audio       INTEGER DEFAULT 0,
                    metadata_json   TEXT,
                    synopsis        TEXT,
                    raw_frames_json TEXT,
                    transcript      TEXT,
                    tags            TEXT DEFAULT '',
                    notes           TEXT DEFAULT '',
                    position        INTEGER,
                    ai_rationale    TEXT,
                    status          TEXT DEFAULT 'pending',
                    error_msg       TEXT,
                    created_at      TEXT DEFAULT (datetime('now')),
                    updated_at      TEXT DEFAULT (datetime('now')),
                    thumbnail_frame TEXT DEFAULT 'frame_0001.jpg',
                    owner_id        INTEGER REFERENCES users(id),
                    processing_stage TEXT,
                    title           TEXT
                )
            """)
            conn.execute("INSERT INTO clips_new SELECT * FROM clips")
            conn.execute("DROP TABLE clips")
            conn.execute("ALTER TABLE clips_new RENAME TO clips")
            # Recreate indexes
            conn.execute("CREATE INDEX IF NOT EXISTS idx_clips_status   ON clips(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_clips_position ON clips(position)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_clips_hash     ON clips(file_hash)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_clips_owner    ON clips(owner_id)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_clips_hash_owner ON clips(file_hash, owner_id)")
