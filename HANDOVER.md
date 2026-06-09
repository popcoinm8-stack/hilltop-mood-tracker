# Mood Tracker — Handover Document

## What this is
A local-first personal app for a neurodivergent / AuDHD adult to track emotional regulation over time. The user writes a daily entry, a local LLM generates a draft observation, everything is stored locally in SQLite. Zero telemetry, zero outbound network beyond localhost.

## Stack (locked)
- **Backend:** Python 3.13 + FastAPI, served at localhost (default port 8000)
- **Storage:** SQLite (`mood.db`) — stored at `mood-tracker/data/mood.db`
- **LLM:** Ollama, model `qwen3.6:35b-a3b` on `localhost:11434`
- **Frontend:** Single HTML file, no build step (`app/static/index.html`)
- **Voice:** faster-whisper `large-v3-turbo`, on-demand load, idle-unload after ~2 min (snappy for back-to-back dictation, frees when idle). VAD (Silero) enabled. GPU: `float16` (RTX 5080 Blackwell — `int8` crashes; cuBLAS not supported for sm_120). Audio processed entirely in memory — never written to disk; discarded after successful transcription.
- **TTS:** Kokoro (82M, `bf_emma` British English female, CPU-only). Loaded once on first use, stays resident. Served as WAV via `/speak`. Audio generated in memory, played in browser, never persisted.
- **No** sliders, no body map, no charts — those are deferred future slices

## File structure
```
mood-tracker/
├── data/                    # SQLite DB lives here, outside the watched source tree
│   └── mood.db              # (created on first run)
├── kokoro/                  # Kokoro TTS model files (downloaded separately)
│   ├── kokoro-v1.0.fp16.onnx
│   └── voices-v1.0.bin
├── venv/                    # Python virtual env
├── requirements.txt         # fastapi, uvicorn[standard], httpx, faster-whisper, kokoro-onnx
└── app/
    ├── main.py              # FastAPI app + routes
    ├── database.py          # SQLite layer
    ├── llm.py               # Ollama call helper + prompts
    ├── transcribe.py        # faster-whisper on-demand transcription
    ├── tts.py               # Kokoro TTS, CPU-resident
    └── static/
        └── index.html       # Single-page frontend
```

## How to run
1. Make sure Ollama is running (`ollama serve`)
2. Activate the venv and start the server:
   ```
   cd mood-tracker
   .\venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
   ```
   **Note:** Do not use `--reload` for normal use. The database is in `data/` specifically so that WAL/DB writes do not trigger the reloader. If you need `--reload` during development, add `--reload-exclude "*.db*"` to exclude the database files.
3. Open `http://localhost:8000`

If port 8000 is already in use, try `--port 8001` or another port.

## Core loop
1. User types or dictates notes (mic button → webm/opus → `/transcribe`)
2. User selects **Quick** (3-4 sentence draft) or **Detailed** (structured: what happened / how it landed / possible pattern / question to sit with)
3. User clicks "Save & reflect"
4. Backend calls Ollama with the notes + last 7 days of kept summaries as context
5. Draft appears in the editable textarea below
6. User edits if needed, then clicks "Save this version"
7. The kept summary is stored and fed into future context windows

## API endpoints

### `POST /reflect`
**Body:** `{ "notes": "...", "mode": "quick" | "detailed" }`
**Response:** `{ "entry_id": int, "draft": string, "mode": string }`
Saves the entry (notes + draft) and returns the draft. If an entry for today already exists, it updates that row. The draft is NOT yet the "kept" version.

### `POST /save-summary`
**Body:** `{ "entry_id": int, "kept_summary": "..." }`
Saves the user-edited draft as the `kept_summary` for that entry. This is what gets fed back into future context windows.

### `POST /speak`
**Body:** `{ "text": "..." }`
**Response:** `audio/wav` — WAV PCM from Kokoro TTS. Read-aloud button in the draft area triggers this. CPU-only (keeps GPU free for Whisper/LLM). Audio generated in memory, played in browser, never written to disk.

### `GET /export`
Returns all entries as JSON for backup. Downloaded as `mood-export.json`.

