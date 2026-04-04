import json
import os
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from vanal import db
from web.api.auth import require_auth

router = APIRouter()


@router.get("/ingest/status")
def ingest_status():
    """Return live ingest progress counts, including the currently-processing clip."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) as count FROM clips GROUP BY status"
        ).fetchall()
        counts = {r["status"]: r["count"] for r in rows}

        current = conn.execute(
            "SELECT filename, updated_at FROM clips WHERE status='processing' ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()

    total = sum(counts.values())
    done = counts.get("done", 0)

    # Treat 'processing' clips as stale if not updated in the last 10 minutes
    # (happens when ingest is killed mid-run)
    processing = counts.get("processing", 0)
    if processing > 0 and current:
        from datetime import datetime, timezone
        try:
            last = datetime.fromisoformat(current["updated_at"])
            age = (datetime.utcnow() - last).total_seconds()
            if age > 600:  # 10 minutes
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


@router.get("/clips")
def list_clips(
    search: str | None = Query(None),
    tag: str | None = Query(None),
    sort: str = Query("filename"),
):
    """List all clips, with optional search and tag filter."""
    with db.get_conn() as conn:
        if search:
            # Use FTS for keyword search
            rows = conn.execute(
                """SELECT c.* FROM clips c
                   JOIN clips_fts f ON c.id = f.rowid
                   WHERE clips_fts MATCH ?
                   ORDER BY CASE WHEN ? = 'position' THEN c.position ELSE NULL END,
                           c.filename""",
                (search, sort),
            ).fetchall()
        elif tag:
            rows = conn.execute(
                """SELECT * FROM clips
                   WHERE ',' || tags || ',' LIKE ?
                   ORDER BY CASE WHEN ? = 'position' THEN position ELSE NULL END,
                           filename""",
                (f"%,{tag},%", sort),
            ).fetchall()
        else:
            order = "position, filename" if sort == "position" else "filename"
            rows = conn.execute(
                f"SELECT * FROM clips ORDER BY {order}"
            ).fetchall()

        return [_row_to_dict(r) for r in rows]


@router.get("/clips/{clip_id}/video")
def stream_clip(clip_id: int, request: Request):
    """Stream the original video file for in-browser playback."""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT filepath FROM clips WHERE id = ?", (clip_id,)
        ).fetchone()
        if not row:
            from fastapi.responses import JSONResponse
            return JSONResponse({"error": "Clip not found"}, status_code=404)

    path = Path(row["filepath"])
    if not path.exists():
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": f"File not found: {path}"}, status_code=404)

    # Determine media type from extension
    ext = path.suffix.lower()
    media_types = {
        ".mp4": "video/mp4", ".mov": "video/quicktime",
        ".avi": "video/x-msvideo", ".mkv": "video/x-matroska",
        ".webm": "video/webm", ".m4v": "video/mp4",
    }
    media_type = media_types.get(ext, "video/mp4")

    return FileResponse(path, media_type=media_type, filename=path.name)


@router.get("/clips/{clip_id}")
def get_clip(clip_id: int):
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
        if not row:
            return {"error": "Clip not found"}, 404
        return _row_to_dict(row)


class ClipUpdate(BaseModel):
    position: int | None = None
    tags: str | None = None
    notes: str | None = None
    transcript: str | None = None


@router.patch("/clips/{clip_id}")
def update_clip(clip_id: int, update: ClipUpdate, _auth=Depends(require_auth)):
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
        if not row:
            return {"error": "Clip not found"}, 404

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
        row = conn.execute("SELECT id, filename, filepath FROM clips WHERE id = ?", (clip_id,)).fetchone()
        if not row:
            return {"error": "Clip not found"}, 404
        clip = dict(row)

    from pathlib import Path
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
        # Update FTS
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
        row = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
        if not row:
            return {"error": "Clip not found"}, 404
        clip = dict(row)

    # Reconstruct frame descriptions from stored JSON
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
    """Re-extract frames for a clip that's missing them (no full reprocess needed)."""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT filepath, file_hash, duration FROM clips WHERE id = ?", (clip_id,)
        ).fetchone()
        if not row:
            from fastapi.responses import JSONResponse
            return JSONResponse({"error": "Clip not found"}, status_code=404)

    filepath = Path(row["filepath"])
    if not filepath.exists():
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": f"File not found: {filepath}"}, status_code=404)

    frames_dir = Path(os.getenv("FRAMES_DIR", "frames")) / row["file_hash"]
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
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/clips/{clip_id}/frames")
def list_clip_frames(clip_id: int):
    """Return sorted list of extracted frame filenames available for this clip."""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT file_hash, thumbnail_frame FROM clips WHERE id = ?", (clip_id,)
        ).fetchone()
        if not row:
            return {"error": "Clip not found"}, 404

    frames_dir = Path(os.getenv("FRAMES_DIR", "frames")) / row["file_hash"]
    if not frames_dir.exists():
        return {"frames": [], "thumbnail_frame": row["thumbnail_frame"] or "frame_0001.jpg"}

    frames = sorted(f.name for f in frames_dir.glob("frame_*.jpg"))
    return {
        "frames": frames,
        "thumbnail_frame": row["thumbnail_frame"] or "frame_0001.jpg",
    }


class ThumbnailRequest(BaseModel):
    frame: str  # e.g. "frame_0003.jpg"


@router.post("/clips/{clip_id}/thumbnail")
def set_thumbnail(clip_id: int, req: ThumbnailRequest, _auth=Depends(require_auth)):
    """Set the preferred thumbnail frame for a clip."""
    import re
    if not re.match(r'^frame_\d+\.jpg$', req.frame):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Invalid frame name")

    with db.get_conn() as conn:
        row = conn.execute("SELECT file_hash FROM clips WHERE id = ?", (clip_id,)).fetchone()
        if not row:
            return {"error": "Clip not found"}, 404

        # Verify the frame actually exists on disk
        frame_path = Path(os.getenv("FRAMES_DIR", "frames")) / row["file_hash"] / req.frame
        if not frame_path.exists():
            from fastapi import HTTPException
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
        row = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
        if not row:
            return {"error": "Clip not found"}, 404
        clip = dict(row)

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

    # Merge with any existing user tags
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
        for item in request.items:
            conn.execute(
                "UPDATE clips SET position = ?, updated_at = datetime('now') WHERE id = ?",
                (item.position, item.id),
            )
    return {"ok": True, "updated": len(request.items)}


def _row_to_dict(row) -> dict:
    d = dict(row)
    # Parse JSON fields for the API response
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
    # Don't send raw JSON strings to frontend
    d.pop("raw_frames_json", None)
    d.pop("metadata_json", None)
    return d
