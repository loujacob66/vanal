#!/usr/bin/env python3
"""
vanal - Video Analyzer & Reel Arranger

Usage:
  python run.py ingest <directory> [options]
  python run.py serve [options]
  python run.py remap <old_base>:<new_base>
"""
import argparse
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def cmd_ingest(args):
    from vanal.ingest import ingest_directory
    ingest_directory(
        directory=args.directory,
        retry_errors=args.retry_errors,
        keep_frames=args.keep_frames,
        delay_secs=args.delay,
        reprocess_all=args.reprocess_all,
    )


def cmd_remap(args):
    from vanal.ingest import _apply_path_remap
    from vanal import db
    db.migrate()
    if ":" not in args.remap:
        print("Error: remap must be in format OLD_BASE:NEW_BASE")
        sys.exit(1)
    old_base, new_base = args.remap.split(":", 1)
    _apply_path_remap(old_base, new_base)


def cmd_serve(args):
    import uvicorn
    from vanal import db
    db.migrate()
    uvicorn.run(
        "web.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


def main():
    parser = argparse.ArgumentParser(
        prog="vanal",
        description="Video Analyzer & Reel Arranger",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- ingest ---
    ingest_parser = subparsers.add_parser("ingest", help="Process video files")
    ingest_parser.add_argument("directory", help="Directory containing video files")
    ingest_parser.add_argument(
        "--retry-errors", action="store_true",
        help="Re-process clips that previously errored"
    )
    ingest_parser.add_argument(
        "--keep-frames", action="store_true",
        help="Keep extracted frames after processing (enables thumbnails in UI)"
    )
    ingest_parser.add_argument(
        "--delay", type=float,
        default=float(os.getenv("INGEST_DELAY_SECS", "1.0")),
        help="Seconds to wait between clips (default: 1.0)"
    )
    ingest_parser.add_argument(
        "--reprocess-all", action="store_true",
        help="Re-analyze all clips, clearing old frames and regenerating synopses"
    )
    ingest_parser.set_defaults(func=cmd_ingest)

    # --- remap ---
    remap_parser = subparsers.add_parser(
        "remap", help="Update file paths when NAS mount point changes"
    )
    remap_parser.add_argument(
        "remap", metavar="OLD_BASE:NEW_BASE",
        help="e.g. /mnt/old/videos:/mnt/new/videos"
    )
    remap_parser.set_defaults(func=cmd_remap)

    # --- serve ---
    serve_parser = subparsers.add_parser("serve", help="Start the web UI")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)
    serve_parser.add_argument("--reload", action="store_true", help="Auto-reload on code changes")
    serve_parser.set_defaults(func=cmd_serve)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
