import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from vanal import db
from web.api.auth import require_auth

router = APIRouter()

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "vanal-outputs"))


def _ensure_output_dir():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


@router.get("/export/json")
def export_json():
    """Export ordered clip list as JSON."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, filename, filepath, synopsis, position, tags, duration "
            "FROM clips WHERE status = 'done' "
            "ORDER BY position NULLS LAST, filename"
        ).fetchall()
    return [dict(r) for r in rows]


@router.get("/export/ffmpeg-concat")
def export_ffmpeg_concat():
    """Export as an ffmpeg concat demuxer manifest."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT filepath, duration FROM clips WHERE status = 'done' "
            "ORDER BY position NULLS LAST, filename"
        ).fetchall()

    if not rows:
        return PlainTextResponse("# No clips found", status_code=404)

    lines = ["ffconcat version 1.0", ""]
    for row in rows:
        safe_path = row["filepath"].replace("'", "'\\''")
        lines.append(f"file '{safe_path}'")
        if row["duration"]:
            lines.append(f"duration {row['duration']:.3f}")
    lines.append("")

    return PlainTextResponse("\n".join(lines), media_type="text/plain")


class RenderRequest(BaseModel):
    clip_ids: list[int]
    filename: str = ""        # optional base name; auto-generated if blank
    reencode: bool = False    # True = libx264/aac re-encode (slower, always compatible)


@router.post("/export/render")
def render_montage(req: RenderRequest, _auth=Depends(require_auth)):
    """Concatenate selected clips into a streamable MP4 saved to OUTPUT_DIR."""
    if not req.clip_ids:
        return JSONResponse({"error": "No clips selected"}, status_code=400)

    with db.get_conn() as conn:
        placeholders = ",".join("?" * len(req.clip_ids))
        rows = conn.execute(
            f"SELECT id, filepath FROM clips WHERE id IN ({placeholders})",
            req.clip_ids,
        ).fetchall()

    # Preserve the requested order
    id_to_path = {r["id"]: r["filepath"] for r in rows}
    paths = []
    missing = []
    for cid in req.clip_ids:
        p = id_to_path.get(cid)
        if not p:
            missing.append(cid)
            continue
        if not Path(p).exists():
            missing.append(cid)
            continue
        paths.append(p)

    if not paths:
        return JSONResponse({"error": "None of the selected clips have accessible files"}, status_code=400)

    _ensure_output_dir()

    # Build output filename
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = req.filename.strip() or "montage"
    # Sanitise: keep alphanumeric, dashes, underscores
    safe_base = "".join(c if c.isalnum() or c in "-_" else "_" for c in base)
    out_filename = f"{safe_base}_{ts}.mp4"
    out_path = OUTPUT_DIR / out_filename

    # Write concat list to a temp file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        tmp_list = f.name
        for p in paths:
            # ffconcat requires single-quoted paths; escape any single quotes
            escaped = p.replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")

    try:
        if req.reencode:
            cmd = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0", "-i", tmp_list,
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                str(out_path),
            ]
        else:
            cmd = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0", "-i", tmp_list,
                "-c", "copy",
                "-movflags", "+faststart",
                str(out_path),
            ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            return JSONResponse({
                "error": "ffmpeg failed",
                "details": result.stderr[-2000:],  # last 2k chars of stderr
            }, status_code=500)
    except subprocess.TimeoutExpired:
        return JSONResponse({"error": "ffmpeg timed out after 10 minutes"}, status_code=500)
    finally:
        Path(tmp_list).unlink(missing_ok=True)

    size_mb = round(out_path.stat().st_size / 1_048_576, 1)
    return {
        "ok": True,
        "filename": out_filename,
        "size_mb": size_mb,
        "clip_count": len(paths),
        "skipped": missing,
    }


@router.get("/outputs")
def list_outputs():
    """List all rendered montages in OUTPUT_DIR."""
    if not OUTPUT_DIR.exists():
        return []

    files = sorted(OUTPUT_DIR.glob("*.mp4"), key=lambda f: f.stat().st_mtime, reverse=True)
    result = []
    for f in files:
        stat = f.stat()
        result.append({
            "filename": f.name,
            "size_mb": round(stat.st_size / 1_048_576, 1),
            "created_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
        })
    return result


@router.get("/outputs/{filename}/video")
def stream_output(filename: str):
    """Stream a rendered montage for in-browser playback / download."""
    # Safety: no path traversal
    if "/" in filename or "\\" in filename or ".." in filename:
        return JSONResponse({"error": "Invalid filename"}, status_code=400)

    path = OUTPUT_DIR / filename
    if not path.exists():
        return JSONResponse({"error": "File not found"}, status_code=404)

    return FileResponse(path, media_type="video/mp4", filename=filename)


@router.delete("/outputs/{filename}")
def delete_output(filename: str, _auth=Depends(require_auth)):
    """Delete a rendered montage."""
    if "/" in filename or "\\" in filename or ".." in filename:
        return JSONResponse({"error": "Invalid filename"}, status_code=400)

    path = OUTPUT_DIR / filename
    if not path.exists():
        return JSONResponse({"error": "File not found"}, status_code=404)

    path.unlink()
    return {"ok": True}
