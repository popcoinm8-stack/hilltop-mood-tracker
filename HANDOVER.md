# Mood Tracker — Project Handover

**Last updated:** June 2026 (Phase A + Phase B + Phase C + Phase D + Phase E + Phase F + Phase G)
**Stack:** FastAPI + SQLite + vanilla JS (no frontend framework) + Ollama (local LLM, optional) + Yuxor cloud BYOK + Whisper (local STT) + Kokoro (local TTS)
**Location:** `C:\Users\ExSpo\OneDrive\Desktop\Mood tracker tool\mood-tracker\`

---

## Project Overview

A private, local-first daily journaling app designed specifically for a neurodivergent adult. The default is entirely offline — no data leaves the machine. An optional BYOK cloud provider (Yuxor) can be enabled for faster or more capable model inference. Data (SQLite DB, exports, analysis files) stays on-device in all cases.

**Core value proposition:** A quiet, private space to document daily experience with optional AI reflection, structured tracking (signals, tags, spoons), clinician appointment preparation, and trend analysis.

---

## Architecture

```
Browser (JS SPA)
    │
    ▼
FastAPI server (app/main.py)   ──►  SQLite DB (data/mood.db)
    │                                   │
    ├── app/llm.py ──► Ollama :11434 (local, optional)
    │              └──► Yuxor gateway   (cloud, BYOK, optional)
    ├── app/transcribe.py ──► faster-whisper (local STT)
    └── app/tts.py ──► Kokoro ONNX (local TTS)
