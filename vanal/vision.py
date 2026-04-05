import base64
import json
import os
import re
import time
from pathlib import Path

import requests

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
VISION_MODEL = os.getenv("VISION_MODEL", "moondream")
TEXT_MODEL = os.getenv("TEXT_MODEL", "llama3")
MAX_RETRIES = 3
RETRY_DELAY = 3  # seconds


def _encode_image(path: Path) -> str:
    with open(path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode()


def _parse_json_response(text: str) -> dict | list:
    """Strip markdown fences and parse JSON from LLM response."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text.strip())


def _ollama_generate(model: str, prompt: str, images: list[str] | None = None, json_mode: bool = False) -> str:
    """Call Ollama generate API and return the response text."""
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.3,
        },
    }
    if json_mode:
        payload["format"] = "json"
    if images:
        payload["images"] = images

    resp = requests.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=600)
    resp.raise_for_status()
    return resp.json()["response"]


def _describe_single_frame(frame_path: Path, index: int, total: int, filename: str) -> str:
    """Describe a single frame using the vision model. Returns a text description."""
    image_b64 = _encode_image(frame_path)
    prompt = (
        f"This is frame {index + 1} of {total} from a short video clip "
        f"named '{filename}'. The video may be real footage or AI-generated, and may "
        f"show food, people, places, products, or abstract visuals. "
        f"Describe what you see accurately in one concise sentence. "
        f"If you see food or a decorated cake, say so — do not describe food items as urns or vases."
    )

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            return _ollama_generate(VISION_MODEL, prompt, images=[image_b64]).strip()
        except requests.RequestException as e:
            last_error = e
            print(f"    Frame {index + 1} failed (attempt {attempt + 1}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)

    return f"(failed to describe frame {index + 1}: {last_error})"


def describe_frames(frame_paths: list[Path], filename: str, transcript: str | None = None, on_progress=None) -> dict:
    """
    Describe each frame individually, then synthesize a synopsis and generate tags.
    If a transcript is provided it is included in the synopsis prompt.
    Returns {"frames": ["desc1", ...], "synopsis": "...", "tags": ["tag1", ...]}
    on_progress(step, current, total) is called after each frame and during synopsis/tag steps.
    """
    # Step 1: Describe each frame one at a time (much faster on CPU)
    total = len(frame_paths)
    frame_descriptions = []
    for i, frame_path in enumerate(frame_paths):
        if on_progress:
            on_progress("frames", i + 1, total)
        print(f"    Frame {i + 1}/{total}...", end=" ", flush=True)
        desc = _describe_single_frame(frame_path, i, total, filename)
        frame_descriptions.append(desc)
        print(f"OK")

    # Step 2: Synthesize synopsis using the text model (no images needed)
    descriptions_text = "\n".join(
        f"Frame {i + 1}: {d}" for i, d in enumerate(frame_descriptions)
    )

    transcript_section = (
        f"\n\nAudio transcript:\n\"{transcript}\"\n"
        if transcript else ""
    )

    synopsis_prompt = (
        f"These are descriptions of {len(frame_descriptions)} frames from a short "
        f"video clip named '{filename}':\n\n"
        f"{descriptions_text}"
        f"{transcript_section}\n\n"
        "Using both the visual descriptions and the transcript (if provided), write a "
        "2-3 sentence synopsis of this clip that captures its subject matter, mood, "
        "and key message. Respond with ONLY the synopsis text, nothing else."
    )

    if on_progress:
        on_progress("synopsis", 0, 0)
    synopsis = ""
    for attempt in range(MAX_RETRIES):
        try:
            synopsis = _ollama_generate(TEXT_MODEL, synopsis_prompt).strip()
            break
        except requests.RequestException as e:
            print(f"  Synopsis generation failed (attempt {attempt + 1}): {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)

    if not synopsis:
        synopsis = " ".join(frame_descriptions)

    # Step 3: Generate content tags
    if on_progress:
        on_progress("tagging", 0, 0)
    tags = generate_tags(frame_descriptions, synopsis, transcript, filename)

    return {"frames": frame_descriptions, "synopsis": synopsis, "tags": tags}


def generate_tags(
    frame_descriptions: list[str],
    synopsis: str,
    transcript: str | None,
    filename: str,
) -> list[str]:
    """
    Ask the text model to produce a list of descriptive content tags for a clip.
    Returns a list of lowercase tag strings.
    """
    transcript_section = f' Transcript: "{transcript}"' if transcript else ""
    prompt = (
        f'Tag this video clip for a searchable library.\n'
        f'Filename: {filename}\n'
        f'Description: {synopsis}{transcript_section}\n\n'
        f'Return a JSON array of 5-15 lowercase tags (setting, subjects, actions, mood, notable content).\n'
        f'Example: ["kitchen", "woman", "cooking", "calm", "indoor"]\n'
        f'Tags:'
    )

    for attempt in range(MAX_RETRIES):
        try:
            response_text = _ollama_generate(TEXT_MODEL, prompt, json_mode=True).strip()
            if not response_text:
                raise ValueError("Empty response from model")
            parsed = _parse_json_response(response_text)
            if isinstance(parsed, dict):
                parsed = parsed.get("tags", parsed.get("Tags", []))
            if isinstance(parsed, list):
                # Clean and deduplicate
                tags = list(dict.fromkeys(
                    t.lower().strip()
                    for t in parsed
                    if isinstance(t, str) and t.strip()
                ))
                return tags
        except (json.JSONDecodeError, ValueError) as e:
            print(f"  Tag parsing failed (attempt {attempt + 1}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
        except requests.RequestException as e:
            print(f"  Tag generation failed (attempt {attempt + 1}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)

    return []


def suggest_ordering(clips: list[dict]) -> list[dict]:
    """
    Ask a local LLM to suggest a narrative ordering for all clips.
    clips: list of {"id": int, "filename": str, "synopsis": str}
    Returns: [{"id": int, "rationale": str}, ...] in suggested order
    """
    clip_lines = "\n".join(
        f'[{c["id"]}] {c["filename"]}: {c["synopsis"] or "(no synopsis)"}'
        for c in clips
    )

    prompt = (
        "You are helping arrange a video reel of short clips into a "
        "compelling narrative sequence.\n\n"
        "Here are all the clips with their IDs and descriptions:\n\n"
        f"{clip_lines}\n\n"
        "Arrange these into a sequence that flows well visually and narratively — "
        "consider pacing, mood transitions, visual similarity, and thematic arcs.\n\n"
        "Return ONLY valid JSON: an array of objects in your suggested order, "
        "one entry per clip, including every clip exactly once:\n"
        '[{"id": 42, "rationale": "brief reason for this position"}, ...]'
    )

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            response_text = _ollama_generate(TEXT_MODEL, prompt, json_mode=True)
            result = _parse_json_response(response_text)
            if isinstance(result, dict):
                # Ollama often wraps arrays: {"clips": [...]} or {"ordering": [...]}
                for v in result.values():
                    if isinstance(v, list):
                        result = v
                        break
            if not isinstance(result, list):
                raise ValueError("Expected a JSON array")
            return result
        except (json.JSONDecodeError, ValueError) as e:
            last_error = e
            print(f"  JSON parse failed (attempt {attempt + 1}/{MAX_RETRIES}), retrying...")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
        except requests.RequestException as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)

    raise RuntimeError(f"Ordering failed after {MAX_RETRIES} attempts: {last_error}")
