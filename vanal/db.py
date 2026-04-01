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
            CREATE TABLE IF NOT EXISTS clips (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                filename        TEXT NOT NULL,
                filepath        TEXT NOT NULL,
                file_hash       TEXT NOT NULL UNIQUE,
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

        # Additive migrations — safe to run repeatedly
        for sql in [
            "ALTER TABLE clips ADD COLUMN thumbnail_frame TEXT DEFAULT 'frame_0001.jpg'",
        ]:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # Column already exists