```

**File structure:**
```
mood-tracker/
├── run.py                  # Entry point; starts uvicorn + handles clean shutdown
├── requirements.txt        # Minimal dependencies (fastapi, uvicorn, httpx)
├── data/
│   ├── config.json         # Secrets (gitignored) — transport, base_url, api_key, model
│   ├── config.example      # Template with placeholders (committed to repo)
│   └── mood.db             # SQLite database (gitignored)
├── kokoro/                 # Kokoro TTS model files (gitignored)
├── app/
│   ├── main.py             # FastAPI app; all HTTP endpoints
│   ├── database.py         # All SQLite queries and schema migrations
│   ├── llm.py             # Pluggable model provider (Ollama + Yuxor)
│   ├── transcribe.py       # faster-whisper STT, in-memory audio
│   ├── tts.py             # Kokoro TTS, in-memory audio
│   └── static/
│       └── index.html     # Entire frontend: ~3500-line vanilla JS SPA
```

---

## Secrets and Configuration

**Location:** `data/config.json` (gitignored — never commit)

```json
{
  "transport": "ollama | openai_compatible",
  "base_url": "https://api.yuxor.tech/v1",
  "api_key": "sk-...",
  "model": "claude-opus-4-7",
  "request_timeout": 180,
  "model_overrides": {},
  "ollama_base_url": "http://localhost:11434"
}
```

- **API key** lives in this file only. It never reaches the browser, never appears in logs, and never appears in error messages returned to the client.
- Default transport is `"openai_compatible"` (cloud) — this is a deliberate default since it provides better model quality. Set to `"ollama"` for fully offline operation.
- **Hot-switching:** The active transport and model are stored in a mutable in-memory state object. `POST /settings` updates this state AND persists to disk. The switch takes effect on the very next LLM call — no server restart needed.
- **Per-job model overrides** via `model_overrides: { reflection, trends, clinician }` — assign different models per task (e.g. fast/cheap for daily reflections, strong model for clinician summaries).
- **Ollama unload on cloud switch:** When switching from ollama to cloud transport, a daemon thread fires a non-blocking `POST {ollama_base}/api/generate` with `keep_alive: 0` to drop the loaded model and free VRAM. Best-effort — no error if Ollama isn't running.

### Available Yuxor models

Via `GET /models`: `claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5`, `gpt-5.5`, `gpt-5.4`, `gpt-5.3-codex`, and date-stamped variants. Local Ollama models are listed via `GET /local-models`.

---

## Database Schema

**File:** `app/database.py`, `init_db()` function.

All migrations are **additive only** — `ALTER TABLE ADD COLUMN IF NOT EXISTS` checks before adding, so existing data is never destroyed. The base table creation uses `CREATE TABLE IF NOT EXISTS`.

### Table: `entries`

One row per day. `entry_date` is the unique key (one entry per day).

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| `id` | INTEGER | No | Primary key |
| `entry_date` | DATE | No | ISO date, UNIQUE constraint |
| `notes` | TEXT | No | User's written text; includes dictation + LLM reflection appended |
| `draft` | TEXT | No | Legacy column; kept for existing entries |
| `kept_summary` | TEXT | Yes | The AI-generated reflection (this is the key data for trends/clinician) |
| `mode` | TEXT | No | `"quick"` or `"detailed"` |
| `energy` | TEXT | Yes | Signal: `"low"`, `"med"`, or `"high"` |
| `sleep_quality` | TEXT | Yes | Signal: `"low"`, `"med"`, or `"high"` |
| `sensory_load` | TEXT | Yes | Signal: `"low"`, `"med"`, or `"high"` |
| `overwhelm` | TEXT | Yes | Signal: `"low"`, `"med"`, or `"high"` |
| `transcription` | TEXT | Yes | Raw voice dictation, stored verbatim for detail view |
| `working_notes` | TEXT | Yes | User annotations added after save |
| `reflection_status` | TEXT | Yes | `"ready"`, `"pending"`, or `"error"` — tracks async LLM reflection |
| `created_at` | TIMESTAMP | No | Auto-set at insert time |

### Table: `tags`

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| `id` | INTEGER | No | Primary key |
| `name` | TEXT | No | Tag name, UNIQUE within category (case-insensitive) |
| `category` | TEXT | No | Any short lowercase string (e.g. `"mood"`, `"work"`, `"people"`, `"self_care"`). Not limited to a fixed set — the AI creates categories dynamically. |

### Table: `entry_tags`

Many-to-many junction between `entries` and `tags`. Both columns form the composite primary key. CASCADE delete on both sides.

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| `entry_id` | INTEGER | No | FK → entries(id) |
| `tag_id` | INTEGER | No | FK → tags(id) |

### Table: `weekly_checkins`

One row per calendar week (keyed by Monday's date).

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| `id` | INTEGER | No | Primary key |
| `week_start` | DATE | No | UNIQUE; always a Monday |
| `spoons` | INTEGER | Yes | Spoons remaining today (0–12) |
| `meltdown_count` | INTEGER | Yes | Meltdowns this week |
| `shutdown_count` | INTEGER | Yes | Shutdowns this week |
| `notes` | TEXT | Yes | Free-text notes for the week |
| `created_at` | TIMESTAMP | No | Auto-set |

---

## API Endpoints

All endpoints are prefixed with `/`. JSON request/response unless noted.

### Settings endpoints

#### `GET /settings`
Returns full settings for the Settings UI. **Never returns the API key.** Returns `api_key_set: true/false` instead.

**Response:**
```json
{
  "transport": "openai_compatible",
  "base_url": "https://api.yuxor.tech/v1",
  "api_key_set": true,
  "model": "claude-opus-4-7",
  "request_timeout": 180,
  "model_overrides": {},
  "ollama_base_url": "http://localhost:11434"
}
```

---

#### `POST /settings`
Hot-switches the provider configuration. Takes effect immediately (no restart).

**Request body (all fields optional):**
```json
{
  "transport": "openai_compatible",
  "base_url": "https://api.yuxor.tech/v1",
  "api_key": "sk-...",
  "model": "claude-sonnet-4-6",
  "request_timeout": 180,
  "model_overrides": { "reflection": "claude-haiku-4-5" },
  "ollama_base_url": "http://localhost:11434"
}
```

**Response:** Same shape as `GET /settings` (key redacted).

---

#### `GET /models`
Proxy to the cloud provider's model list (`GET {base_url}/models`). Populates the Settings dropdown.

**Response:** `{ "models": ["claude-opus-4-7", "claude-sonnet-4-6", ...] }`

---

#### `GET /local-models`
Lists installed Ollama models (`GET {ollama_base_url}/api/tags`).

**Response:** `{ "models": ["qwen3.6:35b-a3b", "llama3:8b", ...] }`

---

#### `POST /test-connection`
Runs a minimal 1-word completion against the current config. Use to verify settings without leaving the app.

**Response (success):**
```json
{ "ok": true, "model_responded": "claude-opus-4-7", "response_preview": "OK" }
```
**Response (failure):**
```json
{ "ok": false, "error": "Authentication failed — check your API key." }
```

---

#### `GET /provider-info`
Returns active transport and model for the UI privacy badge. **Never returns the key.**

```json
{ "transport": "openai_compatible", "model": "claude-opus-4-7", "model_overrides": {} }
```

---

### `POST /reflect`
Saves an entry immediately with `reflection_status='pending'`, then generates the AI reflection in a background task. Returns instantly — the client polls `/reflect-status/{entry_id}` until the reflection is ready.

**Request body:**
```json
{
  "notes": "string",
  "transcription": "string or null",
  "mode": "quick | detailed",
  "energy": "low | med | high | null",
  "sleep_quality": "low | med | high | null",
  "sensory_load": "low | med | high | null",
  "overwhelm": "low | med | high | null"
}
```

**Response:** `{ "entry_id": 42, "mode": "quick", "autostruct_task_id": "uuid-string" }`

**Behaviour:**
1. Validates `notes` is non-empty
2. If `transcription` provided, appends `"\n\n[Dictation]: " + transcription` to notes
3. Saves to DB with `kept_summary=NULL`, `reflection_status='pending'`
4. Snapshots the active provider config (so a mid-flight switch doesn't affect this job)
5. Queues a FastAPI `BackgroundTask` that calls `generate_draft_from_snapshot()`
6. **Also queues an autostruct background task** that auto-generates tags and signals for the entry
7. On completion: updates `kept_summary` and sets `reflection_status='ready'`
8. On error: sets `reflection_status='error'` with a note

**Frontend:** After calling `/reflect`, the client polls `/reflect-status/{entry_id}` every 1.5 seconds (up to 2 minutes) until `status` is `ready` or `error`.

**Errors:** 400 if notes empty. Model errors are captured in the background task — the entry is never lost.

---

### `GET /reflect-status/{entry_id}`
Returns the current reflection status for an entry.

**Response:**
```json
{ "status": "ready", "kept_summary": "..." }
{ "status": "pending", "kept_summary": null }
{ "status": "error", "kept_summary": null }
```

---

### `POST /save-summary`
Updates the `kept_summary` field for an existing entry. Used by the clinician tab for post-hoc editing.

**Request body:** `{ "entry_id": 42, "kept_summary": "edited text" }`

---

### `POST /transcribe`
Accepts raw webm/opus audio blob in request body. Returns transcribed text.

**Request:** Raw bytes, `Content-Type: audio/webm` or similar.
**Response:** `{ "text": "the transcribed words" }`
**Behaviour:** Converts webm/opus → 16kHz mono PCM WAV in-memory using `av` (ffmpeg Python bindings), then runs `faster-whisper`. Model stays warm for 2 minutes after use. Auto-falls back to CPU if CUDA crashes at inference time.

---

### `POST /speak`
Converts text to speech. Kokoro TTS, CPU-only.

**Request body:** `{ "text": "hello" }`
**Response:** WAV audio bytes (`audio/wav`)

---

### `GET /export`
Returns all entries as JSON for backup.

**Response:** `{ "exported_at": "2026-06-09", "entries": [...] }`

---

### `GET /timeline`
Returns all entries with previews and signals.

**Response:**
```json
[{
  "entry_date": "2026-06-09",
  "notes_preview": "first 100 chars...",
  "mode": "quick",
  "has_kept_summary": true,
  "signals": { "energy": "med", "sleep_quality": null, ... }
}]
```

---

### `GET /tags`
Returns all tags in the database.

**Response:** `[{ "id": 1, "name": "Alice", "category": "people" }, ...]`

---

### `GET /tags/search?q=&category=`
Prefix-search for tags by name.

**Query params:** `q` (required), `category` (optional)
**Response:** `[{ "id": 1, "name": "Alice", "category": "people" }, ...]`

---

### `POST /entry-tags`
Creates or replaces all tags for an entry. Tags that don't exist in the `tags` table are auto-created. Categories are open-ended — any short lowercase string works.

**Request body:**
```json
{
  "entry_id": 42,
  "tags": [
    { "name": "stressed", "category": "mood" },
    { "name": "deadlines", "category": "work" },
    { "name": "Alice", "category": "people" }
  ]
}
```
Returns `{ "tags": [...] }`.

---

### `GET /entry-tags/{entry_id}`
Returns tags for a specific entry.

---

### `POST /working-notes`
Save/update working notes for an entry.

**Request body:** `{ "entry_id": 42, "working_notes": "my annotations..." }`

---

### `POST /weekly-checkin`
Create or update a weekly check-in record.

**Request body:**
```json
{
  "week_start": "2026-06-08",
  "spoons": 7,
  "meltdown_count": 1,
  "shutdown_count": 0,
  "notes": "hard week"
}
```

---

### `GET /weekly-checkins?weeks=12`
Returns weekly check-in records for the last N weeks.

---

### `POST /analyze-trends`
Calls the LLM to analyse kept summaries for a time window.

**Request body:** `{ "days": 180 }` — use `0` for all time.
**Response:** `{ "trends": "...", "patterns": [...], "insights": "..." }`

---

### `POST /clinician-summary`
Calls the LLM to draft a plain-language paragraph for a clinician.

**Request body:** `{ "start_date": "2026-05-01", "end_date": "2026-06-09" }`
**Response:** `{ "draft": "...", "entry_count": 30, "date_range": "2026-05-01 to 2026-06-09" }`

---

### `POST /clinician-export`
Returns the clinician draft as a Markdown file download.

**Request body:** `{ "draft": "...", "start_date": "...", "end_date": "...", "generated_date": "..." }`
**Response:** Markdown file (`text/markdown`, `Content-Disposition: attachment`)

---

### `GET /stats`
Returns aggregate statistics.

**Response:**
```json
{
  "total_entries": 42,
  "entries_180d": 38,
  "quick_count": 30,
  "detailed_count": 12,
  "avg_kept_summaries_pct": "85%"
}
```

---

### `POST /autostruct`
Suggest tags and signal levels from a journal entry text. Async — returns immediately with a task ID. The LLM can invent any tag category it thinks fits (mood, work, health, self_care, relationships, finances, etc.) — not limited to the legacy four.

**Request body:** `{ "entry_text": "string" }`

**Response:** `{ "task_id": "uuid-string" }`

---

### `POST /autostruct-rerun/{entry_id}`
Re-run autostruct for an existing entry. Use when the user clicks the "Re-suggest tags" button on a saved entry. Generates new tags, replaces the entry's tag list, fills in any missing signal values, and returns a task ID to poll.

**Response:** `{ "task_id": "uuid-string" }`

---

### `GET /autostruct-status/{task_id}`
Poll the result of an auto-struct task.

**Response (ready):** `{ "status": "ready", "result": { "signals": {...}, "tags": {...} }, "entry_id": 42, "applied": true }`
**Response (pending):** `{ "status": "pending" }`
**Response (error):** `{ "status": "error", "error": "..." }`

`tags` is a flat dict of category → [tag, tag, ...] — categories are open-ended, the model creates them dynamically.

---

### `GET /tags/categories`
Return all distinct tag categories currently in the database.

**Response:** `["mood", "people", "self_care", "work", ...]`

---

### `GET /signal-series`
Return numeric signal series for chart rendering. Deterministic — no model call.

**Query params:** `days` (default 90)

**Response:**
```json
{
  "series": [{ "date": "2026-06-09", "energy": 2, "sleep_quality": 3, "sensory_load": null, "overwhelm": 1 }, ...],
  "weekly_checkins": [...]
}
```
Values: `low=1, med=2, high=3, null=no data`.

---

### `POST /ask`
Answer a natural-language question about journal entries. Async — returns a task ID; poll `/ask-status/{task_id}`.

**Request body:**
```json
{
  "question": "What patterns do you see in my sleep?",
  "keywords": [],
  "date_hints": [],
  "tag_names": []
}
```

**Response:** `{ "task_id": "uuid-string" }`

---

### `GET /ask-status/{task_id}`
Poll the result of an ask-journal task.

**Response (ready):**
```json
{
  "status": "ready",
  "result": {
    "answer": "...",
    "cited_dates": ["2026-06-09", "2026-06-07"],
    "method": "keyword",
    "token_count": 234
  }
}
```
`method` is `"keyword"` (fallback) or `"embeddings"` (if provider supports `/embeddings`).

---

### `GET /embeddings-info`
Return whether the provider exposes a working `/embeddings` endpoint.

**Response:** `{ "embeddings_available": false, "embeddings_enabled": true }`

---

### `GET /correlations`
Return computed correlation statistics. Deterministic — no model call.

**Query params:** `days` (default 180)

**Response:** `{ tag_signal_stats: [...], baseline: {...}, lead_lag: {...}, date_range: "..." }`

---

### `POST /narrate-correlations`
Compute correlation stats then narrate them via LLM. Async — returns task ID; poll `/narrate-correlations-status/{task_id}`.

**Request body:** `{ "days": 180 }`

**Response:** `{ "task_id": "uuid-string" }`

---

### `GET /narrate-correlations-status/{task_id}`
Poll correlation narration result.

**Response (ready):** `{ "status": "ready", "result": { "narration": "...", "stats": {...} } }`

---

### `POST /clinician-summary` (upgraded — Phase E)
Now includes weekly check-in data and uses the structured V2 format.

**Request:** `{ "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD" }`

**Response:** `{ "draft": "...", "entry_count": N, "checkin_count": N, "date_range": "..." }`

---

## Phase E Changelog — Clinician Prep Upgrade

### `app/llm.py`
- Added `CLINICIAN_SYSTEM_PROMPT_V2` — structured output format with sections: Overall Picture, Notable Changes, Meltdowns/Shutdowns, Things to Bring, Timeline of Notable Entries, Suggested Questions
- Updated `generate_clinician_summary()` — added `weekly_checkins` parameter; feeds check-in data (meltdowns, shutdowns, spoons) to the model; signals included per entry; notable entries auto-selected
- Updated `generate_clinician_summary_from_snapshot()` — same upgrade, uses V2 prompt

### `app/database.py`
- Added `get_weekly_checkins_in_range()` — returns check-ins overlapping a date range

### `app/main.py`
- `POST /clinician-summary` now calls `get_weekly_checkins_in_range()` and passes check-ins to `generate_clinician_summary()`
- Response includes `checkin_count`

### `app/static/index.html`
- Print view (`doClinicianPrintView`): parses `## ` headings and `- ` bullets from the draft and renders as proper semantic HTML (`<h2>`, `<ul>/<li>`, `<p>`) with a polished serif stylesheet
- Print stylesheet (`@media print`): hides all UI chrome, shows only the draft textarea rendered as clean serif text; `break-after: avoid` on headings; Georgia font
- Metadata display now shows both entry count and check-in count (e.g. "3 entries, 2 weeks of check-ins in range")
- "Since last appointment" and "Set today as last appointment" buttons now show a persistent hint below them

