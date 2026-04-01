import json
import os
import subprocess
from pathlib import Path


def probe_video(path: str | Path) -> dict:
    """Run ffprobe on a video file, return parsed metadata dict."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {result.stderr.strip()}")

    data = json.loads(result.stdout)
    fmt = data.get("format", {})
    streams = data.get("streams", [])

    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = float(fmt.get("duration", 0) or 0)
    width = int(video_stream.get("width", 0)) if video_stream else None
    height = int(video_stream.get("height", 0)) if video_stream else None
    codec = video_stream.get("codec_name") if video_stream else None
    fps = _parse_fps(video_stream.get("r_frame_rate", "0/1")) if video_stream else None

    return {
        "duration": duration,
        "width": width,
        "height": height,
        "codec": codec,
        "fps": fps,
        "has_audio": 1 if audio_stream else 0,
        "metadata_json": json.dumps(data),
    }


def _parse_fps(r_frame_rate: str) -> float | None:
    """Parse '30000/1001' style fps string."""
    try:
        parts = r_frame_rate.split("/")
        if len(parts) == 2:
            num, den = float(parts[0]), float(parts[1])
            return round(num / den, 3) if den else None
        return float(r_frame_rate)
    except (ValueError, ZeroDivisionError):
        return None


def extract_frames(
    path: str | Path,
    output_dir: str | Path,
    duration: float,
    max_frames: int = 8,
    frame_width: int = 512,
) -> list[Path]:
    """
    Extract evenly-spaced JPEG frames from a video.
    Returns sorted list of frame paths.
    """
    path = Path(path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Determine how many frames to extract based on duration
    if duration < 10:
        n_frames = min(4, max_frames)
    elif duration < 30:
        n_frames = min(6, max_frames)
    else:
        n_frames = max_frames

    # For very short clips, always get at least 1 frame at midpoint
    if duration < 2:
        midpoint = duration / 2
        out_path = output_dir / "frame_0001.jpg"
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(midpoint),
            "-i", str(path),
            "-frames:v", "1",
            "-vf", f"scale={frame_width}:-1",
            "-q:v", "3",
            str(out_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg frame extraction failed for {path}: {result.stderr.strip()}")
        return [out_path] if out_path.exists() else []

    # Use select filter to pick evenly spaced frames
    # fps=N/duration selects N frames evenly across the video
    fps_filter = f"fps={n_frames}/{duration},scale={frame_width}:-1"
    out_pattern = str(output_dir / "frame_%04d.jpg")

    cmd = [
        "ffmpeg", "-y",
        "-i", str(path),
        "-vf", fps_filter,
        "-q:v", "3",
        out_pattern,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg frame extraction failed for {path}: {result.stderr.strip()}")

    frames = sorted(output_dir.glob("frame_*.jpg"))
    return frames


def extract_audio(path: str | Path, output_path: str | Path, timeout: int = 120) -> bool:
    """
    Extract audio stream to a WAV file for transcription.
    Returns True if audio was extracted, False if no audio stream exists.
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", str(path),
        "-vn",               # no video
        "-acodec", "pcm_s16le",
        "-ar", "16000",      # 16kHz for whisper
        "-ac", "1",          # mono
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        # Check if it's just "no audio stream" rather than a real error
        if "no audio" in result.stderr.lower() or "does not contain" in result.stderr.lower():
            return False
        raise RuntimeError(f"ffmpeg audio extraction failed for {path}: {result.stderr.strip()}")
    return Path(output_path).exists() and Path(output_path).stat().st_size > 0
