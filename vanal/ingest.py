import hashlib
import json
import os
import shutil
import time
from pathlib import Path

from vanal import db
from vanal.extractor import probe_video, extract_frames
from vanal.vision import describe_frames

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".flv", ".wmv"}
FRAMES_DIR = os.getenv("FRAMES_DIR", "frames")
MAX_FRAMES = int(os.getenv("MAX_FRAMES_PER_CLIP", "8"))
FRAME_WIDTH = int(os.getenv("FRAME_WIDTH", "512"))
ENABLE_TRANSCRIPTION = os.getenv("ENABLE_TRANSCRIPTION", "false").lower() == "true"
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "vanal-outputs")).resolve()


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def _get_existing(conn, file_hash: str) -> dict | None:
    row = conn.execute(
        "SELECT id, status FROM clips WHERE file_hash = ?", (file_hash,)
    ).fetchone()
    return dict(row) if row else None


def _upsert_pending(conn, filename: str, filepath: str, file_hash: str, owner_id: int | None = None) -> int:
    existing = conn.execute(
        "SELECT id FROM clips WHERE file_hash = ?", (file_hash,)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE clips SET filepath = ?, filename = ?, updated_at = datetime('now') WHERE id = ?",
            (filepath, filename, existing["id"]),
        )
        return existing["id"]
    cursor = conn.execute(
        "INSERT INTO clips (filename, filepath, file_hash, status, owner_id) VALUES (?, ?, ?, 'pending', ?)",
        (filename, filepath, file_hash, owner_id),
    )
    return cursor.lastrowid


def _update_fts(conn, clip_id: int, filename: str, synopsis: str, transcript: str, tags: str, notes: str):
    conn.execute("DELETE FROM clips_fts WHERE rowid = ?", (clip_id,))
    conn.execute(
        "INSERT INTO clips_fts(rowid, filename, synopsis, transcript, tags, notes) VALUES (?,?,?,?,?,?)",
        (clip_id, filename, synopsis or "", transcript or "", tags or "", notes or ""),
    )


def process_file(
    path: Path,
    retry_errors: bool = False,
    keep_frames: bool = False,
    delay_secs: float = 1.0,
    reprocess_all: bool = False,
    owner_id: int | None = None,
) -> str:
    """
    Process a single video file. Returns status: 'done', 'skipped', or 'error'.
    """
    filename = path.name
    filepath = str(path.resolve())

    print(f"\n[{filename}]")

    # Compute hash
    print("  Computing hash...")
    file_hash = sha256_file(path)

    with db.get_conn() as conn:
        existing = _get_existing(conn, file_hash)

        if existing:
            if existing["status"] == "done" and not reprocess_all:
                # Update filepath in case mount point changed
                conn.execute(
                    "UPDATE clips SET filepath = ?, updated_at = datetime('now') WHERE id = ?",
                    (filepath, existing["id"]),
                )
                print("  Skipping (already processed)")
                return "skipped"
            if existing["status"] == "error" and not retry_errors and not reprocess_all:
                print("  Skipping (previous error; use --retry-errors to retry)")
                return "skipped"

        clip_id = _upsert_pending(conn, filename, filepath, file_hash, owner_id=owner_id)
        conn.execute(
            "UPDATE clips SET status = 'processing', error_msg = NULL, updated_at = datetime('now') WHERE id = ?",
            (clip_id,),
        )

    frames_dir = Path(FRAMES_DIR) / file_hash
    try:
        # 1. Probe
        print("  Probing metadata...")
        meta = probe_video(path)

        # 2. Extract frames — reuse existing unless reprocessing (which may want more frames)
        existing_frames = sorted(frames_dir.glob("frame_*.jpg")) if frames_dir.exists() else []
        if existing_frames and not reprocess_all:
            frame_paths = existing_frames
            print(f"  Reusing {len(frame_paths)} existing frames")
        else:
            if reprocess_all and frames_dir.exists():
                shutil.rmtree(frames_dir, ignore_errors=True)
                print(f"  Cleared old frames for re-extraction")
            print(f"  Extracting frames (duration={meta['duration']:.1f}s)...")
            frame_paths = extract_frames(
                path,
                frames_dir,
                duration=meta["duration"],
                max_frames=MAX_FRAMES,
                frame_width=FRAME_WIDTH,
            )
            print(f"  Extracted {len(frame_paths)} frames")

        # 3. Optional transcription
        transcript = None
        if ENABLE_TRANSCRIPTION and meta["has_audio"]:
            print("  Transcribing audio (whisper)...")
            from vanal.transcribe import transcribe_audio
            transcript = transcribe_audio(path)
            if transcript:
                print(f"  Transcript:\n    {transcript}")
            else:
                print("  No speech detected")

        # 4. Vision analysis
        from vanal.vision import VISION_MODEL, TEXT_MODEL
        print(f"  Analyzing frames (vision={VISION_MODEL}, text={TEXT_MODEL})...")
        vision_result = describe_frames(frame_paths, filename, transcript=transcript)
        synopsis = vision_result.get("synopsis", "")
        frame_descriptions = vision_result.get("frames", [])
        auto_tags = vision_result.get("tags", [])
        print(f"  Synopsis:\n    {synopsis}")
        if auto_tags:
            print(f"  Tags: {', '.join(auto_tags)}")

        # 5. Save to DB — merge auto tags with any existing user-set tags
        with db.get_conn() as conn:
            existing_tags_row = conn.execute(
                "SELECT tags FROM clips WHERE id = ?", (clip_id,)
            ).fetchone()
            existing_tags = [
                t.strip() for t in (existing_tags_row["tags"] or "").split(",")
                if t.strip()
            ] if existing_tags_row else []

            # Union: keep user tags, add new auto tags
            merged = list(dict.fromkeys(existing_tags + auto_tags))
            tags_str = ", ".join(merged)

            conn.execute(
                """UPDATE clips SET
                    duration = ?, width = ?, height = ?, codec = ?, fps = ?,
                    has_audio = ?, metadata_json = ?,
                    synopsis = ?, raw_frames_json = ?, transcript = ?,
                    tags = ?,
                    status = 'done', error_msg = NULL,
                    updated_at = datetime('now')
                WHERE id = ?""",
                (
                    meta["duration"], meta["width"], meta["height"],
                    meta["codec"], meta["fps"], meta["has_audio"],
                    meta["metadata_json"],
                    synopsis,
                    json.dumps([
                        {"index": i, "description": d}
                        for i, d in enumerate(frame_descriptions)
                    ]),
                    transcript,
                    tags_str,
                    clip_id,
                ),
            )
            _update_fts(conn, clip_id, filename, synopsis, transcript, tags_str, "")

        print("  Done.")
        return "done"

    except Exception as e:
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE clips SET status = 'error', error_msg = ?, updated_at = datetime('now') WHERE id = ?",
                (str(e), clip_id),
            )
        print(f"  ERROR: {e}")
        return "error"

    finally:
        # Always keep frames — needed for thumbnail picker and UI display
        if delay_secs > 0:
            time.sleep(delay_secs)


