# vanal — Video Analyzer & Reel Arranger

A local-first tool for ingesting, analyzing, and curating short video clips. Point it at a folder of videos and it will:

- Extract key frames and generate per-clip synopses and tags using a local vision LLM (via [Ollama](https://ollama.ai))
- Store everything in a local SQLite database with full-text search
- Serve a web UI for browsing, searching, tagging, and reordering clips
- Suggest narrative clip orderings using an LLM
- Render montages (concatenated clip sequences) via FFmpeg
- Download all videos from one or more [Sora](https://sora.com) accounts (`sora_download.py`)

All AI inference runs **locally** through Ollama — no cloud API required for ingestion or the web UI.

---

## Prerequisites

| Dependency | Version | Purpose |
|---|---|---|
| Python | 3.11+ | Runtime |
| [FFmpeg](https://ffmpeg.org/download.html) + ffprobe | any recent | Frame extraction, audio extraction, montage rendering |
| [Ollama](https://ollama.ai) | latest | Local LLM inference |
| openai-whisper | optional | Audio transcription |

### Install FFmpeg

```bash
# macOS
brew install ffmpeg

# Ubuntu / Debian
sudo apt install ffmpeg

# Windows — download from https://ffmpeg.org/download.html and add to PATH
```

### Install and configure Ollama

```bash
# Install Ollama (see https://ollama.ai for platform-specific instructions)
curl -fsSL https://ollama.ai/install.sh | sh

# Pull the models used by default
ollama pull moondream   # vision model — describes video frames
ollama pull llama3      # text model — writes synopses, generates tags, orders clips
```

You can substitute any Ollama-compatible vision and text model; update `VISION_MODEL` and `TEXT_MODEL` in `.env` accordingly. Tested combinations:

- Vision: `moondream`, `llava`
- Text: `llama3`, `mistral`

---

## Installation

```bash
git clone https://github.com/loujacob66/vanal.git
cd vanal

# Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# Install Python dependencies
pip install -r requirements.txt

# Optional: enable audio transcription
pip install openai-whisper
```

---

## Configuration

```bash
cp .env.example .env
```

Edit `.env` to match your setup:

```env
# Local Ollama server (default port)
OLLAMA_URL=http://localhost:11434
VISION_MODEL=moondream       # ollama pull moondream
TEXT_MODEL=llama3            # ollama pull llama3

# Storage paths
# DATABASE_URL and FRAMES_DIR are relative to the project directory
DATABASE_URL=data/vanal.db
FRAMES_DIR=frames
# OUTPUT_DIR is where rendered montages are written — use an absolute path
# if your target is a NAS, external drive, or a directory outside the project
OUTPUT_DIR=./outputs
# Examples:
#   OUTPUT_DIR=/mnt/nas/videos/exports
#   OUTPUT_DIR=/Volumes/Media/vanal-outputs

# Frame extraction
MAX_FRAMES_PER_CLIP=3        # frames to sample per video
FRAME_WIDTH=256              # JPEG width (height scales proportionally)

# Ingestion pacing (seconds between clips; give Ollama breathing room)
INGEST_DELAY_SECS=1.0

# Transcription — requires: pip install openai-whisper
ENABLE_TRANSCRIPTION=false
WHISPER_MODEL=base           # tiny | base | small | medium | large

# Web UI password — leave blank to disable auth (fine for local use)
# Set a value if you expose the server on a network
WEB_PASSWORD=

# Sora downloader
# SORA_API_KEYS=sk-key1:AccountA,sk-key2:AccountB
SORA_DOWNLOAD_DIR=./sora_downloads
```

---

## Usage

Make sure Ollama is running (`ollama serve`) and your venv is active before running any commands.

### Ingest a directory of videos

Scans for video files, extracts frames, generates synopses and tags, and stores everything in the database.

Input videos can live anywhere — a local folder, a NAS mount, an external drive. The path is just passed directly to the command; files are never copied.

```bash
python run.py ingest /path/to/videos
# or an absolute path to a NAS / external drive:
python run.py ingest /mnt/nas/my-clips

# Keep extracted frames as thumbnails in the UI
python run.py ingest /path/to/videos --keep-frames

# Re-process clips that previously errored
python run.py ingest /path/to/videos --retry-errors

# Re-analyze everything from scratch
python run.py ingest /path/to/videos --reprocess-all
```

### Start the web UI

```bash
python run.py serve
# Open http://127.0.0.1:8000
```

Options:
```
--host 0.0.0.0    # bind to all interfaces (set WEB_PASSWORD if you do this)
--port 8080
--reload          # auto-reload on code changes (development)
```

### Remap file paths

The database stores absolute paths to your video files. If you move your library or a NAS remounts at a different path, use remap to update all stored paths in one shot:

```bash
python run.py remap /old/path/to/videos:/new/path/to/videos
```

### Download Sora videos

Downloads all videos from one or more OpenAI Sora accounts. Add your API key(s) to `.env`:

```env
SORA_API_KEYS=sk-proj-xxxx:MyAccount
```

```bash
python sora_download.py

# Preview what would be downloaded without writing files
python sora_download.py --dry-run

# Custom output directory
python sora_download.py --output /path/to/sora_downloads
```

---

## Web UI Features

- **Browse & search** — full-text search across synopses, tags, transcripts, and filenames
- **Tag management** — view and filter by auto-generated or manual tags
- **Notes** — add free-form notes to any clip
- **AI ordering** — ask the LLM to suggest a narrative sequence for selected clips
- **Export** — export clip list as JSON or an FFmpeg concat script
- **Render montage** — concatenate selected clips into a single output file via FFmpeg

---

## Project Layout

```
vanal/
├── run.py              # CLI entry point (ingest / serve / remap)
├── sora_download.py    # Sora bulk downloader
├── requirements.txt
├── .env.example        # Configuration template — copy to .env
├── vanal/              # Core library
│   ├── ingest.py       # Ingestion pipeline
│   ├── db.py           # SQLite schema and queries
│   ├── vision.py       # Ollama frame analysis
│   ├── extractor.py    # FFmpeg frame/audio extraction
│   ├── transcribe.py   # Whisper transcription (optional)
│   └── auth.py         # Simple password authentication
├── web/                # FastAPI web application
│   ├── app.py
│   ├── static/         # Frontend (HTML + vanilla JS)
│   └── api/            # REST endpoints
├── data/               # SQLite database (gitignored)
├── frames/             # Extracted frame cache (gitignored)
└── outputs/            # Rendered montages (gitignored)
```

---

## License

MIT
