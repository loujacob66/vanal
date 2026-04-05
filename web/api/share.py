from pathlib import Path

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from vanal import db

router = APIRouter()


# ─── Single clip share ────────────────────────────────────────────

@router.get("/clip/{clip_id}", response_class=HTMLResponse)
def share_clip(clip_id: int):
    """Standalone read-only share page for a single clip."""
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()

    if not row:
        return HTMLResponse("<h2>Clip not found</h2>", status_code=404)

    clip = dict(row)
    filename   = clip.get("filename", "")
    title      = clip.get("title") or filename
    synopsis   = clip.get("synopsis") or ""
    transcript = clip.get("transcript") or ""
    duration   = clip.get("duration") or 0
    width      = clip.get("width") or 0
    height     = clip.get("height") or 0

    dur_str = _format_dur(duration)
    meta_parts = [p for p in [dur_str, f"{width}×{height}" if width else ""] if p]
    meta_html  = " &nbsp;·&nbsp; ".join(meta_parts)

    transcript_block = f"""
        <div class="section">
            <div class="label">Transcript</div>
            <div class="transcript">{_esc(transcript)}</div>
        </div>""" if transcript else ""

    synopsis_block = f"""
        <div class="section">
            <div class="label">Synopsis</div>
            <div class="synopsis">{_esc(synopsis)}</div>
        </div>""" if synopsis else ""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{_esc(title)} — vanal</title>
    <meta property="og:title" content="{_esc(title)}">
    <meta property="og:description" content="{_esc(synopsis[:200]) if synopsis else ''}">
    <meta property="og:type" content="video.other">
    {_common_styles()}
</head>
<body>
    <div class="card">
        <div class="brand"><a href="/">vanal</a></div>
        <video controls preload="metadata" src="/api/clips/{clip_id}/video"></video>
        <h1>{_esc(title)}</h1>
        {f'<div class="meta" style="margin-bottom:4px">{_esc(filename)}</div>' if title != filename else ''}
        <div class="meta">{meta_html}</div>
        {synopsis_block}
        {transcript_block}
        <a class="back" href="/">← Back to library</a>
    </div>
