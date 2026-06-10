"""FastAPI application for mood tracker."""
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
import uuid

import numpy as np

import httpx
from fastapi import FastAPI, HTTPException, BackgroundTasks
from starlette.requests import Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.database import (
    export_all,
    get_180day_summaries,
    get_all_entries,
    get_all_tags,
    get_entries_in_range,
    get_entry_status,
    get_entry_tags,
    get_recent_summaries,
    get_or_create_tag,
    get_or_create_weekly_checkin,
    get_weekly_checkins,
    get_weekly_checkins_in_range,
    init_db,
    save_entry,
    save_entry_tags,
    search_tags,
    search_entries_for_qa,
    compute_correlations,
    update_entry_signals,
    update_kept_summary,
    update_reflection_status,
    update_weekly_checkin,
    update_working_notes,
    import_data,
)
from app import llm
from app.transcribe import transcribe
from app.tts import speak
from app import vault as _vault
from app.database import install_vault_connection_factory


# ---------------------------------------------------------------------------
# Auto-struct task store (in-memory; keyed by UUID)
# ---------------------------------------------------------------------------
_autostruct_tasks: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# Ask-journal task store (in-memory; keyed by UUID)
# ---------------------------------------------------------------------------
_ask_tasks: dict[str, dict] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield  # startup done by init_db() below
    # --- Graceful shutdown: release all heavy resources ---
    # 1. Unload Whisper model (frees GPU/CPU memory immediately)
    try:
        from app.transcribe import shutdown as _shutdown_whisper
        _shutdown_whisper()
    except Exception:
        pass
    # 2. For Ollama transport: tell it to drop the LLM so VRAM is freed.
    #    No-op for openai_compatible (nothing to unload on our side).
    try:
        cfg = llm.get_config()
        if cfg.get("transport") == "ollama":
            base_url = cfg.get("base_url", "http://localhost:11434")
            model = cfg.get("model", "")
            url = base_url.rstrip("/") + "/api/generate"
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(url, json={"model": model, "keep_alive": 0})
    except Exception:
        pass  # best-effort; server may already be stopping


app = FastAPI(lifespan=lifespan)

# Mount static files (index.html lives in app/static/)
BASE_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# ---------------------------------------------------------------------------
# Vault-lock middleware: return 401 if vault is set up but locked
# ---------------------------------------------------------------------------
@app.middleware("http")
async def vault_lock_middleware(request, call_next):
    from app import vault as _v
    # Allow vault endpoints and static files without lock
    if request.url.path.startswith("/vault") or request.url.path.startswith("/static"):
        return await call_next(request)
    try:
        return await call_next(request)
    except Exception as exc:
        # VaultLockedError raised when vault is locked and DB access is attempted
        if exc.__class__.__name__ == "VaultLockedError":
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=401, content={"detail": "Vault is locked. Please unlock it first."})
        # Re-raise other exceptions
        raise

# ---------------------------------------------------------------------------
# DB init on startup
# init_db() probes the DB file: if it's an encrypted SQLCipher file,
# sqlite3 can't open it and init_db returns early. If it's plaintext,
# all additive migrations run as normal.
# ---------------------------------------------------------------------------
init_db()


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------
class ReflectRequest(BaseModel):
    notes: str
    transcription: str | None = None  # raw dictation, stored for detail view
    mode: str = "quick"  # "quick" or "detailed"
    energy: str | None = None
    sleep_quality: str | None = None
    sensory_load: str | None = None
    overwhelm: str | None = None


class ReflectResponse(BaseModel):
    entry_id: int
    mode: str


class SpeakRequest(BaseModel):
    text: str


class SaveSummaryRequest(BaseModel):
    entry_id: int
    kept_summary: str


class SettingsUpdateRequest(BaseModel):
    transport: str | None = None
    base_url: str | None = None
    api_key: str | None = None   # write-only — never returned
    model: str | None = None
    request_timeout: int | None = None
    model_overrides: dict | None = None
    ollama_base_url: str | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/")
async def serve_index() -> FileResponse:
    return FileResponse(str(BASE_DIR / "static" / "index.html"))


