import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


class NoCacheHTMLMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        ct = response.headers.get("content-type", "")
        if "text/html" in ct:
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response

from vanal import db
from web.api import auth, clips, ordering, export, share, ingest

app = FastAPI(title="vanal", description="Video Analyzer & Reel Arranger")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SECRET_KEY", "dev-secret-change-me"))
app.add_middleware(NoCacheHTMLMiddleware)

app.include_router(auth.router, prefix="/api")
app.include_router(clips.router, prefix="/api")
app.include_router(ordering.router, prefix="/api")
app.include_router(export.router, prefix="/api")
app.include_router(share.router, prefix="/share")
app.include_router(ingest.router, prefix="/api")

# Serve extracted frames
frames_dir = Path("frames")
frames_dir.mkdir(exist_ok=True)
app.mount("/frames", StaticFiles(directory=str(frames_dir)), name="frames")

# Serve frontend static files (must be last — catches everything)
static_dir = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")


@app.on_event("startup")
def startup():
    db.migrate()

    # Reset any clips stuck as 'processing' from a previous crash/restart
    with db.get_conn() as conn:
        conn.execute("UPDATE clips SET status = 'pending', processing_stage = NULL WHERE status = 'processing'")

    # Kick the worker to process any pending clips left in the queue
    from web.api.clips import _kick_processing_worker
    _kick_processing_worker()