### `POST /transcribe`
**Body:** raw `audio/webm` (no JSON wrapper). Accepts webm/opus from `MediaRecorder`. Supports multi-minute recordings.
**Response:** `{ "text": "..." }` on success; HTTP 500 on decode/transcribe failure.
On failure: audio is retained in the browser and a Retry button is shown — no recording is silently lost.
Audio never leaves the machine: decoded in-memory with PyAV, transcribed, discarded.

### `GET /`
Serves the frontend HTML.

## Database schema
```sql
CREATE TABLE entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_date DATE NOT NULL UNIQUE,   -- one entry per day
    notes TEXT NOT NULL,
    draft TEXT NOT NULL,
    kept_summary TEXT,                 -- user-edited version, NULL until saved
    mode TEXT NOT NULL DEFAULT 'quick', -- 'quick' or 'detailed'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
```

## LLM configuration
- **Model:** `qwen3.6:35b-a3b`
- **Think mode disabled:** `"think": false` — this is important. The model defaults to a thinking mode that puts output in a separate `thinking` JSON field rather than `response`. Disabling it ensures the draft comes back in the `response` field.
- **Temperature:** 0.6
- **Max tokens:** 256 (quick), 512 (detailed)
- **System prompts** are in `llm.py`:
  - `QUICK_PROMPT` — for the 3-4 sentence mode
  - `DETAILED_PROMPT` — for the structured mode

## Design principles
- Local-only, zero egress beyond localhost
- No guilt mechanics (no streaks, no nagging)
- Generated text is always a DRAFT for the user to check — never a verdict or diagnosis
- Tone: grounded and plain. No cheerleading, no saccharine reassurance
- Data is owned by the user — JSON export available

## Things that caused problems during development (to avoid)
- **Port conflicts:** If port 8000 is in use, use a different port. Zombies from crashed uvicorn processes can linger on Windows.
- **Qwen think mode:** The model puts its output in a `thinking` field by default. Must pass `"think": false`.
- **DB locking:** Always use the context manager pattern for SQLite connections (`with get_connection() as conn`). Never hold connections across await points.
- **Pycache corruption:** On Windows, sometimes the Write tool doesn't fully overwrite a file. Always verify `return text` is present at the end of `llm.py`.
- **OOM with large model:** `qwen3.6:35b-a3b` is a 35B model. First run loads it into memory (several GB). Subsequent runs are faster. If Ollama crashes with OOM, the model may need to be re-pulled.
- **faster-whisper int8 on Blackwell:** `compute_type="int8"` crashes on RTX 5080 (cuBLAS not supported for sm_120). Use `float16`. If that also fails, try `int8_float16`, then whisper.cpp.
- **Model unloads:** `keep_alive: 0` in Ollama requests + shutdown handler means the LLM unloads after each call. Whisper model idles out after 2 min of inactivity. No permanent VRAM hold.
- **Audio never persisted:** Audio is held in browser memory during recording and retry attempts. The server never writes audio to disk.
- **Kokoro model files:** Download from `github.com/thewh1teagle/kokoro-onnx/releases` — `kokoro-v1.0.fp16.onnx` (169 MB) and `voices-v1.0.bin` (26 MB). Place in `kokoro/` at project root.
- **Switching TTS voice:** Change `VOICE` in `app/tts.py`. Run `python -c "from app.tts import kokoro; k = kokoro._get_kokoro(); print(sorted(k.voices.keys()))"` to see all available voices.

## Deferred future slices (do NOT build now)
- Sliders/check-in dimensions (energy, mood, anxiety scales)
- Body map
- Strategy library
- Charts/patterns view
- History view (browsing past entries)

## Adding a new reflection mode
1. Add the prompt string in `llm.py` alongside `QUICK_PROMPT` and `DETAILED_PROMPT`
2. Add the instruction text to the `build_prompt` function's conditional
3. Add the mode name to the validation check in `main.py` (`mode in ("quick", "detailed", "new_mode")`)
4. Add a frontend toggle button in `index.html` and pass the mode in the fetch body
5. Consider whether `max_tokens` needs adjusting