@app.post("/reflect", response_model=ReflectResponse)
async def reflect(req: ReflectRequest, background_tasks: BackgroundTasks) -> ReflectResponse:
    """Save today's entry immediately, then generate reflection asynchronously.

    The entry is persisted with status='pending' before the model is called.
    If the model is slow or unreachable the entry is never lost — it appears
    as 'pending' and the result lands when the model finishes.
    """
    if not req.notes.strip():
        raise HTTPException(status_code=400, detail="Notes cannot be empty.")

    mode = req.mode if req.mode in ("quick", "detailed") else "quick"
    today = date.today()

    # Append transcription to notes if provided
    notes = req.notes
    if req.transcription and req.transcription.strip():
        notes = notes + "\n\n[Dictation]: " + req.transcription

    # Save immediately with status='pending' (no kept_summary yet)
    entry_id = save_entry(
        entry_date=today,
        notes=notes,
        transcription=req.transcription,
        kept_summary=None,  # filled in by background task
        mode=mode,
        energy=req.energy,
        sleep_quality=req.sleep_quality,
        sensory_load=req.sensory_load,
        overwhelm=req.overwhelm,
        reflection_status="pending",
    )

    # Snapshot the active config so a mid-flight provider switch doesn't affect this job
    config_snapshot = llm.snapshot_config()

    # Run model call in background — own DB connection via WAL
    background_tasks.add_task(
        _run_reflection_task, entry_id, notes, mode, config_snapshot
    )

    return ReflectResponse(entry_id=entry_id, mode=mode)


def _run_reflection_task(entry_id: int, notes: str, mode: str, config_snapshot: dict) -> None:
    """Background task: generate reflection and update the entry."""
    try:
        recent = get_recent_summaries(days=7)
        llm_reflection = llm.generate_draft_from_snapshot(config_snapshot, notes, recent, mode=mode)
        update_reflection_status(entry_id, "ready", kept_summary=llm_reflection)
    except llm.LLMError as exc:
        update_reflection_status(entry_id, "error", error_note=str(exc))
    except Exception as exc:
        update_reflection_status(entry_id, "error", error_note="Unexpected error during reflection.")


@app.get("/reflect-status/{entry_id}")
async def get_reflect_status(entry_id: int) -> dict:
    """Return the current reflection status for an entry."""
    status = get_entry_status(entry_id)
    return {
        "status": status["status"],
        "kept_summary": status["kept_summary"] if status["status"] == "ready" else None,
    }


@app.post("/save-summary")
async def save_summary(req: SaveSummaryRequest) -> dict:
    """Save the user-edited draft as the kept summary for this entry."""
    update_kept_summary(req.entry_id, req.kept_summary)
    return {"ok": True}


@app.post("/transcribe")
async def transcribe_audio(request: Request) -> dict:
    """
    Receive webm/opus blob, transcribe in-memory (never written to disk), return text.
    On failure, returns {error, retry_id} so the client can retry without losing audio.
    """
    import asyncio
    audio_bytes = await request.body()
    try:
        text = await asyncio.to_thread(transcribe, audio_bytes)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Transcription failed: {exc}")
    return {"text": text}


@app.get("/export")
async def export() -> JSONResponse:
    """Export all entries, tags, weekly check-ins, and embeddings as JSON for backup."""
    data = export_all()
    data["exported_at"] = str(date.today())
    return JSONResponse(
        content=data,
        headers={
            "Content-Disposition": "attachment; filename=mood-export.json"
        },
    )


@app.post("/import")
async def import_backup(request: Request, on_conflict: str = "skip") -> dict:
    """Restore data from a JSON backup (as produced by GET /export).

    By default ("skip") existing entries and check-ins are left unchanged.
    With on_conflict="overwrite", existing rows are replaced.
    Tags are always merged (existing tags reused, new ones created).
    Never deletes data in "skip" mode.

    The request body must be the raw JSON export file content.
    The on_conflict policy is passed as a query parameter: ?on_conflict=skip
    """
    if on_conflict not in ("skip", "overwrite"):
        raise HTTPException(status_code=400, detail="on_conflict must be 'skip' or 'overwrite'.")

    try:
        body = await request.body()
        import json as _json
        data = _json.loads(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON payload: {exc}") from exc

    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Payload must be a JSON object with an 'entries' key.")
    if "entries" not in data:
        raise HTTPException(status_code=400, detail="Payload must contain an 'entries' key.")

    stats = import_data(data, on_conflict=on_conflict)
    return stats


@app.post("/speak")
async def speak_route(req: SpeakRequest) -> Response:
    """
    Generate spoken audio for the given text and return as WAV.
    Kokoro runs on CPU, loaded once and kept resident.
    Audio never written to disk — streamed from memory.
    """
    import asyncio, io
    try:
        samples, sr = await asyncio.to_thread(speak, req.text)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"TTS failed: {exc}")

    import soundfile as sf
    wav_buf = io.BytesIO()
    sf.write(wav_buf, np.array(samples), sr, format="WAV")
    wav_buf.seek(0)
    return Response(content=wav_buf.read(), media_type="audio/wav")


