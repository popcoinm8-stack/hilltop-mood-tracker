"""Pluggable model provider: local Ollama or OpenAI-compatible cloud (Yuxor BYOK).

Config lives in data/config.json (gitignored). Load server-side at startup.
The API key NEVER crosses to the browser, never appears in logs or error text.

Two transports:
  - "ollama": calls POST /api/generate (existing behaviour).
  - "openai_compatible": calls POST {base_url}/chat/completions with
    Authorization: Bearer <key>, OpenAI chat-format messages (SSE).
    Yuxor's unified gateway serves every model through one endpoint;
    only the model string changes.

Runtime hot-switching:
  All three public functions delegate to a single internal helper
  `complete(system_prompt, user_text, job, max_tokens, temperature)`
  that reads from a mutable in-memory state dict. `set_provider_config()`
  updates that state AND persists to disk AND (if switching TO cloud from
  ollama) fires a non-blocking Ollama unload. Background tasks snapshot
  the active config at task-creation time so mid-flight switches are safe.
"""
import json
import logging
import re
import threading
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CONFIG_PATH = Path(__file__).resolve().parent.parent / "data" / "config.json"
EXAMPLE_PATH = Path(__file__).resolve().parent.parent / "data" / "config.example"

_DEFAULTS = {
    "transport": "openai_compatible",
    "base_url": "https://api.yuxor.tech/v1",
    "api_key": "",
    "model": "claude-opus-4-7",
    "request_timeout": 180,
    "model_overrides": {},
}

# ---------------------------------------------------------------------------
# Mutable in-memory state — the single source of truth at runtime.
# Lock must be held for all reads and writes.
# ---------------------------------------------------------------------------
_state: dict = dict(_DEFAULTS)
_state_lock = threading.Lock()