</body>
</html>"""

    return HTMLResponse(html)


# ─── Queue / playlist share ───────────────────────────────────────

@router.get("/playlist", response_class=HTMLResponse)
def share_playlist(ids: str = Query(default="")):
    """Standalone read-only playlist page for a set of clips."""
    if not ids:
        return HTMLResponse("<h2>No clips specified</h2>", status_code=400)

    try:
        id_list = [int(x) for x in ids.split(",") if x.strip()]
    except ValueError:
        return HTMLResponse("<h2>Invalid clip IDs</h2>", status_code=400)

    if not id_list:
        return HTMLResponse("<h2>No clips specified</h2>", status_code=400)

    with db.get_conn() as conn:
        placeholders = ",".join("?" * len(id_list))
        rows = conn.execute(
            f"SELECT * FROM clips WHERE id IN ({placeholders})", id_list
        ).fetchall()

    clip_map = {row["id"]: dict(row) for row in rows}
    clips = [clip_map[i] for i in id_list if i in clip_map]

    if not clips:
        return HTMLResponse("<h2>No clips found</h2>", status_code=404)

    total_dur = sum(c.get("duration") or 0 for c in clips)
    total_str = _format_dur(total_dur) or ""

    def clip_block(c: dict, idx: int) -> str:
        cid       = c["id"]
        filename  = c.get("filename", "")
        title     = c.get("title") or filename
        synopsis  = c.get("synopsis") or ""
        transcript= c.get("transcript") or ""
        duration  = c.get("duration") or 0
        width     = c.get("width") or 0
        height    = c.get("height") or 0

        dur_str = _format_dur(duration)
        meta_parts = [p for p in [dur_str, f"{width}×{height}" if width else ""] if p]
        meta_html  = " &nbsp;·&nbsp; ".join(meta_parts)

        synopsis_html = f"""<div class="sect-label">Synopsis</div>
            <div class="synopsis">{_esc(synopsis)}</div>""" if synopsis else ""
        transcript_html = f"""<div class="sect-label" style="margin-top:10px">Transcript</div>
            <div class="transcript">{_esc(transcript)}</div>""" if transcript else ""

        return f"""
        <div class="pl-item" id="clip-{cid}">
            <div class="pl-num">{idx + 1}</div>
            <div class="pl-body">
                <div class="pl-title">{_esc(title)}</div>
                <div class="meta" style="margin-bottom:10px">{meta_html}</div>
                <video controls preload="metadata" src="/api/clips/{cid}/video"></video>
                {f'<div class="pl-details">{synopsis_html}{transcript_html}</div>' if (synopsis or transcript) else ""}
            </div>
        </div>"""

    items_html = "\n".join(clip_block(c, i) for i, c in enumerate(clips))
    count_label = f"{len(clips)} clip{'s' if len(clips) != 1 else ''}"
    dur_label   = f" · {total_str} total" if total_str else ""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Playlist ({len(clips)} clips) — vanal</title>
    <meta property="og:title" content="vanal Playlist ({len(clips)} clips)">
    <meta property="og:type" content="video.other">
    {_common_styles()}
    <style>
        .pl-item {{
            display: flex;
            gap: 16px;
            background: #1c2030;
            border: 1px solid #2e3345;
            border-radius: 12px;
            padding: 16px;
            margin-bottom: 20px;
        }}
        .pl-num {{
            font-size: 1.4rem;
            font-weight: 700;
            color: #6c7aed;
            min-width: 28px;
            text-align: right;
            padding-top: 2px;
        }}
        .pl-body {{ flex: 1; min-width: 0; }}
        .pl-title {{
            font-size: 1rem;
            font-weight: 600;
            margin-bottom: 4px;
            word-break: break-all;
        }}
        .pl-details {{
            margin-top: 12px;
            padding-top: 12px;
            border-top: 1px solid #2e3345;
        }}
        .sect-label {{
            font-size: 0.7rem;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            color: #6c7aed;
            font-weight: 700;
            margin-bottom: 4px;
        }}
        @media (max-width: 480px) {{
            .pl-num {{ font-size: 1.1rem; min-width: 22px; }}
        }}
    </style>
</head>
<body>
    <div class="card">
        <div class="brand"><a href="/">vanal</a></div>
        <h1 style="margin-bottom:4px">Playlist</h1>
        <div class="meta" style="margin-bottom:24px">{count_label}{dur_label}</div>
        {items_html}
        <a class="back" href="/">← Back to library</a>
    </div>
</body>
</html>"""

    return HTMLResponse(html)


# ─── Montage share ────────────────────────────────────────────────

@router.get("/montage/{montage_id}", response_class=HTMLResponse)
def share_montage(montage_id: int):
    """Standalone read-only share page for a montage."""
    with db.get_conn() as conn:
        montage = conn.execute(
            "SELECT m.*, u.name AS owner_name FROM montages m LEFT JOIN users u ON m.owner_id = u.id WHERE m.id = ?",
            (montage_id,),
        ).fetchone()

    if not montage:
        return HTMLResponse("<h2>Montage not found</h2>", status_code=404)

    m = dict(montage)
    filename = m.get("filename", "")
    display_name = filename.replace("_", " ").rsplit(".", 1)[0]
    # Strip timestamp suffix
    import re
    display_name = re.sub(r'\s+\d{8}\s+\d{6}$', '', display_name)
    owner = m.get("owner_name") or "Unknown"
    size_mb = m.get("size_mb") or 0
    created = m.get("created_at") or ""

    # Get clip thumbnails for poster display
    with db.get_conn() as conn:
        clip_rows = conn.execute(
            """SELECT c.file_hash, c.thumbnail_frame, c.filename, c.title
               FROM montage_clips mc
               JOIN clips c ON mc.clip_id = c.id
               WHERE mc.montage_id = ?
               ORDER BY mc.position""",
            (montage_id,),
        ).fetchall()

    clip_list_html = ""
    if clip_rows:
        items = []
        for i, c in enumerate(clip_rows):
            thumb = f"/frames/{c['file_hash']}/{c['thumbnail_frame'] or 'frame_0001.jpg'}"
            name = _esc(c["title"] or c["filename"])
            items.append(f'<div class="mc-item"><img src="{thumb}" onerror="this.style.display=\'none\'"><span>{i+1}. {name}</span></div>')
        clip_list_html = f"""
        <div class="section">
            <div class="label">Clips in this montage ({len(clip_rows)})</div>
            <div class="mc-grid">{"".join(items)}</div>
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{_esc(display_name)} — vanal</title>
    <meta property="og:title" content="{_esc(display_name)}">
    <meta property="og:description" content="Montage by {_esc(owner)} · {len(clip_rows)} clips · {size_mb} MB">
    <meta property="og:type" content="video.other">
    {_common_styles()}
    <style>
        .mc-grid {{
            display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
            gap: 8px; margin-top: 8px;
        }}
        .mc-item {{
            display: flex; flex-direction: column; gap: 4px;
            background: #161825; border-radius: 8px; overflow: hidden;
        }}
        .mc-item img {{
            width: 100%; aspect-ratio: 16/9; object-fit: cover; display: block;
        }}
        .mc-item span {{
            padding: 4px 8px 6px; font-size: 0.72rem; color: #8b8fa3;
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
        }}
    </style>
</head>
<body>
    <div class="card">
        <div class="brand"><a href="/">vanal</a></div>
        <video controls preload="metadata" src="/share/montage/{montage_id}/video"></video>
        <h1>{_esc(display_name)}</h1>
        <div class="meta">By {_esc(owner)} · {size_mb} MB · {_esc(created)}</div>
        {clip_list_html}
        <a class="back" href="/">← Back to library</a>
    </div>
</body>
</html>"""

    return HTMLResponse(html)


