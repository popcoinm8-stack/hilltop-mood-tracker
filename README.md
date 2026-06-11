# Hilltop Mood Tracker

> A private, local-first daily journal designed for neurodivergent adults.

Hilltop is a single-user journaling app that lives on your machine. It combines a fast, low-friction writing surface with on-device encryption, AI-assisted reflections, and tools that help you notice patterns in your own life — without sending your words anywhere you don't choose.

Your data stays on your disk, encrypted at rest, and only reaches an AI provider if you explicitly configure one.

---

## Highlights

- **🔐 Encryption at rest** — SQLite database is encrypted with SQLCipher. Your journal is protected by a passphrase plus a recovery key, so a lost passphrase doesn't mean a lost journal.
- **🤖 AI reflections, on your terms** — Drafts quick or detailed reflections of your day. Uses local Ollama by default, or any OpenAI-compatible endpoint with your own key. Mid-flight config switches are safe.
- **🧠 Ask Your Journal** — Ask natural-language questions across your history ("When do I usually crash on Sundays?") and get answers grounded in your actual entries, with citations.
- **📊 Tag × Signal correlations** — Computes real statistics (per-tag signal distributions, lead/lag between sleep and overwhelm) and narrates them in plain language. No vibe-based pattern matching.
- **🩺 Clinician summary (V2)** — Generates a structured handout (Overall Picture, Notable Changes, Meltdowns/Shutdowns, Things to Bring, Timeline, Suggested Questions) you can edit before sharing.
- **🎙️ Voice in, voice out** — `faster-whisper` speech-to-text for hands-free journaling. `Kokoro` TTS to read reflections back to you.
- **🏷️ AI-generated tags** — The AI automatically creates tags when you save an entry, using any category that fits (moods, work, health, people, etc.). Tags grow organically from what you write about — no fixed taxonomy. You can remove tags you disagree with or re-run the AI for fresh suggestions.
- **🛡️ Network mode (optional)** — Host on your PC and access from your phone via home Wi-Fi or Tailscale VPN. HTTPS, access password, device whitelisting, and LAN IP filtering. See [Docs/NETWORK_MODE_HOWTO.md](Docs/NETWORK_MODE_HOWTO.md) for setup instructions.
- **🗓️ Weekly check-ins** — Track spoons, meltdowns, and shutdowns at a weekly cadence. These flow into trends and clinician summaries.
- **💾 Backup & restore** — Encrypted export to a single JSON file. Import with conflict-resolution rules. No vendor lock-in.
- **🛡️ Hardened by default** — Local-only binding (`127.0.0.1`), strict CORS allowlist, CSP and security headers, salted recovery keys, scrypt KDF. See [Security](#security) below.

---

## Quick start

### 1. Requirements

- **Python 3.10+**
- **Windows, macOS, or Linux** (tested on Windows 11; uses `SIGBREAK` for graceful shutdown)
- A modern browser (the app is a local SPA)
- **Optional:** [Ollama](https://ollama.com) for fully local AI, or an OpenAI-compatible API key

### 2. Install

```bash
git clone https://github.com/popcoinm8-stack/hilltop-mood-tracker.git
cd hilltop-mood-tracker

python -m venv venv
# Windows:
venv\Scripts\activate
# macOS / Linux:
source venv/bin/activate

pip install -r requirements.txt
```

### 3. Run

```bash
.\venv\Scripts\python.exe run.py
```

Then open <http://localhost:8000> in your browser.

The first time you visit, you'll be asked to create a **vault passphrase** and a **recovery key**. Don't skip the recovery key — it is the only way to regain access if you forget the passphrase.

### 4. Configure an AI provider

The app works without any AI if you just want to journal manually. To enable reflections, pick one of:

- **Local Ollama** — Install [Ollama](https://ollama.com), pull a model (`ollama pull llama3.1`), then point the app to it in Settings. Nothing leaves your machine.
- **OpenAI-compatible endpoint** — Set a `base_url` and `api_key` in Settings. The default points at the Yuxor unified gateway, but any OpenAI-format endpoint works (OpenAI, Azure OpenAI, LocalAI, vLLM, etc.).

You can hot-switch between providers in Settings without restarting. Background tasks snapshot the active config at task creation, so in-flight jobs are unaffected.

---

## What the daily flow looks like

1. **Open the day** — A blank entry for today appears. No friction, no logins.
2. **Write or dictate** — Type, or use the microphone to dictate via `faster-whisper`.
3. **Generate a reflection** — The AI drafts a 3–4 sentence observation (quick mode) or a structured 4-section reflection (detailed mode). It's a draft for you to check, not a verdict.
4. **Edit, keep, or discard** — The summary you keep is stored separately from your raw notes.
5. **Tags appear automatically** — The AI generates tags (with open-ended categories) and fills in any signal levels you left blank. Remove tags you don't agree with, or click "Re-suggest tags" for a fresh set.
6. **Come back later** — Browse timeline, ask questions, see your trends, generate a clinician summary.

---

## Project layout

```
mood-tracker/
├── app/
│   ├── main.py            # FastAPI endpoints, middleware, app factory
│   ├── llm.py             # Pluggable LLM provider (Ollama / OpenAI-compatible)
│   ├── database.py        # SQLite schema, migrations, queries
│   ├── vault.py           # SQLCipher encryption manager
│   ├── crypto.py          # scrypt KDF + AES-128-GCM
│   ├── auth.py            # Network-mode access password + device whitelisting
│   ├── tls.py             # Self-signed cert generation for HTTPS
│   ├── ratelimit.py       # In-memory rate limiting
│   ├── transcribe.py      # faster-whisper STT singleton
│   ├── tts.py             # Kokoro TTS singleton
│   └── static/index.html  # Complete frontend SPA (vanilla JS)
├── data/                  # Runtime data (gitignored)
│   ├── config.example     # Template for config.json
│   ├── config.json        # Your local config (gitignored, holds API key)
│   ├── mood.db            # Your encrypted journal (gitignored)
│   ├── vault.json         # Your vault metadata (gitignored)
│   ├── auth.json          # Network access password (gitignored, network mode)
│   ├── devices.json       # Approved devices (gitignored, network mode)
│   └── tls/               # Self-signed cert (gitignored, network mode)
├── Docs/
│   └── NETWORK_MODE_HOWTO.md  # Setup guide for LAN/Tailscale access
├── run.py                 # uvicorn entry point with graceful shutdown
├── requirements.txt
├── HANDOVER.md            # Comprehensive developer doc (architecture, endpoints, schema)
├── LICENSE                # MIT
└── README.md              # ← you are here
```

For deeper architecture details, the [HANDOVER.md](HANDOVER.md) is a 900+ line developer reference covering every endpoint, the database schema, the LLM provider interface, and the encryption design.

---

## API surface

A few representative endpoints (see `HANDOVER.md` for the full list):

| Endpoint                  | Purpose                                                      |
| ------------------------- | ------------------------------------------------------------ |
| `POST /reflect`           | Generate an AI reflection for an entry (background task); also auto-generates tags |
| `POST /save-summary`      | Persist the kept summary for an entry                        |
| `POST /transcribe`        | Speech-to-text via faster-whisper                            |
| `POST /speak`             | TTS via Kokoro                                               |
| `POST /autostruct`        | Suggest tags and signals from entry text (async)             |
| `POST /autostruct-rerun/{id}` | Re-run autostruct for an existing entry (async)          |
| `GET /autostruct-status/{id}` | Poll autostruct task result                                |
| `GET /tags`               | List all tags                                                |
| `GET /tags/categories`    | List all tag categories in use                                |
| `POST /entry-tags`        | Save tags for an entry (creates missing tags)                |
| `POST /analyze-trends`    | Identify trends, patterns, and insights across kept entries  |
| `POST /ask-journal`       | Answer a natural-language question over the journal          |
| `POST /correlations`      | Computed tag × signal statistics with plain-language narrative |
| `POST /clinician-summary` | Generate a structured clinician handout (V2 format)          |
| `POST /weekly-checkin`    | Record a weekly check-in (spoons, meltdowns, shutdowns)      |
| `GET /export` / `POST /import` | Backup and restore                                          |
| `GET /settings` / `POST /settings` | Read and update provider config (hot-swap safe)        |

---

## Security

- **Local-only network binding** — `run.py` binds to `127.0.0.1` by default. Use `--network` for LAN/Tailscale access with HTTPS, auth, and device whitelisting.
- **CORS allowlist** — Only loopback origins accepted in local mode; LAN + Tailscale origins in network mode.
- **Security headers** — `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, and a strict `Content-Security-Policy` are set on every response.
- **Encryption at rest** — The SQLite database is encrypted with SQLCipher using a key derived from your passphrase via scrypt (`N=2^14, r=8, p=1, dklen=32`).
- **API key handling** — Your AI provider key is stored in `data/config.json`, which is gitignored. It never crosses to the browser, never appears in logs, and is replaced with a sentinel in any UI response.
- **Recovery key** — A separate 24-character token, salted with the vault salt, hashed with scrypt, and stored in `vault.json`. Allows vault recovery if you forget the passphrase. Never stored in plaintext.
- **No telemetry** — The app makes no outbound calls except the ones you configure in Settings.

If you want to expose this app beyond your own machine, use the built-in network mode (see below). Don't widen the CORS allowlist manually.

---

## Network mode (LAN and Tailscale access)

By default the app runs on `http://localhost:8000` only. To access it from your phone or another device:

```
.\venv\Scripts\python.exe run.py --network
```

This starts the server in network mode with:

- **HTTPS** via a self-signed certificate (auto-generated, regenerate by deleting `data/tls/`)
- **Access password** separate from your vault passphrase
- **Device whitelisting** — phone devices must be approved from the desktop
- **LAN IP filter** — only RFC 1918 (10.x, 172.16-31.x, 192.168.x), Tailscale (100.64-127.x), and loopback connections accepted
- **Rate limiting** — 5 login attempts per minute per IP

For remote access from outside your home network, install [Tailscale](https://tailscale.com) on both devices. The server is automatically reachable at your Tailscale IP (e.g. `https://100.x.x.x:8000`) without any router changes.

See [Docs/NETWORK_MODE_HOWTO.md](Docs/NETWORK_MODE_HOWTO.md) for the complete guide.

---

## Privacy model

- **By default, no network calls.** The app does not phone home.
- **Ollama transport:** All inference happens on your machine. The only network call is to `localhost:11434`.
- **Cloud transport:** Requests are sent to the `base_url` you configure. The app sends the model name, your prompt, and the bearer token. It does not send telemetry, usage stats, or anything else.
- **API keys** are read from `data/config.json` server-side and never returned to the frontend.
- **Your data** (`mood.db`, `vault.json`) stays in the project directory. Back up this directory if you want backups beyond the in-app export.

---

## Development

```bash
# Install in editable mode (the app uses a src-style layout)
pip install -r requirements.txt

# Run with the included launcher
.\venv\Scripts\python.exe run.py

# Or run uvicorn directly if you want hot-reload
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

There is no separate frontend build step — `app/static/index.html` is a complete vanilla-JS SPA served as static files.

---

## License

[MIT](LICENSE) — Copyright (c) 2026 Elliot Mason.

---

## A note on intent

This tool is a reflection aid, not a therapist. AI-generated summaries are drafts for you to check, not verdicts. The trend and correlation features state counts and raise questions — they do not diagnose, pathologize, or tell you what to do. If something here isn't helpful, change it. The app is yours.