def _load_config() -> dict:
    """Load from disk, merged with defaults."""
    cfg = dict(_DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            if isinstance(user_cfg, dict):
                cfg.update(user_cfg)
        except Exception as exc:
            logger.warning("Failed to read config.json: %s", exc)
    return cfg


def _persist_config(cfg: dict) -> None:
    """Write to disk (key is never logged)."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    safe_keys = {k: v for k, v in cfg.items() if k != "api_key"}
    logger.info("Persisting config: %s", safe_keys)
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as exc:
        logger.warning("Failed to write config.json: %s", exc)


def _init_state() -> None:
    """Load config into mutable state at startup."""
    global _state
    with _state_lock:
        _state = _load_config()


# ---------------------------------------------------------------------------
# Public config accessors (thread-safe reads of the in-memory state)
# ---------------------------------------------------------------------------

def get_config() -> dict:
    """Return a copy of the current in-memory config (api_key redacted)."""
    with _state_lock:
        cfg = dict(_state)
    # Never return the raw key
    return cfg


def get_provider_info() -> dict:
    """Return transport + model for the UI badge. NEVER returns the key."""
    cfg = get_config()
    return {
        "transport": cfg.get("transport", "ollama"),
        "model": cfg.get("model", ""),
        "model_overrides": cfg.get("model_overrides", {}),
    }


def get_settings() -> dict:
    """Return full settings for the Settings UI. Key is replaced with a sentinel."""
    cfg = get_config()
    return {
        "transport": cfg.get("transport", "ollama"),
        "base_url": cfg.get("base_url", "http://localhost:11434"),
        "api_key_set": bool(cfg.get("api_key")),
        "model": cfg.get("model", ""),
        "request_timeout": cfg.get("request_timeout", 180),
        "model_overrides": cfg.get("model_overrides", {}),
        "ollama_base_url": _get_ollama_base(cfg),
    }


def _get_ollama_base(cfg: dict) -> str:
    """Extract Ollama base URL from config regardless of which transport is active."""
    if cfg.get("transport") == "ollama":
        return cfg.get("base_url", "http://localhost:11434")
    # Also check if there's a stored ollama_base_url (set from local settings)
    return cfg.get("ollama_base_url", "http://localhost:11434")


def snapshot_config() -> dict:
    """Return a frozen snapshot of the current in-memory config.

    Background tasks MUST call this at task-creation time and use the
    snapshot throughout — not the live state — so a mid-flight hot-switch
    never changes which model an in-progress job uses.
    """
    with _state_lock:
        return dict(_state)


def set_provider_config(updates: dict) -> dict:
    """Merge updates into state, persist to disk, hot-switch immediately.

    If switching from ollama to cloud, fires async Ollama unload (non-blocking).
    Returns the new settings (key redacted).
    """
    global _state
    with _state_lock:
        prev_state = dict(_state)

        # Apply updates
        for key in ("transport", "base_url", "api_key", "model",
                    "request_timeout", "model_overrides", "ollama_base_url"):
            if key in updates:
                _state[key] = updates[key]

        # Persist
        _persist_config(dict(_state))

        new_transport = _state.get("transport")
        prev_transport = prev_state.get("transport")

    # Non-blocking Ollama unload after releasing the lock
    if prev_transport == "ollama" and new_transport == "openai_compatible":
        _fire_ollama_unload(prev_state)

    return get_settings()


# ---------------------------------------------------------------------------
# Ollama unload (non-blocking, best-effort)
# ---------------------------------------------------------------------------

def _fire_ollama_unload(old_state: dict) -> None:
    """Fire-and-forget: tell Ollama to drop its loaded model to free VRAM."""
    def _unload():
        try:
            base = old_state.get("base_url", "http://localhost:11434")
            model = old_state.get("model", "")
            url = base.rstrip("/") + "/api/generate"
            with httpx.Client(timeout=15.0) as client:
                resp = client.post(url, json={"model": model, "keep_alive": 0})
                if resp.status_code < 400:
                    logger.info("Ollama model unloaded successfully.")
                else:
                    logger.warning("Ollama unload returned %s (may already be unloaded)", resp.status_code)
        except Exception as exc:
            logger.info("Ollama unload skipped (Ollama not running): %s", exc)

    t = threading.Thread(target=_unload, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Custom errors — clean, no raw stack traces or leaked keys
# ---------------------------------------------------------------------------

class LLMError(Exception):
    """Base class for LLM call failures."""
    pass


class LLMTimeoutError(LLMError):
    """Request timed out."""
    pass


class LLMAuthError(LLMError):
    """Authentication failed (bad key, expired, wrong project)."""
    pass


class LLMRateLimitError(LLMError):
    """Rate-limited by the provider."""
    pass


class LLMConnectionError(LLMError):
    """Network-level failure (server down, DNS, etc.)."""
    pass


def _classify_http_error(exc: httpx.HTTPStatusError) -> LLMError:
    """Map HTTP status codes to typed errors, with safe messages."""
    code = exc.response.status_code
    if code == 401 or code == 403:
        return LLMAuthError("Authentication failed — check your API key.")
    if code == 429:
        return LLMRateLimitError("Rate limit reached — try again in a moment.")
    if 500 <= code <= 599:
        return LLMConnectionError(f"Provider returned server error {code}.")
    return LLMError(f"Provider returned HTTP {code}.")


def _classify_connection_error(exc: Exception) -> LLMError:
    """Turn raw connection errors into clean messages."""
    msg = str(exc).lower()
    if "timed out" in msg or "timeout" in msg:
        return LLMTimeoutError("Request timed out — the model may be slow or unreachable.")
    if "connect" in msg or "refused" in msg or "name" in msg:
        return LLMConnectionError("Could not reach the model server — is it running?")
    return LLMConnectionError("Network error contacting the model server.")


# ---------------------------------------------------------------------------
# Core completion dispatcher — reads from in-memory state at call time
# ---------------------------------------------------------------------------

def _resolve_model(cfg: dict, job: str) -> str:
    """Resolve the model for a given job, checking per-job overrides."""
    overrides = cfg.get("model_overrides", {})
    job_model = overrides.get(job)
    if job_model:
        return job_model
    return cfg.get("model", "claude-opus-4-7")


def complete(
    system_prompt: str,
    user_text: str,
    job: str = "reflection",
    max_tokens: int = 512,
    temperature: float = 0.6,
) -> str:
    """Send a completion request via the currently active transport.

    Uses the in-memory state. Background tasks should pass a snapshot
    from snapshot_config() as their first argument to pin the model.
    """
    cfg = get_config()
    transport = cfg.get("transport", "ollama")
    model = _resolve_model(cfg, job)
    timeout = cfg.get("request_timeout", 180) or 180

    if transport == "openai_compatible":
        return _call_openai_compatible(cfg, model, system_prompt, user_text, max_tokens, temperature, timeout)
    else:
        return _call_ollama(cfg, model, system_prompt, user_text, max_tokens, temperature, timeout)


def complete_from_snapshot(
    snapshot: dict,
    system_prompt: str,
    user_text: str,
    job: str = "reflection",
    max_tokens: int = 512,
    temperature: float = 0.6,
) -> str:
    """Like complete() but takes a config snapshot (for background tasks)."""
    transport = snapshot.get("transport", "ollama")
    model = _resolve_model(snapshot, job)
    timeout = snapshot.get("request_timeout", 180) or 180

    if transport == "openai_compatible":
        return _call_openai_compatible(snapshot, model, system_prompt, user_text, max_tokens, temperature, timeout)
    else:
        return _call_ollama(snapshot, model, system_prompt, user_text, max_tokens, temperature, timeout)


def _call_ollama(
    cfg: dict,
    model: str,
    system_prompt: str,
    user_text: str,
    max_tokens: int,
    temperature: float,
    timeout: int,
) -> str:
    """Call Ollama via POST /api/generate."""
    base_url = cfg.get("base_url", "http://localhost:11434")
    url = base_url.rstrip("/") + "/api/generate"

    payload = {
        "model": model,
        "prompt": user_text,
        "system": system_prompt,
        "stream": False,
        "keep_alive": 0,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
        },
        "think": False,
    }

    try:
        with httpx.Client(timeout=float(timeout)) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        raise _classify_http_error(exc) from exc
    except httpx.TimeoutException:
        raise LLMTimeoutError("Ollama request timed out.")
    except Exception as exc:
        raise _classify_connection_error(exc) from exc

    text = data.get("response", "").strip()
    return _strip_think_blocks(text)


def _call_openai_compatible(
    cfg: dict,
    model: str,
    system_prompt: str,
    user_text: str,
    max_tokens: int,
    temperature: float,
    timeout: int,
) -> str:
    """Call an OpenAI-compatible endpoint (Yuxor gateway) via /chat/completions.

    Handles both plain JSON and SSE streaming responses.
    """
    base_url = cfg.get("base_url", "https://api.yuxor.tech/v1")
    api_key = cfg.get("api_key", "")
    url = base_url.rstrip("/") + "/chat/completions"

    if not api_key:
        raise LLMAuthError("No API key configured — set api_key in Settings.")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }

    try:
        with httpx.Client(timeout=float(timeout)) as client:
            response = client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")

            if "text/event-stream" in content_type or "stream" in content_type:
                return _parse_sse_stream(response.iter_lines())
            else:
                data = response.json()
                try:
                    return data["choices"][0]["message"]["content"].strip()
                except (KeyError, IndexError, TypeError):
                    raise LLMError("Unexpected response format from provider.")

    except httpx.HTTPStatusError as exc:
        raise _classify_http_error(exc) from exc
    except httpx.TimeoutException:
        raise LLMTimeoutError("Cloud model request timed out.")
    except Exception as exc:
        raise _classify_connection_error(exc) from exc


def _parse_sse_stream(lines) -> str:
    """Parse SSE lines and accumulate content from chat.completion.chunk deltas."""
    chunks = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if not line.startswith("data:"):
            continue
        payload_str = line[len("data:") :].strip()
        if payload_str == "[DONE]":
            break
        try:
            import json as _json
            payload = _json.loads(payload_str)
        except Exception:
            continue
        try:
            delta = payload.get("choices", [{}])[0].get("delta", {})
            if delta.get("content"):
                chunks.append(delta["content"])
        except (KeyError, IndexError, TypeError):
            continue
    return "".join(chunks)


def _strip_think_blocks(text: str) -> str:
    """Remove <think>...</think> blocks and markdown code fences."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1:].strip()
    if text.endswith("```"):
        text = text[:-len("```")].strip()
    return text


# ---------------------------------------------------------------------------
# Model discovery
# ---------------------------------------------------------------------------

def fetch_available_models(base_url: str, api_key: str) -> list[str]:
    """Fetch model list from an OpenAI-compatible endpoint."""
    url = base_url.rstrip("/") + "/models"
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.get(url, headers=headers)
            response.raise_for_status()
            ct = response.headers.get("content-type", "")
            if "text/event-stream" in ct:
                model_ids = []
                for raw_line in response.iter_lines():
                    line = raw_line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    payload_str = line[len("data:") :].strip()
                    if payload_str == "[DONE]":
                        break
                    try:
                        import json as _json
                        payload = _json.loads(payload_str)
                        if isinstance(payload, list):
                            for m in payload:
                                if m.get("id"):
                                    model_ids.append(m["id"])
                        elif isinstance(payload, dict) and "data" in payload:
                            for m in payload.get("data", []):
                                if m.get("id"):
                                    model_ids.append(m["id"])
                    except Exception:
                        continue
                return model_ids
            else:
                data = response.json()
                if isinstance(data, list):
                    return [m.get("id", "") for m in data if m.get("id")]
                return [m.get("id", "") for m in data.get("data", []) if m.get("id")]
    except Exception:
        return []


def fetch_local_models(base_url: str) -> list[str]:
    """Fetch installed model list from Ollama via GET /api/tags."""
    url = base_url.rstrip("/") + "/api/tags"
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.get(url)
            response.raise_for_status()
            data = response.json()
            return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    except Exception:
        return []


def test_connection(cfg: dict) -> dict:
    """Run a minimal completion against the active config. Returns result info."""
    transport = cfg.get("transport", "ollama")
    if transport == "openai_compatible":
        base_url = cfg.get("base_url", "https://api.yuxor.tech/v1")
        api_key = cfg.get("api_key", "")
        if not api_key:
            return {"ok": False, "error": "No API key set."}
        try:
            model = cfg.get("model", "claude-opus-4-7")
            url = base_url.rstrip("/") + "/chat/completions"
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": "Reply with exactly one word: OK."},
                    {"role": "user", "content": "Reply with exactly one word: OK."},
                ],
                "max_tokens": 5,
                "temperature": 0.1,
                "stream": True,
            }
            with httpx.Client(timeout=30.0) as client:
                response = client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                text = _parse_sse_stream(response.iter_lines())
                text = text.strip()
                ok = text.lower().startswith("ok")
                return {
                    "ok": ok,
                    "model_responded": model,
                    "response_preview": text[:50] if ok else text[:200],
                }
        except LLMAuthError as exc:
            return {"ok": False, "error": str(exc)}
        except LLMTimeoutError as exc:
            return {"ok": False, "error": str(exc)}
        except LLMError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": f"Connection failed: {exc}"}
    else:
        base_url = cfg.get("base_url", "http://localhost:11434")
        model = cfg.get("model", "")
        url = base_url.rstrip("/") + "/api/generate"
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(url, json={
                    "model": model,
                    "prompt": "Reply with exactly one word: OK.",
                    "stream": False,
                    "options": {"num_predict": 5},
                    "think": False,
                })
                resp.raise_for_status()
                data = resp.json()
                text = data.get("response", "").strip()
                ok = text.lower().startswith("ok")
                return {
                    "ok": ok,
                    "model_responded": model,
                    "response_preview": text[:50] if ok else text[:200],
                }
        except Exception as exc:
            return {"ok": False, "error": f"Connection failed: {exc}"}


