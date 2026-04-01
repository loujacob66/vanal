from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from vanal import db
from web.api import auth, clips, ordering, export, share

app = FastAPI(title="vanal", description="Video Analyzer & Reel Arranger")

app.include_router(auth.router, prefix="/api")
app.include_router(clips.router, prefix="/api")
app.include_router(ordering.router, prefix="/api")
app.include_router(export.router, prefix="/api")
app.include_router(share.router, prefix="/share")

# Serve extracted frames if they exist
frames_dir = Path("frames")
if frames_dir.exists():
    app.mount("/frames", StaticFiles(directory=str(frames_dir)), name="frames")

# Serve frontend static files (must be last — catches everything)
static_dir = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")


@app.on_event("startup")
def startup():
    db.migrate()
