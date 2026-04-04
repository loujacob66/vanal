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

from vanal import db
from web.api import auth, clips, ordering, export, share, ingest

app = FastAPI(title="vanal", description="Video Analyzer & Reel Arranger")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SECRET_KEY", "dev-secret-change-me"))

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
