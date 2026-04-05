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
    """Per-user output directory: {OUTPUT_DIR}/{email_prefix}/"""
    email_prefix = user["email"].split("@")[0].replace("/", "_").replace("\\", "_")
    d = OUTPUT_DIR / email_prefix
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
    canvas: str = ""        # "1920x1080", "1080x1920", "1080x1080", "" = auto/none
    fit: str = "letterbox"  # "letterbox" or "crop"


@router.post("/export/render")
def render_montage(req: RenderRequest, _auth=Depends(require_auth)):
    """Concatenate selected clips into a streamable MP4 saved to per-user output dir."""
    if not req.clip_ids:
        return JSONResponse({"error": "No clips selected"}, status_code=400)

    with db.get_conn() as conn:
        # Verify ownership and get dimensions
        clip_rows = []
        for cid in req.clip_ids:
            clip = get_user_clip(conn, cid, _auth, columns="id, filepath, width, height, owner_id")
            clip_rows.append(clip)

    # Preserve the requested order
    id_to_row = {r["id"]: r for r in clip_rows}
    paths = []
    missing = []
    for cid in req.clip_ids:
        r = id_to_row.get(cid)
        if not r:
            missing.append(cid)
            continue
        if not Path(r["filepath"]).exists():
            missing.append(cid)
            continue
        paths.append(r)

    if not paths:
        return JSONResponse({"error": "None of the selected clips have accessible files"}, status_code=400)

    user_out = _user_output_dir(_auth)

    # Build output filename
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = req.filename.strip() or "montage"
    safe_base = "".join(c if c.isalnum() or c in "-_" else "_" for c in base)
    out_filename = f"{safe_base}_{ts}.mp4"
    out_path = user_out / out_filename

    # Determine if we need canvas-aware re-encoding
    use_canvas = bool(req.canvas)

    if use_canvas:
        # Parse target canvas
        try:
            tw, th = (int(x) for x in req.canvas.split("x"))
        except ValueError:
            return JSONResponse({"error": f"Invalid canvas: {req.canvas}"}, status_code=400)

        # Build ffmpeg command with per-input scaling via filter_complex
        cmd = ["ffmpeg", "-y"]
        for clip in paths:
            cmd += ["-i", clip["filepath"]]

        # Build filter: scale each input to target canvas
        filters = []
        for i, clip in enumerate(paths):
            if req.fit == "crop":
                # Scale up to cover canvas, then crop to exact size
                filters.append(
                    f"[{i}:v]scale={tw}:{th}:force_original_aspect_ratio=increase,"
                    f"crop={tw}:{th},setsar=1[v{i}]"
                )
            else:
                # Letterbox: scale to fit within canvas, pad with black
                filters.append(
                    f"[{i}:v]scale={tw}:{th}:force_original_aspect_ratio=decrease,"
                    f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[v{i}]"
                )

        # Concatenate all scaled streams
        concat_inputs = "".join(f"[v{i}][{i}:a]" for i in range(len(paths)))
        # Handle clips without audio by adding silent audio
        filter_parts = []
        for i, clip in enumerate(paths):
            filter_parts.append(filters[i])
            # Ensure audio stream exists (generate silence if missing)
            filter_parts.append(
                f"[{i}:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[a{i}]"
            )

        # Rebuild: just use scale filters + concat with audio handling
        filter_str = ";".join(filters)
        # Try simpler approach: scale video, then concat with audio fallback
        concat_v = "".join(f"[v{i}]" for i in range(len(paths)))
        concat_a = "".join(f"[{i}:a]" for i in range(len(paths)))
        filter_str += f";{concat_v}concat=n={len(paths)}:v=1:a=0[outv]"

        cmd += [
            "-filter_complex", filter_str,
            "-map", "[outv]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-movflags", "+faststart",
        ]

        # Handle audio: try to concat audio streams separately
        # Use a simpler approach — re-merge audio with amerge or just take audio from concat
        # Actually simplest: use both video and audio in concat
        # Rebuild filter_complex properly
        audio_filters = []
        for i in range(len(paths)):
            audio_filters.append(
                f"[{i}:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]"
            )

        full_filter = ";".join(filters + audio_filters)
        concat_inputs = "".join(f"[v{i}][a{i}]" for i in range(len(paths)))
        full_filter += f";{concat_inputs}concat=n={len(paths)}:v=1:a=1[outv][outa]"

        cmd = ["ffmpeg", "-y"]
        for clip in paths:
            cmd += ["-i", clip["filepath"]]
        cmd += [
            "-filter_complex", full_filter,
            "-map", "[outv]", "-map", "[outa]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(out_path),
        ]
    else:
        # Original simple path — concat demuxer
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            tmp_list = f.name
            for clip in paths:
                escaped = clip["filepath"].replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")

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

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            return JSONResponse({
                "error": "ffmpeg failed",
                "details": result.stderr[-2000:],
            }, status_code=500)
    except subprocess.TimeoutExpired:
        return JSONResponse({"error": "ffmpeg timed out after 10 minutes"}, status_code=500)
    finally:
        if not use_canvas:
            Path(tmp_list).unlink(missing_ok=True)

    size_mb = round(out_path.stat().st_size / 1_048_576, 1)

    # Register montage in database with clip associations
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO montages (filename, filepath, owner_id, size_mb) VALUES (?, ?, ?, ?)",
            (out_filename, str(out_path), _auth["id"], size_mb),
        )
        montage_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for pos, cid in enumerate(req.clip_ids):
            if cid not in missing:
                conn.execute(
                    "INSERT INTO montage_clips (montage_id, clip_id, position) VALUES (?, ?, ?)",
                    (montage_id, cid, pos),
                )

    return {
        "ok": True,
        "filename": out_filename,
        "size_mb": size_mb,
        "clip_count": len(paths),
        "skipped": missing,
    }