---

## Frontend (`app/static/index.html`)

A ~3500-line single-page application. No framework — vanilla JS with a large flat-file approach. All styles are in a `<style>` block at the top; all logic in a single `<script>` block at the bottom.

### Privacy badge

A persistent badge in the bottom-right corner shows whether AI is running locally or via cloud:
- Green dot + "Local model" when `transport: "ollama"`
- Blue dot + "Cloud: <model>" when `transport: "openai_compatible"`
- Refreshed on page load via `GET /provider-info`

### Tab structure

| Tab | Function | Key data |
|-----|----------|----------|
| Journal | Write daily entry | notes, signals, tags, voice dictation |
| Trends | AI trend analysis | kept summaries, time window |
| Clinician | Appointment prep | date range, LLM synthesis, Markdown export |
| Timeline | Browse all entries | date list, click for detail modal, status badge |
| Search | Full-text search | query, calendar heatmap |
| Stats | Aggregate numbers | entry counts |
| ND | Neurodivergent tracking | spoons, meltdowns, shutdowns, interoception |
| Breathe | Guided breathing | 478, box, 5-6, 4-6 breathing patterns |
| **Settings** | Provider configuration | transport, cloud/local settings, model dropdowns, test connection |

### State variables (top of script)

```javascript
let currentEntryId = null;
let currentMode = 'quick';                    // 'quick' or 'detailed'
let currentSignals = {                       // { energy, sleep_quality, sensory_load, overwhelm }
  energy: null, sleep_quality: null,
  sensory_load: null, overwhelm: null
};
let currentTags = [];                       // flat array of {name, category} — auto-populated by AI
window.lastTranscription = null;             // raw dictation captured before submit
let timelineData = null;                     // client-side cache for timeline
```

