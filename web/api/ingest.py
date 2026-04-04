import os
import threading
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from vanal import db
from vanal.ingest import VIDEO_EXTENSIONS, ingest_directory, OUTPUT_DIR
from web.api.auth import require_auth

router = APIRouter()

_ingest_lock = threading.Lock()
_ingest_thread: threading.Thread | None = None

_frames_lock = threading.Lock()
_frames_thread: threading.Thread | None = None
_frames_progress: dict = {"total": 0, "done": 0, "running": False}


class ListVideosRequest(BaseModel):
    directory: str


class StartIngestRequest(BaseModel):
    directory: str
    reprocess_all: bool = False


@router.get("/fs/browse")
def fs_browse(path: str = "", _auth=Depends(require_auth)):
    """List subdirectories at the given server path for the folder picker."""
    if not path:
        browse = Path.home()
    else:
        browse = Path(path)

    if not browse.exists() or not browse.is_dir():
        raise HTTPException(status_code=400, detail=f"Not a directory: {path}")

    try:
        dirs = sorted(
            e.name for e in browse.iterdir()
            if e.is_dir() and not e.name.startswith(".")
        )
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")

    parent = str(browse.parent) if browse.parent != browse else None
    return {"path": str(browse), "parent": parent, "dirs": dirs}


@router.post("/ingest/list-videos")
def list_videos(req: ListVideosRequest, _auth=Depends(require_auth)):
    directory = Path(req.directory)
    if not directory.exists():
        raise HTTPException(status_code=400, detail=f"Directory not found: {req.directory}")
    if not directory.is_dir():
        raise HTTPException(status_code=400, detail=f"Not a directory: {req.directory}")

    video_files = sorted([
        f for f in directory.rglob("*")
        if f.suffix.lower() in VIDEO_EXTENSIONS
        and f.is_file()
        and OUTPUT_DIR not in f.resolve().parents
        and f.resolve() != OUTPUT_DIR
    ])

    return {"files": [f.name for f in video_files], "count": len(video_files)}


@router.post("/ingest/start")
def start_ingest(req: StartIngestRequest, _auth=Depends(require_auth)):
    global _ingest_thread

    with _ingest_lock:
        if _ingest_thread is not None and _ingest_thread.is_alive():
            raise HTTPException(status_code=409, detail="An ingest is already running")

        directory = Path(req.directory)
        if not directory.exists():
            raise HTTPException(status_code=400, detail=f"Directory not found: {req.directory}")

        def run():
            ingest_directory(directory=directory, reprocess_all=req.reprocess_all)

        _ingest_thread = threading.Thread(target=run, daemon=True)
        _ingest_thread.start()

    return {"ok": True}


def _get_all_done_clips():
    """Return all done clips with a file_hash."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, file_hash, filepath FROM clips WHERE status = 'done' AND file_hash IS NOT NULL"
        ).fetchall()
    return [dict(r) for r in rows]


def _get_missing_frames_clips():
    """Return done clips that have no extracted frame files on disk."""
    frames_base = Path(os.getenv("FRAMES_DIR", "frames"))
    return [
        c for c in _get_all_done_clips()
        if not any((frames_base / c["file_hash"]).glob("frame_*.jpg"))
    ]


class FramesRequest(BaseModel):
    force: bool = False  # True = re-extract all, False = only missing


@router.get("/ingest/missing-frames")
def missing_frames_info(_auth=Depends(require_auth)):
    """Return counts of clips missing frames and current extraction progress."""
    missing = _get_missing_frames_clips()
    total_done = len(_get_all_done_clips())
    return {"count": len(missing), "total": total_done, **_frames_progress}


@router.post("/ingest/extract-missing-frames")
def extract_missing_frames(req: FramesRequest = FramesRequest(), _auth=Depends(require_auth)):
    """Background-extract frames. force=True re-extracts all done clips."""
    global _frames_thread, _frames_progress
    import shutil

    with _frames_lock:
        if _frames_thread is not None and _frames_thread.is_alive():
            raise HTTPException(status_code=409, detail="Frame extraction already running")

        clips = _get_all_done_clips() if req.force else _get_missing_frames_clips()
        if not clips:
            return {"ok": True, "count": 0}

        mode = "all" if req.force else "missing"
        _frames_progress = {"total": len(clips), "done": 0, "running": True, "mode": mode}

        def run():
            from vanal.extractor import extract_frames, probe_video
            frames_base = Path(os.getenv("FRAMES_DIR", "frames"))
            max_frames = int(os.getenv("MAX_FRAMES_PER_CLIP", "8"))
            frame_width = int(os.getenv("FRAME_WIDTH", "512"))

            for clip in clips:
                try:
                    filepath = Path(clip["filepath"])
                    if not filepath.exists():
                        continue
                    frames_dir = frames_base / clip["file_hash"]
                    if req.force and frames_dir.exists():
                        shutil.rmtree(frames_dir, ignore_errors=True)
                    frames_dir.mkdir(parents=True, exist_ok=True)
                    meta = probe_video(filepath)
                    extract_frames(filepath, frames_dir,
                                   duration=meta["duration"],
                                   max_frames=max_frames,
                                   frame_width=frame_width)
                except Exception:
                    pass
                finally:
                    _frames_progress["done"] += 1

            _frames_progress["running"] = False

        _frames_thread = threading.Thread(target=run, daemon=True)
        _frames_thread.start()

    return {"ok": True, "count": len(clips)}