def _attach_thumbnails(conn, montages: list[dict]) -> list[dict]:
    """Attach clip thumbnail info to each montage dict."""
    for m in montages:
        rows = conn.execute(
            """SELECT c.file_hash, c.thumbnail_frame
               FROM montage_clips mc
               JOIN clips c ON mc.clip_id = c.id
               WHERE mc.montage_id = ?
               ORDER BY mc.position""",
            (m["id"],),
        ).fetchall()
        m["thumbnails"] = [
            {"file_hash": r["file_hash"], "frame": r["thumbnail_frame"] or "frame_0001.jpg"}
            for r in rows
        ]
    return montages


@router.get("/outputs")
def list_outputs(_auth=Depends(require_auth)):
    """List rendered montages in the current user's output dir."""
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT m.id, m.filename, m.size_mb, m.created_at
               FROM montages m
               WHERE m.owner_id = ?
               ORDER BY m.created_at DESC""",
            (_auth["id"],),
        ).fetchall()
        result = [dict(r) for r in rows]

        # Also pick up any legacy files not yet in DB
        user_out = _user_output_dir(_auth)
        db_filenames = {r["filename"] for r in result}
        for f in sorted(user_out.glob("*.mp4"), key=lambda f: f.stat().st_mtime, reverse=True):
            if f.name not in db_filenames:
                stat = f.stat()
                size_mb = round(stat.st_size / 1_048_576, 1)
                created_at = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
                conn.execute(
                    "INSERT INTO montages (filename, filepath, owner_id, size_mb, created_at) VALUES (?, ?, ?, ?, ?)",
                    (f.name, str(f), _auth["id"], size_mb, created_at),
                )
                mid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                result.append({"id": mid, "filename": f.name, "size_mb": size_mb, "created_at": created_at})

        _attach_thumbnails(conn, result)

    return result


# ── Montage sharing ──────────────────────────────────────────────
# IMPORTANT: static paths must be defined before {filename}/{montage_id} routes

class MontageShareRequest(BaseModel):
    user_ids: list[int]


@router.get("/outputs/shared-with-me")
def shared_montages(_auth=Depends(require_auth)):
    """Return montages shared with the current user."""
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT m.id, m.filename, m.size_mb, m.created_at,
                      u.name AS owner_name, u.email AS owner_email,
                      ms.created_at AS shared_at
               FROM montage_shares ms
               JOIN montages m ON ms.montage_id = m.id
               LEFT JOIN users u ON m.owner_id = u.id
               WHERE ms.shared_with = ?
               ORDER BY ms.created_at DESC""",
            (_auth["id"],),
        ).fetchall()
        result = [dict(r) for r in rows]
        _attach_thumbnails(conn, result)
    return result