### Key frontend functions

| Function | Location | Purpose |
|----------|----------|---------|
| `showTab(name)` | ~1390 | Switch active tab, trigger load functions |
| `doReflect()` | ~1770 | Submit entry; polls `/reflect-status`; triggers autostruct for AI tags |
| `setSignal(btn)` | ~1845 | Toggle Low/Med/High on a signal; updates UI and state |
| `renderTags()` | ~2980 | Render auto-generated tags grouped by category |
| `removeTagFromEntry(cat, name)` | ~3000 | Remove a tag from entry, POST updated list to server |
| `runAutoStruct()` | ~2830 | Re-run AI tag suggestion on demand |
| `runAutoStructForEntry(id)` | ~2870 | Re-run autostruct for an existing entry (timeline detail) |
| `pollAutoStruct(taskId)` | ~2910 | Poll `/autostruct-status` until ready, then refresh tag display |
| `openEntry(date)` | ~3130 | Open detail modal; shows pending/error/ready reflection state |
| `saveWorkingNotes(entryId)` | ~3185 | Save working notes from modal |
| `initNDTab()` | ~2975 | Load current week's checkin, load 12-week history |
| `renderCalendarHeatmap()` | ~2860 | 90-day grid of colored cells; click opens entry |
| `updateProviderBadge()` | ~3490 | Fetch `/provider-info` and update privacy badge |
| `escHtml(str)` | ~1365 | HTML-escape utility used everywhere |
| `initSettingsTab()` | ~3540 | Load settings, populate transport, model dropdowns |
| `selectTransport(t)` | ~3520 | Toggle transport card, show/hide local/cloud panels |
| `loadLocalModels(selected)` | ~3560 | Fetch Ollama models, populate dropdown |
| `loadCloudModels(selected)` | ~3570 | Fetch cloud models, populate dropdown with optgroups |
| `saveSettings()` | ~3600 | POST to `/settings`, hot-switch, refresh badge |
| `testSettingsConnection()` | ~3630 | POST to `/test-connection`, show result |

---

## LLM System Prompts

**File:** `app/llm.py`

All three public functions delegate to a single internal `complete(system_prompt, user_text, job, max_tokens, temperature)` helper that dispatches on transport. The prompts and output behaviour are identical regardless of transport.

### `generate_draft()` — daily reflection

Used for `/reflect`. Two modes:

**Quick prompt (3-4 sentence observation):**
> You are a reflection aid for a neurodivergent adult. Given their daily notes and a short history of recent days, write a brief observation of 3-4 sentences. It is a draft for them to check, not a verdict. No diagnosis. Do not reassure or cheerlead for its own sake. Plain, grounded language. If you notice a possible pattern — including across recent days — raise it as a question, not a conclusion.

**Detailed prompt (4-section structured reflection):**
> 1. What happened — 1-2 sentences describing what the notes describe.
> 2. How it landed — 1-2 sentences on the emotional impact or tone.
> 3. Possible pattern — If you notice something that connects across recent days, raise it as a question. If nothing connects, just say so briefly.
> 4. Question to sit with — One short question worth holding, not answering.

History block appended: last 7 days of kept summaries (newest first).

### `analyze_trends()` — trends engine

Calls LLM with all kept summaries in the time window. Asks for: mood trends, recurring patterns/triggers, protective factors, positive deltas, anything notable. Returns JSON (falls back to raw text in `trends` field if parsing fails).

### `generate_clinician_summary()` — appointment prep

Builds entry text preferring `transcription` over `notes`, always appending `kept_summary`. System prompt instructs: plain English, no pathologising, no speculation beyond entries, no motivational language. Structure: overall feeling, patterns/changes, things to flag. Max 600 tokens.

---

## Setup and Running

### Prerequisites

1. **Python 3.11+** with a virtual environment.

2. **Ollama** (optional, for local/offline mode):
   - Pull the model: `ollama pull qwen3.6:35b-a3b`
   - `ollama serve` on port 11434
   - Set `transport: "ollama"` in `data/config.json`

3. **Yuxor API key** (optional, for cloud mode):
   - Sign up at yuxor.tech (or equivalent)
   - Set `transport: "openai_compatible"` in `data/config.json`
   - Set `api_key` to your key
   - Set `model` to a model ID from the available list (e.g. `claude-opus-4-7`)

### Install

```bash
cd mood-tracker
python -m venv venv
.\venv\Scripts\activate   # Windows
# pip install -r requirements.txt   # if needed

# First run: server starts with whatever transport is in data/config.json
.\venv\Scripts\python.exe run.py
```

Open http://localhost:8000 in your browser.

### Model configuration

Edit `data/config.json` (gitignored):

```json
{
  "transport": "openai_compatible",
  "base_url": "https://api.yuxor.tech/v1",
  "api_key": "sk-your-key-here",
  "model": "claude-opus-4-7",
  "request_timeout": 180,
  "model_overrides": {
    "reflection": "claude-haiku-4-5"
  }
}
```

### Switching transport

Change `transport` in `data/config.json` and restart `.\venv\Scripts\python.exe run.py`.

### TTS voice

Edit `app/tts.py`:
```python
VOICE = "bf_emma"   # British English female
```

### STT model

Edit `app/transcribe.py`:
```python
model = WhisperModel("large-v3-turbo", ...)  # change model name
```

### Network mode (LAN / Tailscale access)

By default the app binds to `127.0.0.1` and is only reachable on the local machine. To expose it to other devices on your network:

```bash
.\venv\Scripts\python.exe run.py --network
```

This starts the server in network mode with:
- HTTPS via an auto-generated self-signed cert (`data/tls/`)
- Access password (set during first-time setup via the UI)
- Device whitelisting (approve phone devices from the desktop Settings panel)
- LAN IP filter (RFC 1918 + Tailscale `100.64.0.0/10`, no public internet)
- Rate limiting (5 login attempts/min/IP)