# ---------------------------------------------------------------------------
# Public API — three job functions, all delegating to complete()
# ---------------------------------------------------------------------------

def generate_draft(notes: str, recent_history: list[dict]) -> str:
    """Generate a structured reflection for a daily journal entry.

    Always uses the detailed (4-section) prompt — quick mode has been removed
    because modern LLMs produce better reflections with structure.
    """
    prompt = build_prompt(notes, recent_history)
    return complete(
        DETAILED_PROMPT,
        prompt,
        job="reflection",
        max_tokens=512,
    )


def generate_draft_from_snapshot(snapshot: dict, notes: str, recent_history: list[dict]) -> str:
    """Like generate_draft but pins to a config snapshot (for background tasks)."""
    prompt = build_prompt(notes, recent_history)
    return complete_from_snapshot(
        snapshot,
        DETAILED_PROMPT,
        prompt,
        job="reflection",
        max_tokens=512,
    )


def analyze_trends(summaries: list[dict], date_range: str = "recent entries") -> dict:
    """Analyse kept summaries and return trends, patterns and insights."""
    if not summaries:
        return {
            "trends": "No summaries available for analysis.",
            "patterns": [],
            "insights": "Start keeping summaries to unlock trend analysis.",
        }

    summaries_text = "\n\n".join(
        f"[{e['entry_date']}] {e['kept_summary']}"
        for e in summaries
    )

    user_prompt = (
        f"Date range: {date_range} ({len(summaries)} entries)\n\n"
        "The following entries are ordered newest first:\n\n"
        f"{summaries_text}\n\n"
        "Please identify:\n"
        "1. Overall mood and wellbeing trends\n"
        "2. Recurring patterns or triggers\n"
        "3. Protective factors or strategies that seem to help\n"
        "4. Positive deltas — moments of clarity, progress, relief, or wellbeing\n"
        "5. Anything notable or unusual"
    )

    raw = complete(TRENDS_SYSTEM_PROMPT, user_prompt, job="trends", max_tokens=768)

    import json as _json
    try:
        result = _json.loads(raw)
    except Exception:
        result = {
            "trends": raw,
            "patterns": [],
            "insights": "(Could not parse structured response — see trends field.)",
        }
    return result