@router.get("/outputs/{filename}/video")
def stream_output(filename: str, _auth=Depends(require_auth)):
    """Stream a rendered montage for in-browser playback / download."""
    if "/" in filename or "\\" in filename or ".." in filename:
        return JSONResponse({"error": "Invalid filename"}, status_code=400)

    # Check ownership or shared access
    with db.get_conn() as conn:
        montage = conn.execute(
            "SELECT id, filepath, owner_id FROM montages WHERE filename = ? AND owner_id = ?",
            (filename, _auth["id"]),
        ).fetchone()
        if not montage:
            # Check shared access
            montage = conn.execute(
                """SELECT m.id, m.filepath, m.owner_id
                   FROM montages m
                   JOIN montage_shares ms ON ms.montage_id = m.id
                   WHERE m.filename = ? AND ms.shared_with = ?""",
                (filename, _auth["id"]),
            ).fetchone()
        if not montage:
            return JSONResponse({"error": "File not found"}, status_code=404)

    path = Path(montage["filepath"])
    if not path.exists():
        return JSONResponse({"error": "File not found on disk"}, status_code=404)

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
    # Remove from DB (cascade deletes shares)
    with db.get_conn() as conn:
        conn.execute(
            "DELETE FROM montages WHERE filename = ? AND owner_id = ?",
            (filename, _auth["id"]),
        )
    return {"ok": True}


@router.post("/outputs/{montage_id}/share")
def share_montage(montage_id: int, req: MontageShareRequest, _auth=Depends(require_auth)):
    """Share a montage with one or more users."""
    with db.get_conn() as conn:
        montage = conn.execute(
            "SELECT id, filename, owner_id FROM montages WHERE id = ? AND owner_id = ?",
            (montage_id, _auth["id"]),
        ).fetchone()
        if not montage:
            return JSONResponse({"error": "Montage not found"}, status_code=404)

        sharer_name = _auth.get("name") or _auth.get("email", "Someone")
        display_name = montage["filename"]
        for uid in req.user_ids:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO montage_shares (montage_id, shared_by, shared_with) VALUES (?, ?, ?)",
                (montage_id, _auth["id"], uid),
            )
            if cursor.rowcount > 0:
                conn.execute(
                    "INSERT INTO notifications (user_id, type, message, montage_id) VALUES (?, ?, ?, ?)",
                    (uid, "montage_share", f'{sharer_name} shared montage "{display_name}" with you', montage_id),
                )
    return {"ok": True}


@router.delete("/outputs/{montage_id}/share/{user_id}")
def unshare_montage(montage_id: int, user_id: int, _auth=Depends(require_auth)):
    """Remove a montage share."""
    with db.get_conn() as conn:
        montage = conn.execute(
            "SELECT id FROM montages WHERE id = ? AND owner_id = ?",
            (montage_id, _auth["id"]),
        ).fetchone()
        if not montage:
            return JSONResponse({"error": "Montage not found"}, status_code=404)
        conn.execute(
            "DELETE FROM montage_shares WHERE montage_id = ? AND shared_with = ?",
            (montage_id, user_id),
        )
    return {"ok": True}


@router.get("/outputs/{montage_id}/shares")
def list_montage_shares(montage_id: int, _auth=Depends(require_auth)):
    """List users a montage is shared with."""
    with db.get_conn() as conn:
        montage = conn.execute(
            "SELECT id FROM montages WHERE id = ? AND owner_id = ?",
            (montage_id, _auth["id"]),
        ).fetchone()
        if not montage:
            return JSONResponse({"error": "Montage not found"}, status_code=404)
        rows = conn.execute(
            """SELECT ms.shared_with AS user_id, u.name, u.email, ms.created_at
               FROM montage_shares ms
               JOIN users u ON ms.shared_with = u.id
               WHERE ms.montage_id = ?
               ORDER BY u.name, u.email""",
            (montage_id,),
        ).fetchall()
    return [dict(r) for r in rows]
