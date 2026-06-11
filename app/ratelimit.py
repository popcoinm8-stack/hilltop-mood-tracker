"""In-memory rate limiting for network mode.

A simple sliding-window rate limiter to defeat online brute force on
/auth/login and similar endpoints.

Threat model: casual LAN neighbor, not a motivated internet attacker.
The LAN IP filter handles the latter. In-memory state is acceptable for
a single-process FastAPI app — restart wipes counters, and an attacker
can also wait 60 seconds.

If the app is later fronted by a reverse proxy, swap this for a Redis-
backed limiter. For now, stdlib only.
"""
import threading
import time
from collections import deque


# Per-endpoint limits. (max_requests, window_seconds)
LIMITS: dict[str, tuple[int, int]] = {
    "/auth/login": (5, 60),
    "/auth/check-device": (10, 60),
    # Catch-all: any other path is rate-limited at 60 req/min/IP as defense-in-depth.
    "_default": (60, 60),
}

# In-memory store: { "ip:endpoint": deque[timestamp] }
_store: dict[str, deque[float]] = {}
_lock = threading.Lock()

# Periodically purge stale entries (called from a background task in main.py).
def purge_stale_entries() -> int:
    """Remove all entries whose latest timestamp is older than the longest window.

    Returns the number of entries removed.
    """
    now = time.time()
    max_window = max(window for _, window in LIMITS.values())
    cutoff = now - max_window

    removed = 0
    with _lock:
        stale_keys = [
            key for key, timestamps in _store.items()
            if not timestamps or timestamps[-1] < cutoff
        ]
        for key in stale_keys:
            del _store[key]
            removed += 1
    return removed


def check_and_record(key: str, max_requests: int, window_seconds: int) -> tuple[bool, int]:
    """Check whether `key` (typically "ip:endpoint") is within the rate limit.

    If under the limit, record this request and return (True, 0).
    If over, return (False, retry_after_seconds) without recording.
    """
    now = time.time()
    cutoff = now - window_seconds

    with _lock:
        timestamps = _store.get(key)
        if timestamps is None:
            timestamps = deque()
            _store[key] = timestamps

        # Prune expired timestamps from the left.
        while timestamps and timestamps[0] < cutoff:
            timestamps.popleft()

        if len(timestamps) >= max_requests:
            # Reject. retry_after = time until oldest timestamp expires.
            retry_after = max(1, int(timestamps[0] + window_seconds - now) + 1)
            return False, retry_after

        # Accept and record.
        timestamps.append(now)
        return True, 0


def is_allowed(client_ip: str, endpoint: str) -> tuple[bool, int]:
    """Check if a request from `client_ip` to `endpoint` is allowed.

    Returns (allowed, retry_after_seconds).
    """
    max_requests, window_seconds = LIMITS.get(endpoint, LIMITS["_default"])
    key = f"{client_ip}:{endpoint}"
    return check_and_record(key, max_requests, window_seconds)
