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
from web.api.clips import _owner_where, get_user_clip

router = APIRouter()

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "vanal-outputs"))


def _user_output_dir(user: dict) -> Path:
    """Per-user output directory: {OUTPUT_DIR}/{user_id}/"""
    d = OUTPUT_DIR / str(user["id"])
    d.mkdir(parents=True, exist_ok=True)
    return d


@router.get("/export/json")
def export_json(_auth=Depends(require_auth)):
    """Export ordered clip list as JSON."""
    owner_frag, owner_params = _owner_where(_auth)
    with db.get_conn() as conn:
        rows = conn.execute(
            f"SELECT id, filename, filepath, synopsis, position, tags, duration "
            f"FROM clips WHERE status = 'done' {owner_frag} "
            f"ORDER BY position NULLS LAST, filename",
            owner_params,
        ).fetchall()
    return [dict(r) for r in rows]


@router.get("/export/ffmpeg-concat")
def export_ffmpeg_concat(_auth=Depends(require_auth)):
    """Export as an ffmpeg concat demuxer manifest."""
    owner_frag, owner_params = _owner_where(_auth)
    with db.get_conn() as conn:
        rows = conn.execute(
            f"SELECT filepath, duration FROM clips WHERE status = 'done' {owner_frag} "
            f"ORDER BY position NULLS LAST, filename",
            owner_params,
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
    filename: str = ""
    reencode: bool = False


@router.post("/export/render")
def render_montage(req: RenderRequest, _auth=Depends(require_auth)):
    """Concatenate selected clips into a streamable MP4 saved to per-user output dir."""
    if not req.clip_ids:
        return JSONResponse({"error": "No clips selected"}, status_code=400)

    with db.get_conn() as conn:
        # Verify ownership of all clips
        clip_rows = []
        for cid in req.clip_ids:
            clip = get_user_clip(conn, cid, _auth, columns="id, filepath, owner_id")
            clip_rows.append(clip)

    # Preserve the requested order
    id_to_path = {r["id"]: r["filepath"] for r in clip_rows}
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

    user_out = _user_output_dir(_auth)

    # Build output filename
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = req.filename.strip() or "montage"
    safe_base = "".join(c if c.isalnum() or c in "-_" else "_" for c in base)
    out_filename = f"{safe_base}_{ts}.mp4"
    out_path = user_out / out_filename

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        tmp_list = f.name
        for p in paths:
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
                "details": result.stderr[-2000:],
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
def list_outputs(_auth=Depends(require_auth)):
    """List rendered montages in the current user's output dir."""
    user_out = _user_output_dir(_auth)

    files = sorted(user_out.glob("*.mp4"), key=lambda f: f.stat().st_mtime, reverse=True)
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
def stream_output(filename: str, _auth=Depends(require_auth)):
    """Stream a rendered montage for in-browser playback / download."""
    if "/" in filename or "\\" in filename or ".." in filename:
        return JSONResponse({"error": "Invalid filename"}, status_code=400)

    path = _user_output_dir(_auth) / filename
    if not path.exists():
        return JSONResponse({"error": "File not found"}, status_code=404)

    return FileResponse(path, media_type="video/mp4", filename=filename)


@router.delete("/outputs/{filename}")
def delete_output(filename: str, _auth=Depends(require_auth)):
    """Delete a rendered montage."""
    if "/" in filename or "\\" in filename or ".." in filename:
        return JSONResponse({"error": "Invalid filename"}, status_code=400)

    path = _user_output_dir(_auth) / filename
    if not path.exists():
        return JSONResponse({"error": "File not found"}, status_code=404)

    path.unlink()
    return {"ok": True}