def ingest_directory(
    directory: str | Path,
    retry_errors: bool = False,
    keep_frames: bool = False,
    delay_secs: float = 1.0,
    base_path_remap: str | None = None,
    reprocess_all: bool = False,
    owner_id: int | None = None,
):
    """Scan a directory (or single file) and process all video files."""
    directory = Path(directory)
    if not directory.exists():
        raise FileNotFoundError(f"Path not found: {directory}")

    # Handle path remapping for NAS mount changes
    if base_path_remap:
        old_base, new_base = base_path_remap.split(":", 1)
        _apply_path_remap(old_base, new_base)
        return

    db.migrate()

    # Support single-file mode
    if directory.is_file():
        if directory.suffix.lower() not in VIDEO_EXTENSIONS:
            print(f"Not a recognised video file: {directory}")
            return
        video_files = [directory]
    else:
        video_files = sorted([
            f for f in directory.rglob("*")
            if f.suffix.lower() in VIDEO_EXTENSIONS and f.is_file()
        ])

    # Filter out anything inside the output dir
    video_files = [f for f in video_files if OUTPUT_DIR not in f.resolve().parents and f.resolve() != OUTPUT_DIR]

    if not video_files:
        print(f"No video files found in {directory}")
        return

    print(f"Found {len(video_files)} video file(s)")

    if reprocess_all:
        print("  --reprocess-all: all clips will be re-analyzed with current settings")

    stats = {"done": 0, "skipped": 0, "error": 0}
    for i, video_path in enumerate(video_files, 1):
        print(f"\n--- [{i}/{len(video_files)}] ---", end="")
        status = process_file(
            video_path,
            retry_errors=retry_errors,
            keep_frames=keep_frames,
            delay_secs=delay_secs,
            reprocess_all=reprocess_all,
            owner_id=owner_id,
        )
        stats[status] = stats.get(status, 0) + 1

    print(f"\n\nDone. Processed: {stats['done']}, Skipped: {stats['skipped']}, Errors: {stats['error']}")


def _apply_path_remap(old_base: str, new_base: str):
    """Update filepath column for all clips whose path starts with old_base."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, filepath FROM clips WHERE filepath LIKE ?",
            (f"{old_base}%",),
        ).fetchall()
        count = 0
        for row in rows:
            new_path = new_base + row["filepath"][len(old_base):]
            conn.execute(
                "UPDATE clips SET filepath = ?, updated_at = datetime('now') WHERE id = ?",
                (new_path, row["id"]),
            )
            count += 1
    print(f"Remapped {count} file path(s) from '{old_base}' to '{new_base}'")
