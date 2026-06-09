"""FastAPI application for mood tracker."""
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

import numpy as np

import httpx
from fastapi import FastAPI, HTTPException
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
    get_entry_tags,
    get_recent_summaries,
    get_or_create_tag,
    get_or_create_weekly_checkin,
    get_weekly_checkins,
    init_db,
    save_entry,
    save_entry_tags,
    search_tags,
    update_entry_signals,
    update_kept_summary,
    update_weekly_checkin,
    update_working_notes,
)
from app.llm import (
    OLLAMA_URL,
    MODEL,
    analyze_trends,
    generate_clinician_summary,
    generate_draft,
)
from app.transcribe import transcribe
from app.tts import speak


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
    # 2. Ask Ollama to unload the LLM so VRAM is freed
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(OLLAMA_URL, json={"model": MODEL, "keep_alive": 0})
    except Exception:
        pass  # best-effort; server may already be stopping


app = FastAPI(lifespan=lifespan)

# Mount static files (index.html lives in app/static/)
BASE_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# ---------------------------------------------------------------------------
# DB init on startup
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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/")
async def serve_index() -> FileResponse:
    return FileResponse(str(BASE_DIR / "static" / "index.html"))


@app.post("/reflect", response_model=ReflectResponse)
async def reflect(req: ReflectRequest) -> ReflectResponse:
    """Save today's entry directly with LLM reflection appended to notes."""
    if not req.notes.strip():
        raise HTTPException(status_code=400, detail="Notes cannot be empty.")

    mode = req.mode if req.mode in ("quick", "detailed") else "quick"
    today = date.today()

    # Append transcription to notes if provided
    notes = req.notes
    if req.transcription and req.transcription.strip():
        notes = notes + "\n\n[Dictation]: " + req.transcription

    # Get recent history for LLM context
    recent = get_recent_summaries(days=7)

    # Call LLM to generate a brief reflection
    try:
        llm_reflection = generate_draft(notes, recent, mode=mode)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"LLM call failed: {exc}",
        )

    # Save entry: notes contain both user text + dictation + LLM reflection
    entry_id = save_entry(
        entry_date=today,
        notes=notes,
        transcription=req.transcription,
        kept_summary=llm_reflection,
        mode=mode,
        energy=req.energy,
        sleep_quality=req.sleep_quality,
        sensory_load=req.sensory_load,
        overwhelm=req.overwhelm,
    )

    return ReflectResponse(entry_id=entry_id, mode=mode)


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
    """Export all entries as JSON for backup."""
    entries = export_all()
    return JSONResponse(
        content={"exported_at": str(date.today()), "entries": entries},
        headers={
            "Content-Disposition": "attachment; filename=mood-export.json"
        },
    )


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
        result = analyze_trends(summaries, date_range=date_range)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"LLM call failed: {exc}")
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
    """Compile entries in the given date range and draft a clinician-facing summary."""
    entries = get_entries_in_range(req.start_date, req.end_date)
    date_range = f"{req.start_date} to {req.end_date}"
    try:
        draft = generate_clinician_summary(entries, date_range)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"LLM call failed: {exc}")
    return {
        "draft": draft,
        "entry_count": len(entries),
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
    """Return all entries, newest first, with previews, signals, and tags."""
    entries = get_all_entries()
    return [
        {
            "entry_date": e["entry_date"],
            "notes_preview": (e["notes"] or "")[:100],
            "mode": e["mode"],
            "has_kept_summary": bool(e["kept_summary"]),
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