def analyze_trends_from_snapshot(snapshot: dict, summaries: list[dict], date_range: str = "recent entries") -> dict:
    """Like analyze_trends but pins to a config snapshot."""
    if not summaries:
        return {
            "trends": "No summaries available for analysis.",
            "patterns": [],
            "insights": "Start keeping summaries to unlock trend analysis.",
        }

    summaries_text = "\n\n".join(
        f"[{e['entry_date']}] {e['kept_summary']}"
        for e in summaries
    )

    user_prompt = (
        f"Date range: {date_range} ({len(summaries)} entries)\n\n"
        "The following entries are ordered newest first:\n\n"
        f"{summaries_text}\n\n"
        "Please identify:\n"
        "1. Overall mood and wellbeing trends\n"
        "2. Recurring patterns or triggers\n"
        "3. Protective factors or strategies that seem to help\n"
        "4. Positive deltas — moments of clarity, progress, relief, or wellbeing\n"
        "5. Anything notable or unusual"
    )

    raw = complete_from_snapshot(snapshot, TRENDS_SYSTEM_PROMPT, user_prompt, job="trends", max_tokens=768)

    import json as _json
    try:
        result = _json.loads(raw)
    except Exception:
        result = {
            "trends": raw,
            "patterns": [],
            "insights": "(Could not parse structured response — see trends field.)",
        }
    return result


def generate_clinician_summary(entries: list[dict], date_range: str, weekly_checkins: list[dict] | None = None) -> str:
    """Draft a structured handout suitable for sharing with a clinician.

    Uses the V2 structured format with sections for Overall Picture,
    Notable Changes, Meltdowns/Shutdowns, Things to Bring, Timeline, and
    Suggested Questions. Includes weekly check-in data if provided.
    """
    if not entries and not weekly_checkins:
        return (
            "No entries found in the selected date range. "
            "Write your own notes or generate this before your appointment."
        )

    def entry_text(e: dict) -> str:
        text = e.get("transcription") or e.get("notes") or ""
        summary = e.get("kept_summary") or ""
        if summary and summary not in text:
            text = text + "\n[reflection]: " + summary
        # Include signals
        signals = []
        for key in ("energy", "sleep_quality", "sensory_load", "overwhelm"):
            v = e.get(key)
            if v:
                signals.append(f"{key}={v}")
        sig_line = " | ".join(signals) if signals else ""
        return f"[{e['entry_date']}]" + (f" ({sig_line})" if sig_line else "") + "\n" + text

    summaries_text = "\n\n".join(entry_text(e) for e in entries)

    # Weekly checkins summary
    checkins_text = ""
    if weekly_checkins:
        total_meltdowns = sum(c.get("meltdown_count", 0) or 0 for c in weekly_checkins)
        total_shutdowns = sum(c.get("shutdown_count", 0) or 0 for c in weekly_checkins)
        avg_spoons = 0
        spoons_count = 0
        for c in weekly_checkins:
            sp = c.get("spoons")
            if sp is not None:
                avg_spoons += sp
                spoons_count += 1
        avg_spoons = round(avg_spoons / spoons_count, 1) if spoons_count else 0
        checkins_text = (
            f"\nWeekly check-ins ({len(weekly_checkins)} weeks):\n"
            f"  Total meltdowns: {total_meltdowns}\n"
            f"  Total shutdowns: {total_shutdowns}\n"
            f"  Average spoons remaining: {avg_spoons}/12\n"
        )
        for c in weekly_checkins:
            ws = c.get("week_start", "?")
            sp = c.get("spoons", 0) or 0
            m = c.get("meltdown_count", 0) or 0
            s = c.get("shutdown_count", 0) or 0
            notes = c.get("notes") or ""
            checkins_text += (
                f"  Week of {ws}: spoons={sp}, meltdowns={m}, shutdowns={s}"
                + (f" — {notes}" if notes else "")
                + "\n"
            )

    # Notable entries (select up to 5: extremes + flagged)
    notable = []
    for e in entries:
        sigs = [e.get("energy"), e.get("overwhelm"), e.get("sensory_load")]
        is_extreme = any(v in ("high", "low") for v in sigs)
        has_summary = bool(e.get("kept_summary"))
        if is_extreme or has_summary:
            date = e.get("entry_date", "?")
            summary = e.get("kept_summary") or (e.get("notes") or "")[:120]
            notable.append(f"- {date}: {summary[:120]}...")
    notable_text = ""
    if notable:
        notable_text = "\nNotable entries:\n" + "\n".join(notable[:5])

    user_prompt = (
        f"Date range: {date_range} ({len(entries)} entries)\n\n"
        f"{checkins_text}\n"
        f"The following journal entries are ordered newest first:\n\n"
        f"{summaries_text}\n\n"
        f"{notable_text}\n\n"
        "Follow the output format in the system prompt. Be specific, grounded, and concise."
    )

    return complete(CLINICIAN_SYSTEM_PROMPT_V2, user_prompt, job="clinician", max_tokens=800)


def suggest_autostruct(entry_text: str, existing_tags: list[dict]) -> dict:
    """Suggest signal levels and tags for a journal entry.

    Uses a fast/cheap model. Falls back to empty signals/tags on parse error.
    existing_tags: flat list of {id, name, category} from the DB — used to
    bias suggestions toward tags that already exist.

    Tags are open-ended: the model can return any category, not just the
    fixed four. New categories are created automatically when applied.
    """
    # Build a hint block listing existing tags grouped by category
    if existing_tags:
        by_cat: dict[str, list[str]] = {}
        for t in existing_tags:
            cat = t.get("category", "")
            if cat:
                by_cat.setdefault(cat, []).append(t.get("name", ""))
        hint_lines = [f"  {cat}: {', '.join(sorted(set(names)))}" for cat, names in by_cat.items()]
        existing_hint = "\nExisting tags you may reuse (any category, not just these):\n" + "\n".join(hint_lines)
    else:
        existing_hint = ""

    user_prompt = (
        f"{existing_hint}\n\n"
        f"Entry:\n{entry_text}\n\n"
        "Respond with ONLY a JSON object, no markdown fences."
    )

    raw = complete(
        AUTOSTRUCT_SYSTEM_PROMPT,
        user_prompt,
        job="autostruct",
        max_tokens=512,
        temperature=0.3,
    )

    return _parse_autostruct(raw)