# ---------------------------------------------------------------------------
# Analytics endpoints
# ---------------------------------------------------------------------------
class AnalyzeTrendsRequest(BaseModel):
    days: int = 180  # 0 means all time / indefinite


@app.post("/analyze-trends")
async def analyze_trends_route(req: AnalyzeTrendsRequest) -> dict:
    """Analyze kept summaries for the given context window (days=0 means all time)."""
    summaries = get_180day_summaries(days=req.days)
    if req.days == 0:
        date_range = "all time"
    else:
        date_range = f"last {req.days} days"
    try:
        result = llm.analyze_trends(summaries, date_range=date_range)
    except llm.LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return result


# ---------------------------------------------------------------------------
# Clinician summary endpoints
# ---------------------------------------------------------------------------
class ClinicianSummaryRequest(BaseModel):
    start_date: str  # ISO date string YYYY-MM-DD
    end_date: str


class ClinicianExportRequest(BaseModel):
    draft: str
    start_date: str
    end_date: str
    generated_date: str


@app.post("/clinician-summary")
async def clinician_summary_route(req: ClinicianSummaryRequest) -> dict:
    """Compile entries in the given date range and draft a clinician-facing structured summary."""
    entries = get_entries_in_range(req.start_date, req.end_date)
    checkins = get_weekly_checkins_in_range(req.start_date, req.end_date)
    date_range = f"{req.start_date} to {req.end_date}"
    try:
        draft = llm.generate_clinician_summary(entries, date_range, weekly_checkins=checkins)
    except llm.LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "draft": draft,
        "entry_count": len(entries),
        "checkin_count": len(checkins),
        "date_range": date_range,
    }


@app.post("/clinician-export")
async def clinician_export(req: ClinicianExportRequest) -> Response:
    """Return a Markdown file of the edited clinician summary."""
    lines = [
        "---",
        f"date-range: {req.start_date} to {req.end_date}",
        f"generated: {req.generated_date}",
        "note: This is a draft prepared by the patient and should be reviewed before sharing.",
        "---",
        "",
        f"# Clinician Appointment Summary",
        f"**Period:** {req.start_date} to {req.end_date}",
        f"**Prepared:** {req.generated_date}",
        "",
        req.draft,
        "",
        "---",
        "*This summary was drafted with AI assistance from personal journal entries. "
        "Review and edit before sharing with your clinician.*",
    ]
    markdown = "\n".join(lines)
    return Response(
        content=markdown,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="clinician-summary-{req.start_date}-to-{req.end_date}.md"'},
    )


@app.get("/timeline")
async def timeline() -> list[dict]:
    """Return all entries, newest first, with previews, signals, and reflection status."""
    entries = get_all_entries()
    return [
        {
            "entry_date": e["entry_date"],
            "notes_preview": (e["notes"] or "")[:100],
            "mode": e["mode"],
            "has_kept_summary": bool(e["kept_summary"]),
            "reflection_status": e.get("reflection_status") or "ready",
            "signals": {
                "energy": e.get("energy"),
                "sleep_quality": e.get("sleep_quality"),
                "sensory_load": e.get("sensory_load"),
                "overwhelm": e.get("overwhelm"),
            },
        }
        for e in entries
    ]


@app.get("/tags")
async def get_tags() -> list[dict]:
    """Return all tags grouped by category."""
    return get_all_tags()


@app.get("/tags/search")
async def tag_search(q: str, category: str | None = None) -> list[dict]:
    """Search tags by name prefix."""
    if not q.strip():
        return []
    return search_tags(q, category)


@app.post("/entry-tags")
async def upsert_entry_tags(req: Request) -> dict:
    """
    Save tags for an entry. Body: {entry_id, tags: [{name, category}, ...]}
    Creates missing tags and replaces all tags for that entry.
    """
    body = await req.json()
    entry_id = body.get("entry_id")
    tag_list = body.get("tags", [])
    if not entry_id:
        raise HTTPException(status_code=400, detail="entry_id required")
    tag_ids = [get_or_create_tag(t["name"], t["category"]) for t in tag_list]
    save_entry_tags(entry_id, tag_ids)
    saved = get_entry_tags(entry_id)
    return {"tags": saved}


@app.get("/entry-tags/{entry_id}")
async def get_tags_for_entry(entry_id: int) -> dict:
    """Return tags for a specific entry."""
    return {"tags": get_entry_tags(entry_id)}


