"""
Wrapper script that starts the FastAPI/Uvicorn server and ensures all
resources (Ollama model, Whisper GPU memory, Kokoro TTS, SQLite WAL handles)
are cleanly released on exit.

Handles Ctrl+C, SIGTERM, and window-close on Windows.

Usage:
    python run.py                          # Local mode (localhost only, HTTP)
    python run.py --network                # Network mode (LAN access, HTTPS, auth)
    python run.py --port 9000              # Custom port
    python run.py --network --port 443     # Network mode on port 443
"""
import argparse
import os
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

    # 2. For Ollama transport: tell it to drop the LLM from memory.
    #    No-op for openai_compatible.
    try:
        import httpx
        from app import llm
        cfg = llm.get_config()
        if cfg.get("transport") == "ollama":
            base_url = cfg.get("base_url", "http://localhost:11434")
            model = cfg.get("model", "")
            url = base_url.rstrip("/") + "/api/generate"
            resp = httpx.post(url, json={"model": model, "keep_alive": 0}, timeout=10)
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
    pass  # SIGBREAK is Windows-only


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mood Tracker server")
    parser.add_argument(
        "--network",
        action="store_true",
        help="Enable network mode: bind to 0.0.0.0, serve HTTPS, require auth for LAN access.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to listen on (default: 8000).",
    )
    parser.add_argument(
        "--bind",
        type=str,
        default=None,
        help="Bind address (default: 127.0.0.1 in local mode, 0.0.0.0 in network mode). "
             "Override to bind to a specific IP (e.g. --bind 100.121.33.100 to listen "
             "only on Tailscale).",
    )
    args = parser.parse_args()

    network_mode = args.network
    port = args.port
    bind_addr = args.bind or ("0.0.0.0" if network_mode else "127.0.0.1")

    # Inject network mode flag into the app module via environment variable.
    # main.py reads this at import time to configure middleware.
    if network_mode:
        os.environ["MT_NETWORK_MODE"] = "1"
    else:
        os.environ.pop("MT_NETWORK_MODE", None)

    # TLS setup: only needed in network mode.
    ssl_kwargs = {}
    if network_mode:
        from app.tls import ensure_self_signed_cert, get_local_ip_addresses, load_cert_paths

        local_ips = get_local_ip_addresses()
        cert_path, key_path = ensure_self_signed_cert(ip_san=local_ips)
        ssl_kwargs["ssl_certfile"] = str(cert_path)
        ssl_kwargs["ssl_keyfile"] = str(key_path)

        print("[run] ============================================================")
        print("[run]  Network mode enabled -- HTTPS + authentication required")
        print("[run] ============================================================")
        print(f"[run]  Bound to: {bind_addr}")
        print(f"[run]  Access URLs:")
        if bind_addr in ("0.0.0.0", "127.0.0.1"):
            # Bound to all interfaces — show all detected IPs.
            print(f"[run]    https://localhost:{port}")
            for ip in local_ips:
                if ip != "127.0.0.1":
                    print(f"[run]    https://{ip}:{port}")
        else:
            # Bound to a specific address — only show that one.
            print(f"[run]    https://{bind_addr}:{port}")
        print()
        print("[run]  First time? Open one of those URLs on your phone:")
        print("[run]    1. Accept the security warning (self-signed certificate)")
        print("[run]    2. Set an access password (10+ characters)")
        print("[run]    3. Approve your phone from the desktop Settings panel")
        print("[run] ============================================================")
    else:
        print(f"Starting Mood Tracker server on http://localhost:{port}")
        print("Press Ctrl+C (or close this window) to stop.")

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=bind_addr,
        port=port,
        reload=False,       # reload=False: no file-watcher threads to leak on shutdown
        factory=False,      # use import string (not app factory)
        log_level="info",
        **ssl_kwargs,
    )