def suggest_autostruct_from_snapshot(snapshot: dict, entry_text: str, existing_tags: list[dict]) -> dict:
    """Like suggest_autostruct but pins to a config snapshot (for background tasks)."""
    if existing_tags:
        by_cat: dict[str, list[str]] = {}
        for t in existing_tags:
            cat = t.get("category", "")
            if cat:
                by_cat.setdefault(cat, []).append(t.get("name", ""))
        hint_lines = [f"  {cat}: {', '.join(sorted(set(names)))}" for cat, names in by_cat.items()]
        existing_hint = "\nExisting tags you may reuse (any category, not just these):\n" + "\n".join(hint_lines)
    else:
        existing_hint = ""

    user_prompt = (
        f"{existing_hint}\n\n"
        f"Entry:\n{entry_text}\n\n"
        "Respond with ONLY a JSON object, no markdown fences."
    )

    raw = complete_from_snapshot(
        snapshot,
        AUTOSTRUCT_SYSTEM_PROMPT,
        user_prompt,
        job="autostruct",
        max_tokens=512,
        temperature=0.3,
    )

    return _parse_autostruct(raw)


def _parse_autostruct(raw: str) -> dict:
    """Parse the autostruct LLM response into the standard dict shape.

    Tolerates: extra categories the model invents, missing categories,
    and garbage. Always returns the 4 known signals (validated) plus
    whatever tags the model proposed, in whatever categories it chose.
    """
    import json as _json
    try:
        result = _json.loads(raw)
    except Exception:
        return _empty_autostruct()

    if not isinstance(result, dict):
        return _empty_autostruct()

    signals = result.get("signals", {}) or {}
    raw_tags = result.get("tags", {}) or {}

    # Validate signals strictly to the 4 known fields
    cleaned_signals = {
        "energy": signals.get("energy") if signals.get("energy") in ("low", "med", "high") else None,
        "sleep_quality": signals.get("sleep_quality") if signals.get("sleep_quality") in ("low", "med", "high") else None,
        "sensory_load": signals.get("sensory_load") if signals.get("sensory_load") in ("low", "med", "high") else None,
        "overwhelm": signals.get("overwhelm") if signals.get("overwhelm") in ("low", "med", "high") else None,
    }

    # Tags: any category the model returned. Normalize: lowercase, dedupe,
    # trim, strip empties. Reject categories that aren't short words.
    cleaned_tags: dict[str, list[str]] = {}
    for cat, names in raw_tags.items():
        if not isinstance(cat, str) or not isinstance(names, list):
            continue
        cat = cat.strip().lower()
        if not cat or len(cat) > 32 or " " in cat:
            continue
        seen = set()
        out = []
        for n in names:
            if not isinstance(n, str):
                continue
            n = n.strip().lower()
            if not n or len(n) > 32 or n in seen:
                continue
            seen.add(n)
            out.append(n)
        if out:
            cleaned_tags[cat] = out

    return {"signals": cleaned_signals, "tags": cleaned_tags}


def _empty_autostruct() -> dict:
    return {
        "signals": {"energy": None, "sleep_quality": None, "sensory_load": None, "overwhelm": None},
        "tags": {},
    }


def generate_clinician_summary_from_snapshot(
    snapshot: dict,
    entries: list[dict],
    date_range: str,
    weekly_checkins: list[dict] | None = None,
) -> str:
    """Like generate_clinician_summary but pins to a config snapshot.

    Uses the structured V2 format with sections: Overall Picture, Notable Changes,
    Meltdowns/Shutdowns, Things to Bring, Timeline, Suggested Questions.
    """
    if not entries and not weekly_checkins:
        return (
            "No entries found in the selected date range. "
            "Write your own notes or generate this before your appointment."
        )

    def entry_text(e: dict) -> str:
        text = e.get("transcription") or e.get("notes") or ""
        summary = e.get("kept_summary") or ""
        if summary and summary not in text:
            text = text + "\n[reflection]: " + summary
        # Include signals
        signals = []
        for key in ("energy", "sleep_quality", "sensory_load", "overwhelm"):
            v = e.get(key)
            if v:
                signals.append(f"{key}={v}")
        sig_line = " | ".join(signals) if signals else ""
        return f"[{e['entry_date']}]" + (f" ({sig_line})" if sig_line else "") + "\n" + text

    summaries_text = "\n\n".join(entry_text(e) for e in entries)

    # Weekly checkins summary
    checkins_text = ""
    if weekly_checkins:
        total_meltdowns = sum(c.get("meltdown_count", 0) or 0 for c in weekly_checkins)
        total_shutdowns = sum(c.get("shutdown_count", 0) or 0 for c in weekly_checkins)
        avg_spoons = 0
        spoons_count = 0
        for c in weekly_checkins:
            sp = c.get("spoons")
            if sp is not None:
                avg_spoons += sp
                spoons_count += 1
        avg_spoons = round(avg_spoons / spoons_count, 1) if spoons_count else 0
        checkins_text = (
            f"\nWeekly check-ins ({len(weekly_checkins)} weeks):\n"
            f"  Total meltdowns: {total_meltdowns}\n"
            f"  Total shutdowns: {total_shutdowns}\n"
            f"  Average spoons remaining: {avg_spoons}/12\n"
        )
        for c in weekly_checkins:
            ws = c.get("week_start", "?")
            sp = c.get("spoons", 0) or 0
            m = c.get("meltdown_count", 0) or 0
            s = c.get("shutdown_count", 0) or 0
            notes = c.get("notes") or ""
            checkins_text += (
                f"  Week of {ws}: spoons={sp}, meltdowns={m}, shutdowns={s}"
                + (f" — {notes}" if notes else "")
                + "\n"
            )

    # Notable entries (select up to 5: extremes + flagged)
    notable = []
    for e in entries:
        sigs = [e.get("energy"), e.get("overwhelm"), e.get("sensory_load")]
        is_extreme = any(v in ("high", "low") for v in sigs)
        has_summary = bool(e.get("kept_summary"))
        if is_extreme or has_summary:
            date = e.get("entry_date", "?")
            summary = e.get("kept_summary") or (e.get("notes") or "")[:120]
            notable.append(f"- {date}: {summary[:120]}...")
    notable_text = ""
    if notable:
        notable_text = "\nNotable entries:\n" + "\n".join(notable[:5])

    user_prompt = (
        f"Date range: {date_range} ({len(entries)} entries)\n\n"
        f"{checkins_text}\n"
        f"The following journal entries are ordered newest first:\n\n"
        f"{summaries_text}\n\n"
        f"{notable_text}\n\n"
        "Follow the output format in the system prompt. Be specific, grounded, and concise."
    )

    return complete_from_snapshot(
        snapshot, CLINICIAN_SYSTEM_PROMPT_V2, user_prompt, job="clinician", max_tokens=800
    )


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