@app.get("/stats")
async def stats() -> dict:
    """Return entry counts and summary statistics."""
    entries = get_all_entries()
    total = len(entries)

    from datetime import timedelta
    cutoff = date.today() - timedelta(days=180)
    entries_180d = [e for e in entries if e["entry_date"] >= str(cutoff)]

    quick_count = sum(1 for e in entries if e["mode"] == "quick")
    detailed_count = sum(1 for e in entries if e["mode"] == "detailed")
    kept_count = sum(1 for e in entries if e["kept_summary"])
    avg_pct = round((kept_count / total * 100) if total > 0 else 0)

    return {
        "total_entries": total,
        "entries_180d": len(entries_180d),
        "quick_count": quick_count,
        "detailed_count": detailed_count,
        "avg_kept_summaries_pct": f"{avg_pct}%",
    }


# ---------------------------------------------------------------------------
# Settings / Provider config
# ---------------------------------------------------------------------------
@app.get("/settings")
async def get_settings() -> dict:
    """Return full settings for the Settings UI. Key is never returned."""
    return llm.get_settings()


@app.post("/settings")
async def update_settings(req: SettingsUpdateRequest) -> dict:
    """Update provider settings. Takes effect immediately (hot-switch). Key is write-only."""
    updates = {}
    if req.transport is not None:
        updates["transport"] = req.transport
    if req.base_url is not None:
        updates["base_url"] = req.base_url
    if req.api_key is not None:
        updates["api_key"] = req.api_key
    if req.model is not None:
        updates["model"] = req.model
    if req.request_timeout is not None:
        updates["request_timeout"] = req.request_timeout
    if req.model_overrides is not None:
        updates["model_overrides"] = req.model_overrides
    if req.ollama_base_url is not None:
        updates["ollama_base_url"] = req.ollama_base_url

    if not updates:
        raise HTTPException(status_code=400, detail="No settings fields provided.")

    # set_provider_config handles persistence + Ollama unload
    return llm.set_provider_config(updates)


@app.get("/provider-info")
async def provider_info() -> dict:
    """Alias for the UI privacy badge. Returns transport + model only."""
    return llm.get_provider_info()


@app.get("/models")
async def get_cloud_models() -> dict:
    """Fetch available models from the cloud provider (Yuxor) using current config."""
    cfg = llm.get_config()
    base_url = cfg.get("base_url", "https://api.yuxor.tech/v1")
    api_key = cfg.get("api_key", "")
    models = llm.fetch_available_models(base_url, api_key)
    return {"models": models}


@app.get("/local-models")
async def get_local_models() -> dict:
    """Fetch installed models from Ollama."""
    cfg = llm.get_config()
    # Use stored ollama_base_url if set, otherwise default
    base_url = cfg.get("ollama_base_url", cfg.get("base_url", "http://localhost:11434"))
    models = llm.fetch_local_models(base_url)
    return {"models": models}


@app.post("/test-connection")
async def test_connection() -> dict:
    """Run a minimal completion against the current config. Returns success info."""
    cfg = llm.get_config()
    result = llm.test_connection(cfg)
    return result


# ---------------------------------------------------------------------------
# Working notes and weekly check-ins
# ---------------------------------------------------------------------------
class WorkingNotesRequest(BaseModel):
    entry_id: int
    working_notes: str


@app.post("/working-notes")
async def post_working_notes(req: WorkingNotesRequest) -> dict:
    """Save or update working notes for an entry."""
    update_working_notes(req.entry_id, req.working_notes)
    return {"ok": True}


class WeeklyCheckinRequest(BaseModel):
    week_start: str  # ISO date (Monday)
    spoons: int | None = None
    meltdown_count: int | None = None
    shutdown_count: int | None = None
    notes: str | None = None


@app.post("/weekly-checkin")
async def post_weekly_checkin(req: WeeklyCheckinRequest) -> dict:
    """Create or update a weekly check-in record."""
    checkin = get_or_create_weekly_checkin(req.week_start)
    update_weekly_checkin(
        req.week_start,
        spoons=req.spoons,
        meltdown_count=req.meltdown_count,
        shutdown_count=req.shutdown_count,
        notes=req.notes,
    )
    return get_or_create_weekly_checkin(req.week_start)


@app.get("/weekly-checkins")
async def get_weekly_checkins_route(weeks: int = 12) -> list[dict]:
    """Return weekly check-in records for the last N weeks."""
    return get_weekly_checkins(weeks=weeks)