To access from outside your home network, install [Tailscale](https://tailscale.com) on both devices. The server automatically detects the Tailscale IP and includes it in the SAN list of the cert. No router changes required.

See [Docs/NETWORK_MODE_HOWTO.md](Docs/NETWORK_MODE_HOWTO.md) for the full guide.

---

## Design Decisions and Rationale

### Why a cloud option now?

Ollama is free and offline but constrained by local hardware. Yuxor provides access to stronger models with no local GPU requirement. The default is still Ollama (offline, no data leaves the machine). The cloud transport is opt-in via config. The API key is never sent to the browser.

### Why no frontend framework?

The SPA is a single `index.html` with vanilla JS. This avoids build tooling, npm, bundlers, and framework lock-in. It's slower to develop with but trivial to deploy — copy the file and it works. State is managed via plain JS variables.

### Why SQLite?

Single-file, zero-configuration, works offline, WAL mode for concurrent reads during writes. Sufficient for this scale (thousands of entries). No server to manage.

### Why additive-only migrations?

Schema changes always use `IF NOT EXISTS` / `IF NOT IN (columns)` checks. This means existing databases are never broken by new code deployments. Old entries always get NULL/empty values for new columns.

### Why SSE streaming for Yuxor?

Yuxor's `/chat/completions` endpoint returns Server-Sent Events (SSE) by default. The streaming response format (`data: {...}` lines) is parsed client-side to accumulate content. Ollama uses its own streaming format (`/api/generate` with `stream: false`).

### Why typed errors in llm.py?

`LLMError` and subclasses (`LLMTimeoutError`, `LLMAuthError`, `LLMRateLimitError`, `LLMConnectionError`) are mapped from HTTP status codes and network failures. They produce clean, human-readable messages for the browser — no raw stack traces, no API key in the output.

---

## Known Limitations

- **Encryption at rest (Phase S2).** The SQLite database is encrypted with SQLCipher (pysqlcipher3). The passphrase-derived key (scrypt, N=2^14, r=8, p=1) is held in memory only while the vault is unlocked. The cloud API key is encrypted under the same passphrase. A one-time recovery key is generated at setup and hashed (SHA-256) for storage — the plaintext is shown once. Lost passphrase + lost recovery key = unrecoverable data.
- **No multi-user support.** Single local user only.
- **No data migration scripts.** The `init_db()` migrations are one-way additive. There's no downgrade path.
- **LLM prompts are hardcoded strings.** Changing the reflection style requires editing `app/llm.py`.
- **No backup/restore UI.** The `/export` endpoint provides a manual JSON backup. There's no restore mechanism.
- **`/reflect` is synchronous.** The LLM call blocks the response. If the model is slow (especially with cloud latency), the UI waits. Phase 2 makes this asynchronous.

---

## Phase 1b Changelog — Hot-Switching, Ollama Unload, and Settings Page

### `app/llm.py`
- Replaced module-level frozen config with `_state` dict + `_state_lock` (thread-safe mutex)
- `_init_state()` loads config at module import; `_persist_config()` writes to disk
- `get_config()` — returns redacted copy of in-memory state
- `get_settings()` — returns full settings with `api_key_set` bool (never the key itself)
- `snapshot_config()` — returns frozen snapshot for background tasks
- `set_provider_config(updates)` — merges updates into state, persists to disk, fires async Ollama unload if switching ollama→cloud
- `_fire_ollama_unload(old_state)` — daemon thread, non-blocking, best-effort
- `complete_from_snapshot(snapshot, ...)` and all `*_from_snapshot()` variants — for background tasks
- `fetch_available_models(base_url, api_key)` — now takes explicit params (no longer reads from state)
- `fetch_local_models(base_url)` — Ollama model discovery
- `test_connection(cfg)` — minimal 1-word completion, returns `{ok, model_responded, response_preview, error}`
- `_DEFAULTS["transport"]` changed from `"ollama"` to `"openai_compatible"`

### `app/main.py`
- Removed `GET /available-models` and `POST /provider-config`
- Added `GET /settings`, `POST /settings`, `GET /models`, `GET /local-models`, `POST /test-connection`
- Kept `GET /provider-info` as a UI badge alias

### `app/static/index.html`
- New Settings tab: transport cards (Local/Cloud), local settings panel (Ollama URL, model dropdown), cloud settings panel (API endpoint, key write-only, model dropdown, timeout, per-job overrides)
- Settings panel shows `api_key_set` indicator (green dot / red dot)
- Test Connection button with success/error result
- Transport switch hot-switches immediately via `POST /settings`
- Model dropdowns populated via `GET /local-models` and `GET /models`
- Save button disabled until dirty; saves via `POST /settings`
- Privacy badge updated after save

### `data/config.example`
- Default transport changed to `"openai_compatible"`
- Default model changed to `"claude-opus-4-7"`
- Default base_url changed to `"https://api.yuxor.tech/v1"`
- Added `"ollama_base_url": "http://localhost:11434"`

### `data/config.json` (live config)
- Created with Yuxor cloud settings + `ollama_base_url`

---

## Phase 2 Changelog — Async Reflect

### `app/database.py`
- Added `reflection_status TEXT DEFAULT 'ready'` column via additive migration (Phase 3 comment position unchanged)
- Backfill: existing entries with non-null `kept_summary` set to `'ready'`
- `save_entry()` now accepts `reflection_status` param (default `"ready"`) and writes it to the INSERT/UPDATE
- Added `draft=''` to INSERT to satisfy NOT NULL constraint
- Added `update_reflection_status(entry_id, status, kept_summary=None, error_note=None)` — called by background task
- Added `get_entry_status(entry_id)` — returns `{status, kept_summary}` (treats NULL as `'ready'`)
- `get_all_entries()` and `get_entries_in_range()` now include `reflection_status` in SELECT

### `app/main.py`
- Imported `BackgroundTasks` from `fastapi`
- Added `get_entry_status`, `update_reflection_status` to database imports
- `POST /reflect` now: saves immediately with `reflection_status='pending'`, snapshots config, queues `BackgroundTask`
- Added `_run_reflection_task(entry_id, notes, mode, config_snapshot)` — the background task function
- Added `GET /reflect-status/{entry_id}` — returns `{status, kept_summary}`

### `app/static/index.html`
- `doReflect()`: polls `/reflect-status/{entry_id}` every 1.5s for up to 2 minutes
- Status messages: "Reflecting..." → "Entry saved." or "Entry saved (reflection still in progress)."
- Timeline rendering: shows ⏳ (pending), ⚠ (error), ✓ (ready with summary), — (no summary)
- Entry detail modal: shows "Reflecting... this will appear shortly." or error message
- Added `sleep(ms)` utility

---

## Future Improvements (Not Yet Implemented)

1. **Talk it through (Phase F)** — opt-in conversational back-and-forth mode (pending design approval).

2. **Tag analytics v2** — when tags become open-ended, correlation analytics can discover unexpected tag × signal patterns beyond the fixed "activities/triggers" set. Needs a query rewrite to consider all categories.

3. **Spoons tracking integration** — the ND tab spoons value could be shown in the Journal tab's signal row as a quick daily log.

4. **Reminder notifications** — browser notifications to prompt daily journaling at a set time.

5. **Entry editing** — allow editing the notes of a past entry (currently only working notes and kept_summary are editable).

6. **Offline PWA** — service worker so the app works without internet (even though cloud model requires internet).

---

## Quick Reference

```bash
# Start the app
.\venv\Scripts\python.exe run.py

# Ollama must be running (local mode only)
ollama serve
ollama pull qwen3.6:35b-a3b

# The config is at
mood-tracker/data/config.json

# Backup (manual)
copy mood-tracker/data/mood.db mood-tracker/data/mood.db.backup

# Check what's in the DB
sqlite3 mood-tracker/data/mood.db ".schema"
sqlite3 mood-tracker/data/mood.db "SELECT COUNT(*) FROM entries;"
sqlite3 mood-tracker/data/mood.db "SELECT entry_date, energy, reflection_status FROM entries ORDER BY entry_date DESC LIMIT 5;"

# Check active provider
curl http://localhost:8000/provider-info

# Check settings (key is never returned)
curl http://localhost:8000/settings

# List available cloud models
curl -H "Authorization: Bearer sk-..." https://api.yuxor.tech/v1/models

# List available Ollama models
curl http://localhost:11434/api/tags

# Test connection
curl -X POST http://localhost:8000/test-connection

# Hot-switch to cloud (example)
curl -X POST http://localhost:8000/settings \
  -H "Content-Type: application/json" \
  -d '{"transport":"openai_compatible","base_url":"https://api.yuxor.tech/v1","api_key":"sk-..."}'

# Hot-switch to local (example)
curl -X POST http://localhost:8000/settings \
  -H "Content-Type: application/json" \
  -d '{"transport":"ollama","base_url":"http://localhost:11434","model":"qwen3.6:35b-a3b"}'
```

---

## File Summary

| File | Lines | Purpose |
|------|-------|---------|
| `run.py` | ~170 | Uvicorn runner with argparse (`--network`, `--port`, `--bind`), TLS, graceful shutdown |
| `app/main.py` | ~970 | FastAPI app, all HTTP endpoints, async reflect, autostruct, ask-journal, correlations, clinician V2, auth, network mode middleware |
| `app/database.py` | ~830 | SQLite schema, migrations, all query functions, embeddings + Q&A retrieval + correlations + checkins + open-ended tags |
| `app/llm.py` | ~1060 | Pluggable model provider, hot-switch, autostruct (open-ended categories), embeddings, Q&A, correlation narration, clinician V2 |
| `app/auth.py` | ~310 | Network mode: access password (scrypt), session tokens (HMAC-SHA256), device management (approve/deny/revoke) |
| `app/tls.py` | ~80 | Self-signed cert generation (RSA 2048, SAN includes all local IPs + Tailscale) |
| `app/ratelimit.py` | ~50 | In-memory sliding window rate limiting for /auth/login |
| `app/static/index.html` | ~5000 | Complete frontend SPA (CSS + HTML + JS), auth overlay, AI tag display, charts, Ask panel, clinician print view |
| `requirements.txt` | 3 | fastapi, uvicorn[standard], httpx |
| `data/config.json` | — | Secrets (gitignored): transport, key, model, overrides, ollama_base_url |
| `data/config.example` | — | Config template (committed): default = openai_compatible |

Third-party dependencies not in requirements.txt (installed separately or vendored):
- `faster-whisper` — STT
- `av` — ffmpeg Python bindings (webm decode)
- `kokoro-onnx` — TTS
- `onnxruntime` — ONNX runtime
- `numpy` — array operations
- `soundfile` — WAV write
- `pydantic` — (bundled with fastapi)

---

## Phase A Changelog — Auto-structuring (Tag & Signal Suggestions)

### `app/llm.py`
- Added `AUTOSTRUCT_SYSTEM_PROMPT` — instructs model to output JSON with signals and categorised tags only
- Added `suggest_autostruct(entry_text, existing_tags)` — uses `complete()` with `job="autostruct"`, passes existing tag list as reuse hints
- Added `suggest_autostruct_from_snapshot(snapshot, ...)` — pins to config snapshot for background tasks
- Added `_empty_autostruct()` — fallback on parse error
- `complete()` and `complete_from_snapshot()` now route `job="autostruct"` through the model override resolution

### `app/main.py`
- Added `_autostruct_tasks` in-memory dict keyed by UUID
- Added `POST /autostruct` — saves task immediately, queues `_run_autostruct_task` as `BackgroundTask`, returns task ID
- Added `_run_autostruct_task(task_id, entry_text, config_snapshot, existing_tags)` — background task calling `suggest_autostruct_from_snapshot`
- Added `GET /autostruct-status/{task_id}` — returns `{status, result|error}`

### `app/static/index.html`
- Added "Suggest tags & signals" button inside the signals row in Journal tab
- Added auto-struct section below tags (hidden until suggestions are loaded)
- Added `runAutoStruct()`, `renderAutoStructSuggestions()`, `acceptAutoSignal()`, `acceptAutoTag()`, `dismissAutoStruct()`, `renderAutoStructError()`, `clearAutoStruct()` JavaScript functions
- Signals: model suggestions shown as highlighted buttons; click to accept into `currentSignals`
- Tags: suggested chips shown per category; click to add to `currentTags` (duplicates prevented)
- Polls `/autostruct-status/{task_id}` every 1.2s until ready
- `clearJournalEntry()` now calls `clearAutoStruct()`
- Settings: added "Auto-struct" model override field in cloud settings panel

---

## Phase B Changelog — Trend Charts

### `app/main.py`
- Added `GET /signal-series` — returns numeric signal series (low→1, med→2, high→3, null→no data) and weekly check-ins; deterministic, no model call

### `app/static/index.html`
- Added signal chart section above the LLM analysis in Trends tab (loads on tab switch)
- Added `loadCharts()`, `renderSignalChart()`, `renderWeeklyChart()` — hand-rolled SVG rendering (no library)
- Signal chart: multi-line chart with colored lines for energy/sleep/sensory/overwhelm, dot markers, axis labels
- Weekly chart: bar chart for spoons (0-12), M/shutdown markers above bars
- Time-window selector (30/60/90/180 days) with re-render on change
- Empty state shown when no signal data exists

---

## Phase C Changelog — "Ask Your Journal" (NL Q&A)

### `app/database.py`
- Added `entry_embeddings` table (`entry_id`, `embedding BLOB`, `model`, `updated_at`) — additive, optional
- Added `save_entry_embedding()`, `get_entry_embedding()`, `get_all_embeddings()` — CRUD for embeddings sidecar
- Added `search_entries_for_qa()` — keyword + recency ranking; pre-fetches tags per entry; scores by keyword matches, date hints, and tag matches; returns up to 20 candidates

### `app/llm.py`
- Added `check_embeddings_support()` — probes `POST /embeddings`; returns `False` on 404
- Added `compute_embedding()` — calls `POST /embeddings`, handles OpenAI/Yuxor response shape
- Added `cosine_similarity()`, `rank_by_embedding()` — semantic re-ranking if embeddings are available
- Added `build_qa_context()` — assembles context preferring `kept_summary` over notes; token-capped at ~10k tokens; returns `(context_string, cited_dates)`
- Added `answer_journal_question()` and `answer_journal_question_from_snapshot()` — `job="ask_journal"`, `max_tokens=768`, returns `{answer, cited_dates, method, token_count}`
- Added `QA_SYSTEM_PROMPT`

### `app/main.py`
- Added `_ask_tasks` in-memory dict keyed by UUID
- Added `POST /ask` — queues `_run_ask_task` as `BackgroundTask`, returns task ID
- Added `_run_ask_task()` — retrieves entries (last 200 days), runs keyword search, optionally probes and uses embeddings, calls `answer_journal_question_from_snapshot()`
- Added `GET /ask-status/{task_id}` — returns `{status, result|error}`
- Added `GET /embeddings-info` — probes `/embeddings` and returns availability
- Added `search_entries_for_qa` to database imports

### `app/static/index.html`
- Added "Ask your journal" panel at the bottom of the Search tab
- Added `doAskJournal()` and `renderAskResult()` — polls `/ask-status/{task_id}` until ready
- Citations rendered as clickable date links that open the entry modal
- Retrieval method + token estimate shown below answer

### Embeddings availability test
- Tested against Yuxor: `embeddings_available: false` — provider does not expose `/embeddings`
- Phase C uses **keyword + recency ranking** as the active retrieval path
- The embeddings infrastructure is in place; it will activate automatically if a provider with `/embeddings` support is configured

---

## Phase D Changelog — Correlation Analytics

### `app/database.py`
- Added `compute_correlations(days=180)` — runs three SQL queries:
  - Tag × signal co-occurrence: for each activity/trigger tag, counts low/med/high per signal vs baseline
  - Baseline signal distribution: aggregate counts across all entries in the window
  - Lead/lag: consecutive-day pairs for poor-sleep→overwhelm and high-energy→overwhelm
- All computation is deterministic SQLite — no model call

### `app/llm.py`
- Added `CORRELATION_NARRATION_PROMPT` — instructs model to narrate computed counts as questions/observations, not verdicts; cites actual numbers; flags thin data
- Added `narrate_correlations()` and `narrate_correlations_from_snapshot()` — feeds `CORRELATION_NARRATION_PROMPT` with the full stats block, `job="correlations"`, `max_tokens=512`

### `app/main.py`
- Added `_correlation_tasks` in-memory dict
- Added `GET /correlations` — returns `compute_correlations()` directly (no model)
- Added `POST /narrate-correlations` — queues background task, returns task ID
- Added `_run_narrate_correlations()` — computes stats then calls `narrate_correlations_from_snapshot()`
- Added `GET /narrate-correlations-status/{task_id}`
- `complete()` routes `job="correlations"` through `model_overrides.correlations`

### `app/static/index.html`
- Added "Correlations" button to Trends tab controls
- Added `correlation-section` div (hidden until button clicked)
- Added `doCorrelations()` — POSTs to `/narrate-correlations`, polls, renders
- Added `renderCorrelations()` — renders tag×signal table (colored counts), lead/lag cards, LLM narration block
- Settings: added "Correlations" model override field

---

## Phase S2 Changelog — Encryption at Rest

### `app/crypto.py` (new)
- KDF: scrypt (N=2^14, r=8, p=1, dklen=32) — strongest params available on this Windows build
- `derive_db_key()` → 32-byte raw key for SQLCipher PRAGMA key
- `derive_aes_key()` → 16-byte AES key + 16-byte HMAC key for config encryption
- AES-128-GCM encryption for config values (API key), with random nonce per encryption
- `hash_recovery_key()` → SHA-256 hash for verifying recovery key without storing plaintext
- `verify_recovery_key()` → constant-time comparison against stored hash
- `passphrase_strength_hint()` → plain-English strength feedback for the UI

### `app/vault.py` (new)
- `Vault` class: singleton managing in-memory key state (db_key, config_key_aes, config_key_hmac, salt)
- `unlock(passphrase)` → derives keys, opens SQLCipher DB, sets connection factory
- `lock()` → closes DB connection, clears all keys from memory
- `setup_vault()` → generates salt, stores vault.json, marks db_encrypted=True
- `get_vault_state()` → returns VaultState (is_setup, is_unlocked, has_recovery_key, db_encrypted)
- `change_passphrase()` → re-encrypts DB and vault.json with new passphrase-derived key
- `_open_sqlcipher_db()` → opens encrypted DB with hex key, PRAGMA cipher_compatibility=4
- Recovery key stored as SHA-256 hash (not plaintext) — backward-compatible with legacy plaintext vaults

### `app/main.py`
- Added `GET /vault-status` — returns VaultState dict for frontend
- Added `POST /vault-setup` — first-time setup: creates vault, encrypts DB, generates recovery key, verifies encrypted DB, moves plaintext aside
- Added `POST /vault-unlock` — unlocks vault, decrypts API key into active LLM config
- Added `POST /vault-lock` — locks vault, clears keys
- Added `POST /vault-recovery-unlock` — unlock with recovery key, set new passphrase (DB key unchanged)
- Added vault-lock middleware: blocks all API calls except vault endpoints when vault is set up but locked
- `/vault-status` and static files bypass middleware; root `/` also bypasses (serves lock screen)

### `app/database.py`
- Added `VaultLockedError` exception raised when DB access is attempted while vault is locked
- `install_vault_connection_factory()` — allows vault to inject SQLCipher connection into all DB operations
- `get_connection()` — routes through vault factory when set; raises VaultLockedError if vault is locked
- `init_db()` — detects encrypted DB (sqlite3 can't open it) and returns early; vault handles schema
- `export_all()` — now includes top-level `"tags"` key with all tags (fixes orphaned-tag export bug)
- `import_data()` — now imports orphaned tags from the top-level `"tags"` key

### `app/static/index.html`
- Replaced cosmetic PIN lock screen with vault unlock/setup UI:
  - **Unlock screen**: passphrase input, show/hide toggle, "Forgot passphrase?" link
  - **Setup screen**: passphrase + confirm, strength hint, warning about data loss, one-time recovery key display
  - **Recovery screen**: recovery key input + new passphrase + confirm
- `initVaultUI()` replaces `initPinLock()` — fetches `/vault-status` and shows the correct screen
- All PIN-related CSS/JS removed (`.pin-*`, `PIN_KEY`, `checkPin`, `buildPinKeypad`, etc.)
- New CSS classes: `.vault-*` for the lock/setup screens
- Recovery key shown once at setup with copy-to-clipboard; user must confirm understanding of data-loss risk

### `data/vault.json` (new, gitignored)
- Stores: version, salt (base64), encrypted_api_key (AES-GCM), recovery_key_hash (SHA-256), db_encrypted flag, db_path_b64, scrypt params
- Never contains plaintext passphrase, key, or recovery key
- Legacy vaults storing plaintext recovery key are supported (backward-compatible verification)

### `data/mood.db.plaintext.backup`
- Created automatically by `vault-setup` before encrypting the DB
- Kept indefinitely as a safety net; never auto-deleted

---

## Phase F Changelog — Network Mode (LAN & Tailscale Access)

### `app/auth.py` (new)
- Access password: scrypt-hashed (same params as vault), stored in `data/auth.json`
- Session tokens: HMAC-SHA256 signed, 24-hour TTL, transparent renewal via `X-Renewed-Session` header
- Device management: records in `data/devices.json` with id, name, IP, user_agent, status (pending/approved/denied/revoked)
- `is_lan_ip()` — accepts RFC 1918 (10.x, 172.16-31.x, 192.168.x), loopback (127.x), and Tailscale (100.64.0.0/10)
- Rate limiting: 5 req/min/IP for `/auth/login`

### `app/tls.py` (new)
- Self-signed cert generation: RSA 2048, CN=mood-tracker.local, SAN includes all detected local IPs + localhost + Tailscale IPs
- 825-day validity (Chrome max for self-signed)
- Certs stored in `data/tls/cert.pem` and `data/tls/key.pem`

### `app/ratelimit.py` (new)
- In-memory sliding window rate limiter
- `/auth/login`: 5 req/60s per IP
- All other endpoints: 60 req/60s per IP
- Periodic purge of stale entries

### `app/main.py`
- Network mode activated by env var `MT_NETWORK_MODE=1` (set by `run.py --network`)
- LAN/Tailscale IP filter middleware: rejects non-RFC1918/non-Tailscale IPs in network mode
- Auth gate middleware: requires session token for all non-public paths in network mode
- CSRF check: requires `X-Requested-With: mood-tracker-app` on POST/PUT/DELETE
- CORS: regex allowing `https://` + RFC 1918 origins + Tailscale IPs + `*.ts.net` in network mode
- CSP: adds `upgrade-insecure-requests` and `block-all-mixed-content` in network mode; HSTS header
- New endpoints: `/auth/status`, `/auth/login`, `/auth/approve`, `/auth/deny`, `/auth/revoke`, `/auth/devices`, `/auth/logout`, `/auth/change-password`, `/auth/enable`, `/auth/disable`
- `authFetch()` wrapper in frontend: adds `Authorization: Bearer <token>` and `X-Requested-With` to every fetch call

### `app/static/index.html`
- Auth overlay: login form (password + device name), pending-approval screen, denied screen
- Network Access section in Settings: enable/disable, device list with approve/deny/revoke, change password
- Mobile-responsive CSS: media queries for ≤640px and ≤380px
- `initAuthUI()` runs before `initVaultUI()`

### `run.py`
- Argparse: `--network` flag, `--port`, `--bind`
- On `--network`: generates TLS cert, binds to `0.0.0.0`, prints access URLs
- Injects `MT_NETWORK_MODE=1` env var

### `.gitignore`
- Added `data/auth.json`, `data/devices.json`, `data/tls/`

---

## Phase G Changelog — AI-Generated Tags (Open-Ended Categories)

### What changed

Tags used to be limited to four fixed categories (people, places, activities, triggers) with manual input. Now the AI generates tags automatically when you save an entry, using whatever categories fit your writing (mood, work, health, self_care, finances, relationships, etc.). New categories are created on the fly. The manual tag input boxes are removed; tags appear automatically after saving.

### `app/llm.py`
- Rewrote `AUTOSTRUCT_SYSTEM_PROMPT` — tags are now open-ended, any category the model thinks fits
- Rewrote `suggest_autostruct()` and `suggest_autostruct_from_snapshot()` — hint block lists all existing tags grouped by their actual categories (not just the legacy four)
- Rewrote `_parse_autostruct()` — validates signals strictly, but accepts any category name for tags; normalizes to lowercase, dedupes, rejects categories longer than 32 chars or containing spaces
- `_empty_autostruct()` returns `"tags": {}` instead of the old four empty categories
- `max_tokens` for autostruct increased from 384 to 512

### `app/database.py`
- Added `get_all_categories()` — returns distinct tag categories in use
- `tags.category` column: no longer restricted to four values; accepts any short lowercase string
- `get_or_create_tag()` already handled arbitrary categories (case-insensitive match on name+category)

### `app/main.py`
- `POST /reflect` now also queues an autostruct background task; returns `autostruct_task_id` in response
- Added `_run_autostruct_and_apply_task()` — runs autostruct, creates missing tags via `get_or_create_tag()`, saves them to the entry via `save_entry_tags()`, fills in any missing signals via `update_entry_signals()`
- Added `POST /autostruct-rerun/{entry_id}` — re-run autostruct for an existing entry
- Added `GET /tags/categories` — returns distinct categories in use
- `GET /autostruct-status/{task_id}` response now includes `entry_id` and `applied` fields
- `ReflectResponse` model now includes `autostruct_task_id: str | None = None`

### `app/static/index.html`
- **Removed:** four manual tag input areas (people/places/activities/triggers), "Suggest tags & signals" button, autostruct suggestion panel, `TAG_CATEGORIES` constant, `addTagFromInput()`, `handleTagInput()`, `handleTagKeydown()`, `closeAllSuggestions()`, old `currentTags` dict structure
- **Removed:** CSS for `.tags-category-label`, `.autostruct-tag-row`, `.autostruct-tag-label`, `.autostruct-suggested-chip`, `.autostruct-apply-hint`, `.autostruct-dismiss`, `.autostruct-error`
- **Added:** `renderTags()` — renders tags grouped by category with removable × chips
- **Added:** `removeTagFromEntry()` — removes a tag and saves updated list to server
- **Added:** `runAutoStructForEntry(entryId)` — re-runs autostruct for a timeline entry
- **Added:** `pollAutoStruct(taskId)` — polls autostruct result, auto-fills tags and signals, renders
- **Added:** `setSignalByName()` — programmatically set a signal button (used when AI suggests signals)
- `currentTags` changed from `{people: [], ...}` to flat `[{name, category}]` array
- `doReflect()` now calls `pollAutoStruct()` after saving, shows "Generating tags…" spinner
- "Re-suggest tags" button (hidden until entry is saved) calls `runAutoStructForEntry()`
- `saveCurrentTags()` deprecated (tags are now auto-applied by the server)
- `clearJournalEntry()` resets `currentTags = []` and calls `renderTags()`
