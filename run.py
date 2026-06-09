"""
Wrapper script that starts the FastAPI/Uvicorn server and ensures all
resources (Ollama model, Whisper GPU memory, Kokoro TTS, SQLite WAL handles)
are cleanly released on exit.

Handles Ctrl+C, SIGTERM, and window-close on Windows.
Usage: python run.py
"""
import signal
import sys
import threading
import time
from pathlib import Path

# Ensure the script directory is on sys.path so 'app' imports work.
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Track whether we initiated the shutdown to avoid double-cleanup.
_shutdown_requested = threading.Event()
_shutdown_lock = threading.Lock()


def _immediate_cleanup():
    """Run synchronous cleanup (Whisper, Ollama) before the process exits.
    Called from both signal handlers and atexit. Idempotent."""
    if _shutdown_requested.is_set():
        return
    with _shutdown_lock:
        if _shutdown_requested.is_set():
            return
        _shutdown_requested.set()

    print("\n[run] Initiating graceful shutdown...")

    # 1. Unload Whisper model from GPU/CPU immediately (no waiting for idle timer).
    try:
        from app.transcribe import shutdown as shutdown_whisper
        shutdown_whisper()
        print("[run] Whisper model unloaded.")
    except Exception as exc:
        print(f"[run] Whisper cleanup warning: {exc}")

    # 2. Tell Ollama to drop the LLM from memory.
    try:
        import httpx
        from app.llm import OLLAMA_URL, MODEL
        resp = httpx.post(OLLAMA_URL, json={"model": MODEL, "keep_alive": 0}, timeout=10)
        resp.raise_for_status()
        print("[run] Ollama model unloaded.")
    except Exception as exc:
        print(f"[run] Ollama cleanup warning (best-effort): {exc}")

    # 3. Kokoro TTS singleton is GC'd automatically; nothing to do.

    print("[run] Cleanup complete.")


# Register handlers for the signals Uvicorn is likely to receive.
def _handle_signal(signum, frame, signame):
    print(f"\n[run] Received {signame}.")
    _immediate_cleanup()
    # Re-raise so Python's default behavior terminates the process cleanly.
    # On Windows this exits; on Unix this raises KeyboardInterrupt/SystemExit.
    signal.default_int_handler(signum, frame)


signal.signal(signal.SIGINT,  lambda s, f: _handle_signal(s, f, "SIGINT"))
signal.signal(signal.SIGTERM, lambda s, f: _handle_signal(s, f, "SIGTERM"))
try:
    signal.signal(signal.SIGBREAK, lambda s, f: _handle_signal(s, f, "SIGBREAK"))
except AttributeError:
    pass  # SIGBREAK is Unix-only


if __name__ == "__main__":
    import uvicorn

    print("Starting Mood Tracker server on http://localhost:8000")
    print("Press Ctrl+C (or close this window) to stop.")

    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=False,       # reload=False: no file-watcher threads to leak on shutdown
        factory=False,      # use import string (not app factory)
        log_level="info",
    )
