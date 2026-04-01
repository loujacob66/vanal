#!/usr/bin/env python3
"""
sora_download.py - Download all videos from one or more Sora (OpenAI) accounts.

Configuration via .env:
  SORA_API_KEYS=sk-key1,sk-key2          # comma-separated API keys
  # Optional per-key labels:
  # SORA_API_KEYS=sk-key1:AccountA,sk-key2:AccountB
  SORA_DOWNLOAD_DIR=./sora_downloads     # output directory (default: ./sora_downloads)

Usage:
  python sora_download.py [--output DIR] [--dry-run]
"""
import argparse
import os
import sys
import time
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests

VIDEOS_API = "https://api.openai.com/v1/videos"
CONTENT_API = "https://api.openai.com/v1/videos/{video_id}/content"


def parse_api_keys(raw: str) -> list[tuple[str, str]]:
    """Parse SORA_API_KEYS into list of (api_key, label) tuples."""
    accounts = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" in entry:
            key, label = entry.split(":", 1)
        else:
            key = entry
            label = f"acct_{key[-4:]}"
        accounts.append((key.strip(), label.strip()))
    return accounts


def list_videos(api_key: str) -> list[dict]:
    """Paginate through all videos for one account."""
    headers = {"Authorization": f"Bearer {api_key}"}
    videos = []
    after = None

    while True:
        params = {"limit": 100, "order": "asc"}
        if after:
            params["after"] = after

        resp = _get_with_retry(VIDEOS_API, headers=headers, params=params)
        data = resp.json()

        page = data.get("data", [])
        videos.extend(page)

        if not data.get("has_more", False) or not page:
            break
        after = page[-1]["id"]

    return videos


def download_video(api_key: str, video_id: str, dest: Path) -> None:
    """Stream a single video to dest."""
    url = CONTENT_API.format(video_id=video_id)
    headers = {"Authorization": f"Bearer {api_key}"}

    resp = _get_with_retry(url, headers=headers, stream=True)
    tmp = dest.with_suffix(".tmp")
    try:
        with tmp.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
        tmp.rename(dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _get_with_retry(url: str, *, stream: bool = False, **kwargs) -> requests.Response:
    delay = 1
    for attempt in range(4):
        resp = requests.get(url, stream=stream, timeout=60, **kwargs)
        if resp.status_code == 429:
            if attempt < 3:
                print(f"  Rate limited, retrying in {delay}s...")
                time.sleep(delay)
                delay *= 2
                continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()  # final raise


def download_account(api_key: str, label: str, output_dir: Path, dry_run: bool) -> None:
    print(f"\n[{label}] Fetching video list...")
    try:
        videos = list_videos(api_key)
    except requests.HTTPError as e:
        print(f"[{label}] ERROR listing videos: {e}")
        return

    total = len(videos)
    downloaded = 0
    skipped = 0
    errors = 0

    for video in videos:
        video_id = video["id"]
        filename = f"{label}_{video_id}.mp4"
        dest = output_dir / filename

        if dest.exists():
            skipped += 1
            continue

        if dry_run:
            print(f"  [dry-run] Would download {filename}")
            downloaded += 1
            continue

        try:
            download_video(api_key, video_id, dest)
            print(f"  Downloaded {filename}")
            downloaded += 1
        except Exception as e:
            print(f"  ERROR downloading {video_id}: {e}")
            errors += 1

    parts = [f"{total} found", f"{downloaded} downloaded", f"{skipped} skipped"]
    if errors:
        parts.append(f"{errors} errors")
    print(f"[{label}] {', '.join(parts)}")


def main():
    parser = argparse.ArgumentParser(description="Download all Sora videos across accounts")
    parser.add_argument(
        "--output", "-o",
        default=os.getenv("SORA_DOWNLOAD_DIR", "./sora_downloads"),
        help="Directory to save videos (default: ./sora_downloads)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List videos without downloading",
    )
    args = parser.parse_args()

    raw_keys = os.getenv("SORA_API_KEYS", "").strip()
    if not raw_keys:
        print("Error: SORA_API_KEYS not set. Add it to your .env file.")
        print("  Example: SORA_API_KEYS=sk-key1:AccountA,sk-key2:AccountB")
        sys.exit(1)

    accounts = parse_api_keys(raw_keys)
    if not accounts:
        print("Error: No valid API keys found in SORA_API_KEYS.")
        sys.exit(1)

    output_dir = Path(args.output)
    if not args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Output directory: {output_dir.resolve()}")
    print(f"Accounts: {len(accounts)}")
    if args.dry_run:
        print("Dry-run mode — no files will be written.\n")

    for api_key, label in accounts:
        download_account(api_key, label, output_dir, args.dry_run)

    print("\nDone.")


if __name__ == "__main__":
    main()
