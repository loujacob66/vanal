import json
import os
import threading
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel

from vanal import db
from vanal.ingest import VIDEO_EXTENSIONS
from web.api.auth import require_auth

UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "uploads"))
MAX_UPLOAD_SIZE_MB = int(os.getenv("MAX_UPLOAD_SIZE_MB", "500"))

router = APIRouter()


# ── Ownership helpers ──────────────────────────────────────────────
def _owner_where(user: dict) -> tuple[str, list]:
    """Return (SQL fragment, params) to scope queries by owner_id.  Admin sees all."""
    if user["is_admin"]:
        return "", []
    return " AND owner_id = ?", [user["id"]]


def get_user_clip(conn, clip_id: int, user: dict, columns: str = "*") -> dict:
    """Fetch a clip and verify ownership.  Admin can access any clip.  Raises 404/403."""
    row = conn.execute(f"SELECT {columns} FROM clips WHERE id = ?", (clip_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Clip not found")
    clip = dict(row)
    if not user["is_admin"] and clip.get("owner_id") != user["id"]:
        raise HTTPException(status_code=403, detail="Access denied")
    return clip


# ── Routes ─────────────────────────────────────────────────────────
@router.get("/ingest/status")
def ingest_status(_auth=Depends(require_auth)):
    """Return live ingest progress counts, including the currently-processing clip."""
    owner_frag, owner_params = _owner_where(_auth)
    with db.get_conn() as conn:
        rows = conn.execute(
            f"SELECT status, COUNT(*) as count FROM clips WHERE 1=1 {owner_frag} GROUP BY status",
            owner_params,
        ).fetchall()
        counts = {r["status"]: r["count"] for r in rows}

        current = conn.execute(
            f"SELECT filename, updated_at FROM clips WHERE status='processing' {owner_frag} ORDER BY updated_at DESC LIMIT 1",
            owner_params,
        ).fetchone()

    total = sum(counts.values())
    done = counts.get("done", 0)

    # Treat 'processing' clips as stale if not updated in the last 10 minutes
    processing = counts.get("processing", 0)
    if processing > 0 and current:
        from datetime import datetime, timezone
        try:
            last = datetime.fromisoformat(current["updated_at"])
            age = (datetime.utcnow() - last).total_seconds()
            if age > 600:
                processing = 0
                current = None
        except Exception:
            pass

    return {
        "total": total,
        "done": done,
        "error": counts.get("error", 0),
        "processing": processing,
        "pending": counts.get("pending", 0),
        "current": dict(current) if current else None,
        "pct": round(done / total * 100) if total else 0,
    }


@router.get("/clips/owners")
def list_owners(_auth=Depends(require_auth)):
    """Return list of users who own clips (admin only, used for owner filter)."""
    if not _auth["is_admin"]:
        return []
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT u.id, u.name, u.email, COUNT(c.id) AS clip_count
               FROM users u
               JOIN clips c ON c.owner_id = u.id
               GROUP BY u.id
               ORDER BY u.name""",
        ).fetchall()
    return [
        {"id": r["id"], "name": r["name"], "email_prefix": r["email"].split("@")[0], "clip_count": r["clip_count"]}
        for r in rows
    ]


@router.get("/clips")
def list_clips(
    search: str | None = Query(None),
    tag: str | None = Query(None),
    sort: str = Query("filename"),
    owner: str | None = Query(None),
    _auth=Depends(require_auth),
):
    """List clips owned by the current user (admin sees all).
    Admin can filter by owner email prefix via ?owner=name.
    """
    # Use table-aliased owner filter for the JOIN queries below
    if _auth["is_admin"] and owner:
        owner_frag = " AND u.email LIKE ?"
        owner_params = [f"{owner}@%"]
    elif _auth["is_admin"]:
        owner_frag, owner_params = "", []
    else:
        owner_frag = " AND c.owner_id = ?"
        owner_params = [_auth["id"]]

    with db.get_conn() as conn:
        if search:
            rows = conn.execute(
                f"""SELECT c.*, u.name AS owner_name, u.email AS owner_email
                   FROM clips c
                   LEFT JOIN users u ON c.owner_id = u.id
                   JOIN clips_fts f ON c.id = f.rowid
                   WHERE clips_fts MATCH ? {owner_frag}
                   ORDER BY CASE WHEN ? = 'position' THEN c.position ELSE NULL END,
                           c.filename""",
                [search] + owner_params + [sort],
            ).fetchall()
        elif tag:
            rows = conn.execute(
                f"""SELECT c.*, u.name AS owner_name, u.email AS owner_email
                   FROM clips c
                   LEFT JOIN users u ON c.owner_id = u.id
                   WHERE ',' || c.tags || ',' LIKE ? {owner_frag}
                   ORDER BY CASE WHEN ? = 'position' THEN c.position ELSE NULL END,
                           c.filename""",
                [f"%,{tag},%"] + owner_params + [sort],
            ).fetchall()
        else:
            order = "c.position, c.filename" if sort == "position" else "c.filename"
            rows = conn.execute(
                f"""SELECT c.*, u.name AS owner_name, u.email AS owner_email
                   FROM clips c
                   LEFT JOIN users u ON c.owner_id = u.id
                   WHERE 1=1 {owner_frag} ORDER BY {order}""",
                owner_params,
            ).fetchall()

        return [_row_to_dict(r) for r in rows]


@router.get("/clips/{clip_id}/video")
def stream_clip(clip_id: int, request: Request, _auth=Depends(require_auth)):
    """Stream the original video file for in-browser playback."""
    with db.get_conn() as conn:
        clip = get_user_clip(conn, clip_id, _auth, columns="filepath")

    path = Path(clip["filepath"])
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")

    ext = path.suffix.lower()
    media_types = {
        ".mp4": "video/mp4", ".mov": "video/quicktime",
        ".avi": "video/x-msvideo", ".mkv": "video/x-matroska",
        ".webm": "video/webm", ".m4v": "video/mp4",
    }
    media_type = media_types.get(ext, "video/mp4")
    return FileResponse(path, media_type=media_type, filename=path.name)


@router.get("/clips/{clip_id}")
def get_clip(clip_id: int, _auth=Depends(require_auth)):
    with db.get_conn() as conn:
        clip = get_user_clip(conn, clip_id, _auth)
        return _row_to_dict(clip)


class ClipUpdate(BaseModel):
    position: int | None = None
    tags: str | None = None
    notes: str | None = None
    transcript: str | None = None


@router.patch("/clips/{clip_id}")
def update_clip(clip_id: int, update: ClipUpdate, _auth=Depends(require_auth)):
    with db.get_conn() as conn:
        get_user_clip(conn, clip_id, _auth, columns="id, owner_id")

        fields = []
        values = []
        if update.position is not None:
            fields.append("position = ?")
            values.append(update.position)
        if update.transcript is not None:
            fields.append("transcript = ?")
            values.append(update.transcript)
        if update.tags is not None:
            fields.append("tags = ?")
            values.append(update.tags)
        if update.notes is not None:
            fields.append("notes = ?")
            values.append(update.notes)

        if fields:
            fields.append("updated_at = datetime('now')")
            values.append(clip_id)
            conn.execute(
                f"UPDATE clips SET {', '.join(fields)} WHERE id = ?",
                values,
            )

            # Update FTS
            updated = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
            conn.execute("DELETE FROM clips_fts WHERE rowid = ?", (clip_id,))
            conn.execute(
                "INSERT INTO clips_fts(rowid, filename, synopsis, transcript, tags, notes) VALUES (?,?,?,?,?,?)",
                (clip_id, updated["filename"], updated["synopsis"] or "",
                 updated["transcript"] or "", updated["tags"] or "", updated["notes"] or ""),
            )

        return {"ok": True}


@router.post("/clips/{clip_id}/transcribe")
def transcribe_clip(clip_id: int, _auth=Depends(require_auth)):
    """Run Whisper transcription on a single clip on-demand."""
    with db.get_conn() as conn:
        clip = get_user_clip(conn, clip_id, _auth, columns="id, filename, filepath, owner_id")

    if not Path(clip["filepath"]).exists():
        return {"error": f"File not found at {clip['filepath']}"}

    try:
        from vanal.transcribe import transcribe_audio
        transcript = transcribe_audio(clip["filepath"])
    except ImportError:
        return {"error": "openai-whisper is not installed. Run: pip install openai-whisper"}

    if not transcript:
        return {"ok": True, "transcript": None, "message": "No speech detected"}

    with db.get_conn() as conn:
        conn.execute(
            "UPDATE clips SET transcript = ?, updated_at = datetime('now') WHERE id = ?",
            (transcript, clip_id),
        )
        row = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
        conn.execute("DELETE FROM clips_fts WHERE rowid = ?", (clip_id,))
        conn.execute(
            "INSERT INTO clips_fts(rowid, filename, synopsis, transcript, tags, notes) VALUES (?,?,?,?,?,?)",
            (clip_id, row["filename"], row["synopsis"] or "", transcript,
             row["tags"] or "", row["notes"] or ""),
        )

    return {"ok": True, "transcript": transcript}


@router.post("/clips/{clip_id}/regenerate-synopsis")
def regenerate_synopsis(clip_id: int, _auth=Depends(require_auth)):
    """Re-synthesize the synopsis using existing frame descriptions, transcript, and notes."""
    with db.get_conn() as conn:
        clip = get_user_clip(conn, clip_id, _auth)

    frame_descriptions = []
    if clip.get("raw_frames_json"):
        try:
            frames = json.loads(clip["raw_frames_json"])
            frame_descriptions = [f.get("description", "") for f in frames]
        except json.JSONDecodeError:
            pass

    if not frame_descriptions:
        return {"error": "No frame descriptions found — re-run ingest first"}

    from vanal.vision import _ollama_generate, TEXT_MODEL
    import re

    descriptions_text = "\n".join(
        f"Frame {i + 1}: {d}" for i, d in enumerate(frame_descriptions)
    )
    transcript = clip.get("transcript") or ""
    notes = clip.get("notes") or ""

    transcript_section = f'\n\nAudio transcript:\n"{transcript}"\n' if transcript else ""
    notes_section = (
        f"\n\nAdditional context / steering notes from the editor:\n{notes}\n"
        if notes else ""
    )

    prompt = (
        f"These are descriptions of {len(frame_descriptions)} frames from a short "
        f"AI-generated video clip named '{clip['filename']}':\n\n"
        f"{descriptions_text}"
        f"{transcript_section}"
        f"{notes_section}\n\n"
        "Using all of the above, write a 2-3 sentence synopsis of this clip that "
        "captures its subject matter, mood, and key message. "
        "Respond with ONLY the synopsis text, nothing else."
    )

    synopsis = _ollama_generate(TEXT_MODEL, prompt).strip()

    with db.get_conn() as conn:
        conn.execute(
            "UPDATE clips SET synopsis = ?, updated_at = datetime('now') WHERE id = ?",
            (synopsis, clip_id),
        )
        conn.execute("DELETE FROM clips_fts WHERE rowid = ?", (clip_id,))
        conn.execute(
            "INSERT INTO clips_fts(rowid, filename, synopsis, transcript, tags, notes) VALUES (?,?,?,?,?,?)",
            (clip_id, clip["filename"], synopsis, transcript,
             clip.get("tags") or "", notes),
        )

    return {"ok": True, "synopsis": synopsis}


@router.post("/clips/{clip_id}/extract-frames")
def extract_clip_frames(clip_id: int, _auth=Depends(require_auth)):
    """Re-extract frames for a clip that's missing them."""
    with db.get_conn() as conn:
        clip = get_user_clip(conn, clip_id, _auth, columns="id, filepath, file_hash, duration, owner_id")

    filepath = Path(clip["filepath"])
    if not filepath.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {filepath}")

    frames_dir = Path(os.getenv("FRAMES_DIR", "frames")) / clip["file_hash"]
    frames_dir.mkdir(parents=True, exist_ok=True)

    from vanal.extractor import extract_frames, probe_video
    max_frames = int(os.getenv("MAX_FRAMES_PER_CLIP", "8"))
    frame_width = int(os.getenv("FRAME_WIDTH", "512"))

    try:
        meta = probe_video(filepath)
        frame_paths = extract_frames(
            filepath, frames_dir,
            duration=meta["duration"],
            max_frames=max_frames,
            frame_width=frame_width,
        )
        frames = sorted(f.name for f in frame_paths)
        return {"ok": True, "frames": frames}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/clips/{clip_id}/frames")
def list_clip_frames(clip_id: int, _auth=Depends(require_auth)):
    """Return sorted list of extracted frame filenames available for this clip."""
    with db.get_conn() as conn:
        clip = get_user_clip(conn, clip_id, _auth, columns="id, file_hash, thumbnail_frame, owner_id")

    frames_dir = Path(os.getenv("FRAMES_DIR", "frames")) / clip["file_hash"]
    if not frames_dir.exists():
        return {"frames": [], "thumbnail_frame": clip["thumbnail_frame"] or "frame_0001.jpg"}

    frames = sorted(f.name for f in frames_dir.glob("frame_*.jpg"))
    return {
        "frames": frames,
        "thumbnail_frame": clip["thumbnail_frame"] or "frame_0001.jpg",
    }


class ThumbnailRequest(BaseModel):
    frame: str


@router.post("/clips/{clip_id}/thumbnail")
def set_thumbnail(clip_id: int, req: ThumbnailRequest, _auth=Depends(require_auth)):
    """Set the preferred thumbnail frame for a clip."""
    import re
    if not re.match(r'^frame_\d+\.jpg$', req.frame):
        raise HTTPException(status_code=400, detail="Invalid frame name")

    with db.get_conn() as conn:
        clip = get_user_clip(conn, clip_id, _auth, columns="id, file_hash, owner_id")

        frame_path = Path(os.getenv("FRAMES_DIR", "frames")) / clip["file_hash"] / req.frame
        if not frame_path.exists():
            raise HTTPException(status_code=404, detail="Frame not found on disk")

        conn.execute(
            "UPDATE clips SET thumbnail_frame = ?, updated_at = datetime('now') WHERE id = ?",
            (req.frame, clip_id),
        )
    return {"ok": True, "thumbnail_frame": req.frame}


@router.post("/clips/{clip_id}/regenerate-tags")
def regenerate_tags(clip_id: int, _auth=Depends(require_auth)):
    """Re-generate content tags from stored frame descriptions, synopsis, and transcript."""
    with db.get_conn() as conn:
        clip = get_user_clip(conn, clip_id, _auth)

    frame_descriptions = []
    if clip.get("raw_frames_json"):
        try:
            frames = json.loads(clip["raw_frames_json"])
            frame_descriptions = [f.get("description", "") for f in frames]
        except json.JSONDecodeError:
            pass

    if not frame_descriptions:
        return {"error": "No frame descriptions found — re-run ingest first"}

    from vanal.vision import generate_tags
    auto_tags = generate_tags(
        frame_descriptions,
        clip.get("synopsis") or "",
        clip.get("transcript"),
        clip["filename"],
    )

    if not auto_tags:
        return {"error": "Tag generation failed — check that Ollama is running and the text model is available"}

    existing = [t.strip() for t in (clip.get("tags") or "").split(",") if t.strip()]
    merged = list(dict.fromkeys(existing + auto_tags))
    tags_str = ", ".join(merged)

    with db.get_conn() as conn:
        conn.execute(
            "UPDATE clips SET tags = ?, updated_at = datetime('now') WHERE id = ?",
            (tags_str, clip_id),
        )
        conn.execute("DELETE FROM clips_fts WHERE rowid = ?", (clip_id,))
        conn.execute(
            "INSERT INTO clips_fts(rowid, filename, synopsis, transcript, tags, notes) VALUES (?,?,?,?,?,?)",
            (clip_id, clip["filename"], clip.get("synopsis") or "",
             clip.get("transcript") or "", tags_str, clip.get("notes") or ""),
        )

    return {"ok": True, "tags": tags_str}


class ReorderItem(BaseModel):
    id: int
    position: int


class ReorderRequest(BaseModel):
    items: list[ReorderItem]


@router.post("/clips/reorder")
def reorder_clips(request: ReorderRequest, _auth=Depends(require_auth)):
    with db.get_conn() as conn:
        # Verify ownership of all clips being reordered
        for item in request.items:
            get_user_clip(conn, item.id, _auth, columns="id, owner_id")
        for item in request.items:
            conn.execute(
                "UPDATE clips SET position = ?, updated_at = datetime('now') WHERE id = ?",
                (item.position, item.id),
            )
    return {"ok": True, "updated": len(request.items)}


# ── Delete ─────────────────────────────────────────────────────────
@router.delete("/clips/{clip_id}")
def delete_clip(clip_id: int, _auth=Depends(require_auth)):
    """Delete a clip, its video file, and extracted frames. Owners can delete their own clips; admins can delete any."""
    import shutil

    with db.get_conn() as conn:
        clip = get_user_clip(conn, clip_id, _auth, columns="id, filename, filepath, file_hash, owner_id, status")

        # Don't allow deleting a clip that's currently processing
        if clip["status"] == "processing":
            raise HTTPException(status_code=409, detail="Cannot delete a clip that is currently processing")

        # Delete from DB
        conn.execute("DELETE FROM clips_fts WHERE rowid = ?", (clip_id,))
        conn.execute("DELETE FROM clips WHERE id = ?", (clip_id,))

    # Clean up video file
    video_path = Path(clip["filepath"])
    if video_path.exists():
        video_path.unlink(missing_ok=True)

    # Clean up extracted frames
    if clip["file_hash"]:
        frames_dir = Path(os.getenv("FRAMES_DIR", "frames")) / clip["file_hash"]
        if frames_dir.exists():
            shutil.rmtree(frames_dir, ignore_errors=True)

    return {"ok": True, "filename": clip["filename"]}


# ── Upload ─────────────────────────────────────────────────────────
@router.post("/clips/upload")
async def upload_clip(file: UploadFile = File(...), _auth=Depends(require_auth)):
    """Upload a video file, save to per-user dir, and kick off background processing."""
    # Validate extension
    ext = Path(file.filename or "").suffix.lower()
    if ext not in VIDEO_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {ext}")

    # Read file and check size
    contents = await file.read()
    size_mb = len(contents) / 1_048_576
    if size_mb > MAX_UPLOAD_SIZE_MB:
        raise HTTPException(status_code=400, detail=f"File too large ({size_mb:.0f} MB, max {MAX_UPLOAD_SIZE_MB} MB)")

    # Save to per-user upload directory (named by email prefix)
    email_prefix = _auth["email"].split("@")[0].replace("/", "_").replace("\\", "_")
    user_dir = UPLOAD_DIR / email_prefix
    user_dir.mkdir(parents=True, exist_ok=True)

    # Handle duplicate filenames
    safe_name = file.filename or "upload.mp4"
    dest = user_dir / safe_name
    counter = 1
    while dest.exists():
        stem = Path(safe_name).stem
        dest = user_dir / f"{stem}_{counter}{ext}"
        counter += 1

    dest.write_bytes(contents)

    # Create pending DB row immediately so it shows in the queue
    from vanal.ingest import sha256_file, _upsert_pending
    file_hash = sha256_file(dest)
    with db.get_conn() as conn:
        _upsert_pending(conn, dest.name, str(dest.resolve()), file_hash, owner_id=_auth["id"])

    # Kick the background worker to pick it up
    _kick_processing_worker()

    return {"ok": True, "filename": dest.name, "size_mb": round(size_mb, 1)}


# ── Background processing worker ─────────────────────────────────
_worker_lock = threading.Lock()
_worker_running = False


def _kick_processing_worker():
    """Ensure the background worker is running. Only one runs at a time."""
    global _worker_running
    with _worker_lock:
        if _worker_running:
            return  # already processing the queue
        _worker_running = True

    def worker():
        global _worker_running
        from vanal.ingest import process_file
        try:
            while True:
                # Pick next pending clip
                with db.get_conn() as conn:
                    row = conn.execute(
                        "SELECT id, filepath FROM clips WHERE status = 'pending' ORDER BY created_at ASC LIMIT 1"
                    ).fetchone()
                if not row:
                    break  # queue empty
                process_file(Path(row["filepath"]), delay_secs=0)
        finally:
            with _worker_lock:
                _worker_running = False

    threading.Thread(target=worker, daemon=True).start()


# ── Processing queue ───────────────────────────────────────────────
@router.get("/processing/queue")
def processing_queue(_auth=Depends(require_auth)):
    """Return processing queue info.

    Admin sees ALL queued clips with full details.
    Regular users see their own clips with details + count of others ahead.
    """
    with db.get_conn() as conn:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        if _auth["is_admin"]:
            # Admin sees everything with full details
            all_rows = conn.execute(
                """SELECT c.id, c.filename, c.status, c.processing_stage,
                          c.created_at, c.updated_at,
                          c.owner_id, u.name AS owner_name, u.email AS owner_email
                   FROM clips c
                   LEFT JOIN users u ON c.owner_id = u.id
                   WHERE c.status IN ('pending', 'processing')
                   ORDER BY CASE c.status WHEN 'processing' THEN 0 ELSE 1 END, c.updated_at ASC""",
            ).fetchall()
            return {
                "own": [dict(r) for r in all_rows],
                "others_ahead": 0,
                "server_time": now,
            }
        else:
            # Own clips — full details
            own_rows = conn.execute(
                """SELECT id, filename, status, processing_stage, created_at, updated_at
                   FROM clips
                   WHERE status IN ('pending', 'processing') AND owner_id = ?
                   ORDER BY CASE status WHEN 'processing' THEN 0 ELSE 1 END, updated_at ASC""",
                (_auth["id"],),
            ).fetchall()

            # Count of other users' clips that are ahead (processing or queued before user's earliest)
            own_earliest = None
            if own_rows:
                own_earliest = own_rows[0]["updated_at"]

            if own_earliest:
                # Items ahead = other users' clips that started before or are currently processing
                others_ahead = conn.execute(
                    """SELECT COUNT(*) FROM clips
                       WHERE status IN ('pending', 'processing')
                         AND owner_id != ?
                         AND (status = 'processing' OR updated_at <= ?)""",
                    (_auth["id"], own_earliest),
                ).fetchone()[0]
            else:
                # User has nothing queued — show total backlog so they know wait time
                others_ahead = conn.execute(
                    "SELECT COUNT(*) FROM clips WHERE status IN ('pending', 'processing') AND owner_id != ?",
                    (_auth["id"],),
                ).fetchone()[0]

            return {
                "own": [dict(r) for r in own_rows],
                "others_ahead": others_ahead,
                "server_time": now,
            }


def _row_to_dict(row) -> dict:
    d = dict(row) if not isinstance(row, dict) else row
    if d.get("raw_frames_json"):
        try:
            d["raw_frames"] = json.loads(d["raw_frames_json"])
        except json.JSONDecodeError:
            d["raw_frames"] = []
    else:
        d["raw_frames"] = []
    if d.get("metadata_json"):
        try:
            d["metadata"] = json.loads(d["metadata_json"])
        except json.JSONDecodeError:
            d["metadata"] = {}
    else:
        d["metadata"] = {}
    d.pop("raw_frames_json", None)
    d.pop("metadata_json", None)
    return d