DETAILED_PROMPT = (
    "You are a reflection aid for a neurodivergent adult. "
    "Given their daily notes and a short history of recent days, "
    "write a structured draft reflection with these four sections:\n"
    "1. What happened - 1-2 sentences describing what the notes describe.\n"
    "2. How it landed - 1-2 sentences on the emotional impact or tone.\n"
    "3. Possible pattern - If you notice something that connects across recent days, "
    "raise it as a question, not a conclusion. If nothing connects, just say so briefly.\n"
    "4. Question to sit with - One short question worth holding, not answering.\n\n"
    "This is a draft for them to check, not a verdict. "
    "No diagnosis. Do not reassure or cheerlead for its own sake. "
    "Plain, grounded language."
)

TRENDS_SYSTEM_PROMPT = (
    "You are a thoughtful, non-judgmental observer reviewing a person's journal entries. "
    "Your goal is to identify themes and patterns, not to diagnose or pathologise. "
    "Be specific where possible. Use phrases like 'there may be a tendency toward…' "
    "rather than absolute statements. "
    "When you identify a pattern, also note any exceptions or mitigating factors. "
    "Flag protective factors and strategies that seem to help, not just difficulties. "
    "Include positive deltas — moments of clarity, progress, relief, or wellbeing — alongside any challenges."
)

CLINICIAN_SYSTEM_PROMPT_V2 = (
    "You are a calm, objective writing assistant. Your only job is to synthesise "
    "a patient's journal entries into a structured handout for their clinician. "
    "Write in plain English. Do not pathologise normal variation. "
    "Do not speculate beyond what is in the entries. "
    "Do not use motivational language or reassurance. "
    "Do not diagnose. Do not offer therapeutic advice. "
    "The patient will edit this before sharing — it is a draft.\n\n"
    "Output format — produce ALL of the following sections, separated by blank lines:\n\n"
    "## Overall Picture\n"
    "2-3 sentences on how the patient has been overall in this period.\n\n"
    "## Notable Changes\n"
    "Any shifts from the patient's usual pattern — better or worse.\n\n"
    "## Meltdowns and Shutdowns\n"
    "Frequency this period, any notable context.\n\n"
    "## Things to Bring to the Appointment\n"
    "1-3 bullet points of things the patient might want to flag with their clinician.\n\n"
    "## Timeline of Notable Entries\n"
    "A brief timeline (2-5 entries) that captures key moments or patterns.\n\n"
    "## Suggested Questions\n"
    "2-4 questions the patient might want to ask at their appointment, phrased neutrally."
)

AUTOSTRUCT_SYSTEM_PROMPT = (
    "You are a quiet, non-judgmental journal assistant. Given a person's written "
    "journal entry, suggest signal levels and tags. Respond ONLY with a valid JSON "
    "object — no explanation, no preamble.\n\n"
    "Signals: energy, sleep_quality, sensory_load, overwhelm — each is low, med, or high.\n"
    "  - energy: overall alertness and capacity. low = drained/fatigued. med = moderate. high = energised.\n"
    "  - sleep_quality: how restorative sleep was. low = poor/restless. med = adequate. high = restorative.\n"
    "  - sensory_load: sensory input burden today. low = calm environment. med = noticeable input. high = overwhelming input.\n"
    "  - overwhelm: emotional/neurocess overwhelm. low = calm, manageable. med = some strain. high = near-capacity or past it.\n\n"
    "Tags: categorise anything relevant from the entry. You are NOT limited to a fixed set of categories — "
    "use whatever categories make sense. Common ones include: people, places, activities, triggers, moods, "
    "health, work, relationships, self-care, finances. But invent new categories when the entry calls for it.\n"
    "Each tag should be lowercase, concise (1-2 words max). Reuse existing tags when they fit. "
    "If no tag fits a category, omit that category entirely (don't include it with an empty array).\n\n"
    "IMPORTANT:\n"
    "  - If you cannot infer a signal value from the entry, use null for that field — do not guess.\n"
    "  - Tags should be based on what is actually written, not assumed.\n"
    "  - Output ONLY JSON in this exact shape, with no markdown fences:\n\n"
    '  {"signals": {"energy": "low|med|high|null", "sleep_quality": "...", "sensory_load": "...", "overwhelm": "..."}, '
    '"tags": {"moods": ["stressed"], "work": ["deadlines", "meeting"], "people": ["sarah"], "triggers": ["noise"]}}'
)


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _build_history_block(recent_history: list[dict]) -> str:
    if recent_history:
        history_lines = "\n".join(
            f"- {entry['entry_date']}: {entry['kept_summary']}"
            for entry in recent_history
        )
        return f"\nRecent days:\n{history_lines}\n"
    return "\n(No prior entries yet.)\n"


def build_prompt(notes: str, recent_history: list[dict]) -> str:
    """Build the user-facing part of the prompt (history + notes).

    The system prompt (DETAILED_PROMPT) is passed separately as the
    system message — do NOT include it here to avoid duplication.
    """
    history_block = _build_history_block(recent_history)
    return (
        f"{history_block}"
        f"Today's notes:\n{notes}\n\n"
    )


# ---------------------------------------------------------------------------
# Embeddings — test and upsert
# ---------------------------------------------------------------------------

def check_embeddings_support(base_url: str, api_key: str) -> bool:
    """Test whether the provider exposes a working POST /embeddings endpoint."""
    url = base_url.rstrip("/") + "/embeddings"
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.post(
                url,
                json={"model": "claude-opus-4-7", "input": "test"},
                headers=headers,
            )
            if response.status_code == 404:
                return False
            response.raise_for_status()
            return True
    except Exception:
        return False


