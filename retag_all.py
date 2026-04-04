#!/usr/bin/env python3
"""
Batch re-tag all clips using the current generate_tags logic.

Usage:
    python retag_all.py              # skip clips that already have tags
    python retag_all.py --overwrite  # re-tag everything
    python retag_all.py --dry-run    # show what would be tagged, don't write
"""
import argparse
import json
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from vanal import db
from vanal.vision import generate_tags


def main():
    parser = argparse.ArgumentParser(description="Batch re-tag all clips")
    parser.add_argument("--overwrite", action="store_true",
                        help="Re-tag clips that already have tags")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would happen without writing to DB")
    args = parser.parse_args()

    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, filename, synopsis, transcript, tags, raw_frames_json "
            "FROM clips WHERE status = 'done' ORDER BY id"
        ).fetchall()

    clips = [dict(r) for r in rows]
    print(f"Found {len(clips)} done clips")

    skipped = 0
    tagged = 0
    failed = 0

    for i, clip in enumerate(clips, 1):
        existing_tags = [t.strip() for t in (clip["tags"] or "").split(",") if t.strip()]

        if existing_tags and not args.overwrite:
            skipped += 1
            continue

        frame_descriptions = []
        if clip["raw_frames_json"]:
            try:
                frames = json.loads(clip["raw_frames_json"])
                frame_descriptions = [f.get("description", "") for f in frames]
            except (json.JSONDecodeError, AttributeError):
                pass

        if not frame_descriptions and not clip["synopsis"]:
            print(f"  [{i}/{len(clips)}] SKIP {clip['filename']} — no frame data or synopsis")
            skipped += 1
            continue

        print(f"  [{i}/{len(clips)}] {clip['filename']}...", end=" ", flush=True)

        auto_tags = generate_tags(
            frame_descriptions,
            clip["synopsis"] or "",
            clip["transcript"] or None,
            clip["filename"],
        )

        if not auto_tags:
            print("FAILED (model returned nothing)")
            failed += 1
            continue

        # Merge: keep existing user tags, add new auto tags
        merged = list(dict.fromkeys(existing_tags + auto_tags))
        tags_str = ", ".join(merged)

        print(f"→ {tags_str}")

        if not args.dry_run:
            with db.get_conn() as conn:
                conn.execute(
                    "UPDATE clips SET tags = ?, updated_at = datetime('now') WHERE id = ?",
                    (tags_str, clip["id"]),
                )
                conn.execute("DELETE FROM clips_fts WHERE rowid = ?", (clip["id"],))
                conn.execute(
                    "INSERT INTO clips_fts(rowid, filename, synopsis, transcript, tags, notes) "
                    "VALUES (?,?,?,?,?,?)",
                    (clip["id"], clip["filename"], clip["synopsis"] or "",
                     clip["transcript"] or "", tags_str, ""),
                )

        tagged += 1

    print(f"\nDone. Tagged: {tagged}, Skipped: {skipped}, Failed: {failed}")
    if args.dry_run:
        print("(dry run — nothing was written)")


if __name__ == "__main__":
    main()