@router.get("/montage/{montage_id}/video")
def share_montage_video(montage_id: int):
    """Stream montage video for the public share page (no auth)."""
    with db.get_conn() as conn:
        montage = conn.execute(
            "SELECT filepath, filename FROM montages WHERE id = ?", (montage_id,)
        ).fetchone()

    if not montage:
        return JSONResponse({"error": "Not found"}, status_code=404)

    path = Path(montage["filepath"])
    if not path.exists():
        return JSONResponse({"error": "File not found"}, status_code=404)

    return FileResponse(path, media_type="video/mp4", filename=montage["filename"])


# ─── Helpers ──────────────────────────────────────────────────────

def _format_dur(duration: float) -> str:
    if not duration:
        return ""
    m, s = divmod(int(duration), 60)
    return f"{m}:{s:02d}" if m else f"{s}s"


def _esc(s: str) -> str:
    return (s
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;"))


def _common_styles() -> str:
    return """<style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #0f1117;
            color: #f0f0f8;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            align-items: center;
            padding: 24px 16px 48px;
        }
        .card {
            width: 100%;
            max-width: 800px;
        }
        .brand {
            font-size: 0.8rem;
            color: #6c7aed;
            font-weight: 700;
            letter-spacing: 0.1em;
            text-transform: uppercase;
            margin-bottom: 16px;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .brand a { color: inherit; text-decoration: none; }
        video {
            width: 100%;
            max-height: 70vh;
            border-radius: 12px;
            background: #000;
            display: block;
            box-shadow: 0 8px 32px rgba(0,0,0,0.5);
            margin-bottom: 10px;
            /* ensure controls bar is never clipped */
            min-height: 48px;
        }
        h1 {
            font-size: 1.2rem;
            margin: 16px 0 4px;
            word-break: break-all;
        }
        .meta {
            font-size: 0.82rem;
            color: #8b8fa3;
            margin-bottom: 20px;
        }
        .section {
            margin-bottom: 20px;
            background: #1c2030;
            border: 1px solid #2e3345;
            border-radius: 10px;
            padding: 14px 16px;
        }
        .label {
            font-size: 0.7rem;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            color: #6c7aed;
            font-weight: 700;
            margin-bottom: 8px;
        }
        .synopsis {
            font-size: 0.95rem;
            line-height: 1.7;
            color: #e0e0e8;
        }
        .transcript {
            font-size: 0.9rem;
            line-height: 1.8;
            color: #c0c4d8;
            font-style: italic;
        }
        .back {
            margin-top: 24px;
            display: inline-flex;
            align-items: center;
            gap: 6px;
            color: #6c7aed;
            font-size: 0.85rem;
            text-decoration: none;
        }
        .back:hover { text-decoration: underline; }
    </style>"""