def compute_embedding(text: str, base_url: str, api_key: str, model: str) -> list[float] | None:
    """Compute an embedding for a text string via POST /embeddings."""
    url = base_url.rstrip("/") + "/embeddings"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                url,
                json={"model": model, "input": text[:8192]},  # cap input length
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
            # Support both OpenAI-style {data:[{embedding:[...]}]} and Yuxor responses
            items = data.get("data", data)
            if isinstance(items, list) and len(items) > 0:
                emb = items[0].get("embedding") if isinstance(items[0], dict) else items[0]
                if isinstance(emb, list):
                    return emb
            return None
    except Exception:
        return None


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    import math
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def rank_by_embedding(query: str, entries: list[dict], base_url: str, api_key: str, model: str) -> list[dict]:
    """Re-rank entries by semantic similarity to the query.

    Returns entries sorted by cosine similarity (descending). If embedding
    computation fails, returns entries in their original order.
    """
    query_emb = compute_embedding(query, base_url, api_key, model)
    if query_emb is None:
        return entries  # fallback to original order

    scored = []
    for e in entries:
        emb = e.get("_embedding")
        if emb:
            score = cosine_similarity(query_emb, emb)
            scored.append((score, e))
        else:
            scored.append((0.0, e))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in scored]


# ---------------------------------------------------------------------------
# Ask-journal Q&A (Phase C)
# ---------------------------------------------------------------------------

QA_SYSTEM_PROMPT = (
    "You are a helpful, non-judgmental journal assistant. "
    "Answer the user's question based ONLY on the journal entries provided below. "
    "If the entries do not contain enough information to answer, say so plainly. "
    "Do not make up patterns or extrapolate beyond what is in the entries. "
    "When you draw on a specific entry, cite it by its date. "
    "Format your answer clearly. If you are uncertain, say so."
)

TOKEN_ESTIMATE = 4  # rough characters-per-token estimate for context capping


def build_qa_context(entries: list[dict], max_tokens: int = 12000) -> tuple[str, list[str]]:
    """Build a context string from entries, capped to max_tokens.

    Prefers kept_summary + structured fields over raw notes.
    Returns (context_string, list of cited dates).
    """
    context_parts = []
    cited_dates = []
    used_tokens = 0

    for e in entries:
        date = e.get("entry_date", "?")
        cited_dates.append(date)

        # Prefer kept_summary, fall back to notes
        summary = e.get("kept_summary") or e.get("notes") or ""
        # Signals as a compact line
        signals = []
        for key in ("energy", "sleep_quality", "sensory_load", "overwhelm"):
            v = e.get(key)
            if v:
                signals.append(f"{key}={v}")
        signal_line = f"[{date}] " + " | ".join(signals) if signals else f"[{date}]"

        # Rough token count
        block_text = signal_line + "\n" + summary
        est_tokens = len(block_text) // TOKEN_ESTIMATE

        if used_tokens + est_tokens > max_tokens:
            break
        used_tokens += est_tokens
        context_parts.append(f"---\n{signal_line}\n{summary}\n---")

    return "\n\n".join(context_parts), cited_dates[: len(context_parts)]


def answer_journal_question(question: str, entries: list[dict], date_range: str) -> dict:
    """Answer a natural-language question about journal entries.

    Returns {answer, cited_dates, method, token_count}.
    """
    context, cited_dates = build_qa_context(entries, max_tokens=10000)

    user_prompt = (
        f"Date range of available entries: {date_range}\n\n"
        f"Journal entries:\n{context}\n\n"
        f"Question: {question}\n\n"
        "Answer based on the entries above. Cite dates when you draw on specific entries."
    )

    # Estimate tokens for the prompt
    token_count = len(user_prompt) // TOKEN_ESTIMATE

    answer = complete(
        QA_SYSTEM_PROMPT,
        user_prompt,
        job="ask_journal",
        max_tokens=768,
        temperature=0.4,
    )

    return {
        "answer": answer,
        "cited_dates": cited_dates,
        "method": "keyword",
        "token_count": token_count,
    }


def answer_journal_question_from_snapshot(snapshot: dict, question: str, entries: list[dict], date_range: str) -> dict:
    """Like answer_journal_question but pins to a config snapshot."""
    context, cited_dates = build_qa_context(entries, max_tokens=10000)

    user_prompt = (
        f"Date range of available entries: {date_range}\n\n"
        f"Journal entries:\n{context}\n\n"
        f"Question: {question}\n\n"
        "Answer based on the entries above. Cite dates when you draw on specific entries."
    )

    token_count = len(user_prompt) // TOKEN_ESTIMATE

    answer = complete_from_snapshot(
        snapshot,
        QA_SYSTEM_PROMPT,
        user_prompt,
        job="ask_journal",
        max_tokens=768,
        temperature=0.4,
    )

    return {
        "answer": answer,
        "cited_dates": cited_dates,
        "method": "keyword",
        "token_count": token_count,
    }


CORRELATION_NARRATION_PROMPT = (
    "You are a thoughtful, non-judgmental analyst reviewing statistics from a person's "
    "journal entries. You are given COMPUTED COUNTS ONLY — do not invent or assume any pattern. "
    "For each finding, first state what the numbers actually show, then phrase it as a "
    "question or observation rather than a conclusion.\n\n"
    "RULES:\n"
    "  - State actual counts. E.g. '6 out of 8 entries tagged \"work\" had low energy.'\n"
    "  - Compare to baseline: if tagged entries skew differently from the baseline, note it.\n"
    "  - If a tag has fewer than 3 entries, note the data is thin.\n"
    "  - For lead/lag findings: state how many pairs were checked.\n"
    "  - Never say 'this proves', 'this causes', or 'you always'. Say 'there may be a tendency'.\n"
    "  - If numbers are ambiguous or flat, just say so.\n"
    "  - No diagnosis. No reassurance. Plain language.\n"
    "  - Keep it concise — one paragraph per major finding."
)


