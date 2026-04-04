"""
Optional Whisper-based audio transcription.
Only imported/used when ENABLE_TRANSCRIPTION=true in environment.
Requires: pip install openai-whisper
"""
import os
import tempfile
from pathlib import Path

from vanal.extractor import extract_audio

WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
NO_SPEECH_THRESHOLD = float(os.getenv("WHISPER_NO_SPEECH_THRESHOLD", "0.6"))


def transcribe_audio(video_path: str | Path) -> str | None:
    """
    Extract audio from video and transcribe with Whisper.
    Returns transcript text, or None if no audio / no speech detected.
    """
    try:
        import whisper
    except ImportError:
        raise ImportError(
            "openai-whisper is not installed. "
            "Run: pip install openai-whisper\n"
            "Or set ENABLE_TRANSCRIPTION=false to skip transcription."
        )

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        has_audio = extract_audio(video_path, tmp_path)
        if not has_audio:
            return None

        model = whisper.load_model(WHISPER_MODEL)
        result = model.transcribe(
            tmp_path,
            fp16=False,
            no_speech_threshold=NO_SPEECH_THRESHOLD,
        )
        # Filter out segments flagged as non-speech even with lower threshold
        segments = result.get("segments", [])
        if segments:
            text = " ".join(
                s["text"].strip()
                for s in segments
                if s.get("no_speech_prob", 0) < NO_SPEECH_THRESHOLD
            ).strip()
        else:
            text = result.get("text", "").strip()
        return text if text else None
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