# ---------------------------------------------------------------------------
# Signal series for charts (deterministic — no model call)
# ---------------------------------------------------------------------------
@app.get("/signal-series")
async def signal_series(days: int = 90) -> dict:
    """Return numeric signal series for charting.

    Maps low/med/high -> 1/2/3. Returns entries ordered oldest-first
    for easy SVG polyline plotting. Also returns weekly check-ins for
    spoons/meltdown/shutdown overlay.
    """
    from datetime import timedelta
    cutoff = str(date.today() - timedelta(days=days))
    entries = get_entries_in_range(cutoff, str(date.today()))

    def to_num(v):
        return {"low": 1, "med": 2, "high": 3}.get(v or "", None)

    series = []
    for e in entries:
        series.append({
            "date": e["entry_date"],
            "energy": to_num(e.get("energy")),
            "sleep_quality": to_num(e.get("sleep_quality")),
            "sensory_load": to_num(e.get("sensory_load")),
            "overwhelm": to_num(e.get("overwhelm")),
        })

    # Sort oldest first for chart rendering
    series.sort(key=lambda x: x["date"])

    checkins = get_weekly_checkins(weeks=max(1, (days // 7) + 1))
    return {"series": series, "weekly_checkins": checkins}


# ---------------------------------------------------------------------------
# Auto-struct: suggest tags and signals from entry text
# ---------------------------------------------------------------------------
class AutostructRequest(BaseModel):
    entry_text: str


@app.post("/autostruct")
async def autostruct(req: AutostructRequest, background_tasks: BackgroundTasks) -> dict:
    """Suggest signal levels and tags for a journal entry.

    Runs the model asynchronously and immediately returns a task ID.
    The client polls /autostruct-status/{task_id} until ready.
    """
    if not req.entry_text.strip():
        raise HTTPException(status_code=400, detail="Entry text cannot be empty.")

    task_id = str(uuid.uuid4())
    config_snapshot = llm.snapshot_config()
    existing_tags = get_all_tags()

    _autostruct_tasks[task_id] = {"status": "pending", "result": None}

    background_tasks.add_task(
        _run_autostruct_task, task_id, req.entry_text, config_snapshot, existing_tags
    )

    return {"task_id": task_id}


def _run_autostruct_task(task_id: str, entry_text: str, config_snapshot: dict, existing_tags: list[dict]) -> None:
    """Background task: run autostruct and store the result."""
    try:
        result = llm.suggest_autostruct_from_snapshot(config_snapshot, entry_text, existing_tags)
        _autostruct_tasks[task_id] = {"status": "ready", "result": result}
    except llm.LLMError as exc:
        _autostruct_tasks[task_id] = {"status": "error", "error": str(exc)}
    except Exception as exc:
        _autostruct_tasks[task_id] = {"status": "error", "error": "Unexpected error during auto-structuring."}


@app.get("/autostruct-status/{task_id}")
async def autostruct_status(task_id: str) -> dict:
    """Return the current status and result for an auto-struct task."""
    task = _autostruct_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    resp = {"status": task["status"]}
    if task["status"] == "ready":
        resp["result"] = task["result"]
    elif task["status"] == "error":
        resp["error"] = task.get("error", "Unknown error")
    return resp


# ---------------------------------------------------------------------------
# Vault endpoints (Phase S2 — encryption at rest)
# ---------------------------------------------------------------------------

@app.get("/vault-status")
async def vault_status() -> dict:
    """Return whether the vault is set up and whether it's currently unlocked."""
    from app import vault
    state = vault.get_vault_state()
    return state.to_dict()


class VaultSetupRequest(BaseModel):
    passphrase: str
    recovery_key: str


@app.post("/vault-setup")
async def vault_setup(req: VaultSetupRequest) -> dict:
    """First-time setup: create vault.json, encrypt the DB, and generate recovery key.

    Called once. After this, the app requires unlock on every start.
    """
    from app import vault as _v
    import app.crypto as crypto
    from app.database import DB_PATH

    if _v.is_vault_setup():
        raise HTTPException(status_code=409, detail="Vault already set up.")
    if len(req.passphrase) < 8:
        raise HTTPException(status_code=400, detail="Passphrase must be at least 8 characters.")
    if len(req.recovery_key) < 8:
        raise HTTPException(status_code=400, detail="Recovery key must be at least 8 characters.")

    # Encrypt existing API key if present
    cfg = llm.get_config()
    api_key_plaintext = cfg.get("api_key", "")
    encrypted_api_key = ""
    if api_key_plaintext:
        # Generate a temporary salt for encryption (setup_vault will generate its own)
        import secrets as _secrets
        temp_salt = _secrets.token_bytes(16)
        encrypted_api_key = crypto.encrypt_value(req.passphrase, api_key_plaintext, temp_salt)
        # Store the temp salt alongside so we can decrypt later
        # Actually: we need to store with the same salt that setup_vault creates
        # Let's just call setup_vault first (which creates the salt), then encrypt
        encrypted_api_key = ""

    # Step 1: backup plaintext DB first (before creating vault.json)
    plaintext_backup = DB_PATH.with_name("mood.db.plaintext.backup")
    shutil.copy2(str(DB_PATH), str(plaintext_backup))

    # Step 2: create vault.json (generates salt)
    _v.setup_vault(
        passphrase=req.passphrase,
        encrypted_api_key="",  # will update after we have the salt
        encrypted_recovery_key=req.recovery_key,  # stored as plain text
    )

    # Step 3: now encrypt API key with the vault's salt
    vault_data = _v.load_vault_data()
    vault_salt = _v.b64decode(vault_data["salt"])
    if api_key_plaintext:
        encrypted_api_key = crypto.encrypt_value(req.passphrase, api_key_plaintext, vault_salt)
        vault_data["encrypted_api_key"] = encrypted_api_key
        _v.save_vault_data(vault_data)

    # Step 4: derive DB key
    db_key = crypto.derive_db_key(req.passphrase, vault_salt)
    db_key_hex = db_key.hex()

    # Step 5: encrypt the DB using sqlcipher_export
    temp_encrypted = DB_PATH.with_name("mood.db.enc.tmp")
    enc_conn = sqlcipher3.connect(str(temp_encrypted))
    enc_conn.execute(f"PRAGMA key = '{db_key_hex}'")
    enc_conn.execute(f"ATTACH DATABASE '{DB_PATH}' AS src KEY ''")
    enc_conn.execute("SELECT sqlcipher_export('main', 'src')")
    enc_conn.execute("DETACH DATABASE src")
    enc_conn.close()

    # Step 6: verify encrypted DB opens with passphrase
    verify_conn = sqlcipher3.connect(str(temp_encrypted))
    verify_conn.execute(f"PRAGMA key = '{db_key_hex}'")
    try:
        count = verify_conn.execute("SELECT count(*) FROM entries").fetchone()
        verify_conn.close()
    except Exception as exc:
        temp_encrypted.unlink()
        raise HTTPException(status_code=500, detail=f"DB encryption verification failed: {exc}") from exc

    # Step 7: atomically replace original with encrypted
    DB_PATH.unlink()
    temp_encrypted.rename(DB_PATH)

    # Step 8: unlock vault
    _v.unlock_vault(req.passphrase)
    install_vault_connection_factory(lambda: _v._vault.conn)

    return {
        "recovery_key": req.recovery_key,
        "message": "Encryption enabled. Save your recovery key somewhere safe.",
    }


@app.post("/vault-unlock")
async def vault_unlock(request: Request) -> dict:
    """Unlock the vault with a passphrase."""
    from app import vault as _v
    body = await request.json()
    passphrase = body.get("passphrase", "")
    try:
        _v.unlock_vault(passphrase)
        install_vault_connection_factory(lambda: _v._vault.conn)

        # Decrypt API key if stored
        vault_data = _v.load_vault_data()
        if vault_data and vault_data.get("encrypted_api_key"):
            try:
                _v._vault.decrypt_api_key(vault_data["encrypted_api_key"])
            except Exception:
                pass

        state = _v.get_vault_state()
        return {**state.to_dict()}
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Incorrect passphrase.")


@app.post("/vault-lock")
async def vault_lock() -> dict:
    """Lock the vault and clear keys from memory."""
    from app import vault as _v
    _v.lock_vault()
    install_vault_connection_factory(None)
    return {"ok": True}


@app.post("/vault-recovery-unlock")
async def vault_recovery_unlock(request: Request) -> dict:
    """Unlock using a recovery key and set a new passphrase.

    The recovery key is stored in plain text in vault.json so it can be verified directly.
    Since we don't have the old passphrase, the DB encryption key cannot be changed.
    The API key remains encrypted with the original salt; it will be re-keyed the next time
    the user enters their old passphrase (if they remember it).
    """
    from app import vault as _v
    import app.crypto as crypto
    import secrets
    from app.database import DB_PATH

    body = await request.json()
    typed_recovery_key = body.get("recovery_key", "")
    new_passphrase = body.get("new_passphrase", "")
    if len(new_passphrase) < 8:
        raise HTTPException(status_code=400, detail="New passphrase must be at least 8 characters.")
    if len(typed_recovery_key) < 8:
        raise HTTPException(status_code=400, detail="Invalid recovery key.")

    vault_data = _v.load_vault_data()
    if not vault_data:
        raise HTTPException(status_code=400, detail="Vault not set up.")

    stored_recovery_key = vault_data.get("encrypted_recovery_key", "")
    if not stored_recovery_key:
        raise HTTPException(status_code=400, detail="No recovery key stored.")

    # Verify typed key matches stored key
    if not secrets.compare_digest(stored_recovery_key, typed_recovery_key):
        raise HTTPException(status_code=401, detail="Incorrect recovery key.")

    # Keep the original salt — we can't re-derive the old DB key without the old passphrase
    # The API key was encrypted with the original passphrase+salt; we try to re-encrypt it
    old_salt = _v.b64decode(vault_data["salt"])
    enc_api_key = vault_data.get("encrypted_api_key", "")
    new_enc_api_key = enc_api_key

    if enc_api_key and ":" in enc_api_key:
        # API key is encrypted (AES-GCM format). Try to decrypt with new_passphrase+old_salt.
        # If it fails, leave it as-is — user will need to re-enter the API key.
        try:
            plaintext = crypto.decrypt_value(new_passphrase, enc_api_key, old_salt)
            new_enc_api_key = crypto.encrypt_value(new_passphrase, plaintext, old_salt)
        except Exception:
            pass  # keep old value

    # Update vault: keep salt, update encrypted_recovery_key and encrypted_api_key
    new_vault_data = dict(vault_data)
    new_vault_data["encrypted_api_key"] = new_enc_api_key
    new_vault_data["encrypted_recovery_key"] = typed_recovery_key  # plain text
    _v.save_vault_data(new_vault_data)

    # Unlock with new passphrase
    _v.unlock_vault(new_passphrase)
    install_vault_connection_factory(lambda: _v._vault.conn)

    return {
        "ok": True,
        "message": "Passphrase changed. Your journal entries remain encrypted with the original passphrase. "
                   "If you remember your old passphrase, go to Settings to change it properly.",
    }


# Import shutil at module level (needed in vault-setup)
import shutil
import sqlcipher3


# ---------------------------------------------------------------------------
# Ask-journal: natural-language Q&A over journal history
# ---------------------------------------------------------------------------
class AskRequest(BaseModel):
    question: str
    keywords: list[str] = []  # optional extracted keywords for pre-filtering
    date_hints: list[str] = []  # optional date fragments (e.g. ["2026-03", "March"])
    tag_names: list[str] = []  # optional tag names from the question


@app.post("/ask")
async def ask_journal(req: AskRequest, background_tasks: BackgroundTasks) -> dict:
    """Answer a natural-language question about journal entries.

    Retrieval pre-filters candidate entries using keywords/dates/tags, then
    assembles context and calls the model asynchronously. The client polls
    /ask-status/{task_id} until ready.

    Embeddings are used if available; otherwise falls back to keyword ranking.
    """
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty.")

    task_id = str(uuid.uuid4())
    config_snapshot = llm.snapshot_config()

    _ask_tasks[task_id] = {"status": "pending", "result": None}

    background_tasks.add_task(
        _run_ask_task, task_id, req.question, req.keywords, req.date_hints, req.tag_names, config_snapshot
    )

    return {"task_id": task_id}


def _run_ask_task(
    task_id: str,
    question: str,
    keywords: list[str],
    date_hints: list[str],
    tag_names: list[str],
    config_snapshot: dict,
) -> None:
    """Background task: retrieve entries, optionally use embeddings, then answer."""
    try:
        # Determine date range for the context header
        from datetime import date, timedelta
        today = date.today()
        # Cap at 200 days for context size
        cutoff = str(today - timedelta(days=200))
        all_entries = get_entries_in_range(cutoff, str(today))

        if not all_entries:
            _ask_tasks[task_id] = {
                "status": "ready",
                "result": {
                    "answer": "You don't have any journal entries in the last 200 days to answer this question.",
                    "cited_dates": [],
                    "method": "no_entries",
                    "token_count": 0,
                },
            }
            return

        # Retrieve candidate entries using keyword + recency ranking
        candidates = search_entries_for_qa(
            keywords=keywords,
            date_hints=date_hints,
            tag_names=tag_names,
            limit=20,
        )

        if not candidates:
            # No keyword matches — use recency only
            candidates = all_entries[:20]

        # Check if embeddings are available and entries have stored embeddings
        cfg = config_snapshot
        embeddings_available = False
        embeddings_enabled = cfg.get("embeddings_enabled", True)

        if embeddings_enabled and cfg.get("transport") == "openai_compatible":
            # Probe for embeddings support
            try:
                embeddings_available = llm.check_embeddings_support(
                    cfg.get("base_url", "https://api.yuxor.tech/v1"),
                    cfg.get("api_key", ""),
                )
            except Exception:
                embeddings_available = False

        method = "keyword"
        if embeddings_available:
            # Try to use embeddings for semantic ranking
            from app.database import get_all_embeddings
            all_embs = get_all_embeddings()

            # Attach stored embeddings to candidates
            for c in candidates:
                emb = all_embs.get(c["id"])
                if emb:
                    c["_embedding"] = emb

            # If enough entries have embeddings, re-rank by similarity
            entries_with_embs = [c for c in candidates if c.get("_embedding")]
            if len(entries_with_embs) >= 3:
                emb_model = cfg.get("model", "claude-opus-4-7")
                ranked = llm.rank_by_embedding(
                    question,
                    candidates,
                    cfg.get("base_url", "https://api.yuxor.tech/v1"),
                    cfg.get("api_key", ""),
                    emb_model,
                )
                candidates = ranked
                method = "embeddings"

        date_range = f"{cutoff} to {today}"
        result = llm.answer_journal_question_from_snapshot(
            config_snapshot, question, candidates, date_range
        )
        result["method"] = method

        _ask_tasks[task_id] = {"status": "ready", "result": result}

    except llm.LLMError as exc:
        _ask_tasks[task_id] = {"status": "error", "error": str(exc)}
    except Exception as exc:
        _ask_tasks[task_id] = {"status": "error", "error": "Unexpected error answering your question."}


@app.get("/ask-status/{task_id}")
async def ask_status(task_id: str) -> dict:
    """Return the current status and result for an ask-journal task."""
    task = _ask_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    resp = {"status": task["status"]}
    if task["status"] == "ready":
        resp["result"] = task["result"]
    elif task["status"] == "error":
        resp["error"] = task.get("error", "Unknown error")
    return resp


@app.get("/embeddings-info")
async def embeddings_info() -> dict:
    """Return whether embeddings are available and configured."""
    cfg = llm.get_config()
    available = False
    if cfg.get("transport") == "openai_compatible":
        try:
            available = llm.check_embeddings_support(
                cfg.get("base_url", "https://api.yuxor.tech/v1"),
                cfg.get("api_key", ""),
            )
        except Exception:
            available = False
    return {
        "embeddings_available": available,
        "embeddings_enabled": cfg.get("embeddings_enabled", True),
    }


# ---------------------------------------------------------------------------
# Correlation analytics (Phase D — deterministic compute + LLM narration)
# ---------------------------------------------------------------------------

# In-memory task store for correlation narration (runs async)
_correlation_tasks: dict[str, dict] = {}


@app.get("/correlations")
async def get_correlations(days: int = 180) -> dict:
    """Return computed correlation statistics (deterministic — no model call)."""
    try:
        stats = compute_correlations(days=days)
        return stats
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


class NarrateCorrelationsRequest(BaseModel):
    days: int = 180


@app.post("/narrate-correlations")
async def narrate_correlations(req: NarrateCorrelationsRequest, background_tasks: BackgroundTasks) -> dict:
    """Compute correlation stats then narrate them via LLM. Async — poll via task ID."""
    task_id = str(uuid.uuid4())
    config_snapshot = llm.snapshot_config()

    _correlation_tasks[task_id] = {"status": "pending", "result": None}

    background_tasks.add_task(
        _run_narrate_correlations, task_id, req.days, config_snapshot
    )

    return {"task_id": task_id}


def _run_narrate_correlations(task_id: str, days: int, config_snapshot: dict) -> None:
    """Background task: compute stats then narrate via LLM."""
    try:
        stats = compute_correlations(days=days)
        narration = llm.narrate_correlations_from_snapshot(config_snapshot, stats)
        _correlation_tasks[task_id] = {
            "status": "ready",
            "result": {
                "narration": narration,
                "stats": stats,
            },
        }
    except llm.LLMError as exc:
        _correlation_tasks[task_id] = {"status": "error", "error": str(exc)}
    except Exception as exc:
        _correlation_tasks[task_id] = {"status": "error", "error": "Unexpected error during analysis."}


@app.get("/narrate-correlations-status/{task_id}")
async def narrate_correlations_status(task_id: str) -> dict:
    """Poll the result of a correlation narration task."""
    task = _correlation_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    resp = {"status": task["status"]}
    if task["status"] == "ready":
        resp["result"] = task["result"]
    elif task["status"] == "error":
        resp["error"] = task.get("error", "Unknown error")
    return resp