def narrate_correlations(correlation_data: dict) -> str:
    """Narrate computed correlation statistics in plain language.

    The model receives the raw counts only — it does not infer, it narrates.
    """
    import json as _json
    user_prompt = (
        "Date range: " + correlation_data.get("date_range", "recent entries") + "\n\n"
        "=== TAG × SIGNAL STATISTICS ===\n"
        "Baseline (all entries with signals, " + str(correlation_data.get("total_entries_with_signals", 0)) + " total):\n"
    )

    baseline = correlation_data.get("baseline", {})
    for sig, dist in baseline.items():
        total = sum(dist.values())
        if total == 0:
            continue
        user_prompt += (
            f"  {sig}: "
            + ", ".join(f"{lvl}={v}({round(v/max(total,1)*100)}%)" for lvl, v in dist.items())
            + "\n"
        )

    user_prompt += "\nPer-tag breakdown (tagged entries only):\n"
    for ts in correlation_data.get("tag_signal_stats", []):
        name = ts.get("tag_name", "?")
        cat = ts.get("tag_category", "?")
        total = ts.get("total", 0)
        user_prompt += f"\n[{cat}] {name} — {total} entries\n"
        for sig in ("energy", "sleep_quality", "sensory_load", "overwhelm"):
            dist = ts.get(sig, {})
            dist_str = ", ".join(f"{lvl}={v}" for lvl, v in dist.items() if v > 0 or lvl != "null")
            if dist_str:
                user_prompt += f"  {sig}: {dist_str}\n"

    user_prompt += "\n=== LEAD/LAG (consecutive-day pairs) ===\n"
    ll = correlation_data.get("lead_lag", {})
    ps = ll.get("poor_sleep_preceding_overwhelm", {})
    if ps.get("poor_sleep_days", 0) > 0:
        user_prompt += (
            f"\nPoor sleep → next-day high overwhelm:\n"
            f"  Days with low sleep: {ps['poor_sleep_days']}\n"
            f"  Followed by high overwhelm: {ps['followed_by_high_overwhelm']} ({ps.get('pct', 'N/A')}%)\n"
        )
    else:
        user_prompt += "\nPoor sleep → next-day high overwhelm: insufficient data.\n"

    he = ll.get("high_energy_preceding_overwhelm", {})
    if he.get("high_energy_days", 0) > 0:
        user_prompt += (
            f"\nHigh energy → next-day high overwhelm:\n"
            f"  High-energy days: {he['high_energy_days']}\n"
            f"  Followed by high overwhelm: {he['followed_by_high_overwhelm']} ({he.get('pct', 'N/A')}%)\n"
        )
    else:
        user_prompt += "\nHigh energy → next-day high overwhelm: insufficient data.\n"

    user_prompt += (
        "\n=== YOUR TASK ===\n"
        "Write a brief, plain-language narrative of what these numbers show. "
        "Follow the rules above. Be specific about counts. "
        "Raise findings as questions or observations, not conclusions."
    )

    return complete(
        CORRELATION_NARRATION_PROMPT,
        user_prompt,
        job="correlations",
        max_tokens=512,
        temperature=0.4,
    )


def narrate_correlations_from_snapshot(snapshot: dict, correlation_data: dict) -> str:
    """Like narrate_correlations but pins to a config snapshot."""
    import json as _json
    user_prompt = (
        "Date range: " + correlation_data.get("date_range", "recent entries") + "\n\n"
        "=== TAG × SIGNAL STATISTICS ===\n"
        "Baseline (all entries with signals, " + str(correlation_data.get("total_entries_with_signals", 0)) + " total):\n"
    )

    baseline = correlation_data.get("baseline", {})
    for sig, dist in baseline.items():
        total = sum(dist.values())
        if total == 0:
            continue
        user_prompt += (
            f"  {sig}: "
            + ", ".join(f"{lvl}={v}({round(v/max(total,1)*100)}%)" for lvl, v in dist.items())
            + "\n"
        )

    user_prompt += "\nPer-tag breakdown (tagged entries only):\n"
    for ts in correlation_data.get("tag_signal_stats", []):
        name = ts.get("tag_name", "?")
        cat = ts.get("tag_category", "?")
        total = ts.get("total", 0)
        user_prompt += f"\n[{cat}] {name} — {total} entries\n"
        for sig in ("energy", "sleep_quality", "sensory_load", "overwhelm"):
            dist = ts.get(sig, {})
            dist_str = ", ".join(f"{lvl}={v}" for lvl, v in dist.items() if v > 0 or lvl != "null")
            if dist_str:
                user_prompt += f"  {sig}: {dist_str}\n"

    user_prompt += "\n=== LEAD/LAG (consecutive-day pairs) ===\n"
    ll = correlation_data.get("lead_lag", {})
    ps = ll.get("poor_sleep_preceding_overwhelm", {})
    if ps.get("poor_sleep_days", 0) > 0:
        user_prompt += (
            f"\nPoor sleep → next-day high overwhelm:\n"
            f"  Days with low sleep: {ps['poor_sleep_days']}\n"
            f"  Followed by high overwhelm: {ps['followed_by_high_overwhelm']} ({ps.get('pct', 'N/A')}%)\n"
        )
    else:
        user_prompt += "\nPoor sleep → next-day high overwhelm: insufficient data.\n"

    he = ll.get("high_energy_preceding_overwhelm", {})
    if he.get("high_energy_days", 0) > 0:
        user_prompt += (
            f"\nHigh energy → next-day high overwhelm:\n"
            f"  High-energy days: {he['high_energy_days']}\n"
            f"  Followed by high overwhelm: {he['followed_by_high_overwhelm']} ({he.get('pct', 'N/A')}%)\n"
        )
    else:
        user_prompt += "\nHigh energy → next-day high overwhelm: insufficient data.\n"

    user_prompt += (
        "\n=== YOUR TASK ===\n"
        "Write a brief, plain-language narrative of what these numbers show. "
        "Follow the rules above. Be specific about counts. "
        "Raise findings as questions or observations, not conclusions."
    )

    return complete_from_snapshot(
        snapshot,
        CORRELATION_NARRATION_PROMPT,
        user_prompt,
        job="correlations",
        max_tokens=512,
        temperature=0.4,
    )


# ---------------------------------------------------------------------------
# Initialise state at module import time
# ---------------------------------------------------------------------------
_init_state()
