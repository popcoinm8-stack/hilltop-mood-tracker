"""Call the local Ollama model and return a draft reflection."""
import httpx

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "qwen3.6:35b-a3b"

LOOKBACK_DAYS = 180


def _call_llm(system_prompt: str, user_prompt: str, max_tokens: int = 512, temperature: float = 0.6) -> str:
    """Shared LLM call via Ollama. Returns raw text with think-blocks and fences stripped."""
    payload = {
        "model": MODEL,
        "prompt": user_prompt,
        "system": system_prompt,
        "stream": False,
        "keep_alive": 0,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
        },
        "think": False,
    }

    with httpx.Client(timeout=180.0) as client:
        response = client.post(OLLAMA_URL, json=payload)
        response.raise_for_status()
        data = response.json()

    text = data.get("response", "").strip()

    # Strip any think-block the model might emit
    if text.startswith("<think>"):
        end = text.find("</think>")
        if end != -1:
            text = text[end + 8:].strip()

    # Strip markdown code fences
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1:].strip()
    if text.endswith("```"):
        text = text[:-len("```")].strip()

    return text


def generate_clinician_summary(entries: list[dict], date_range: str) -> str:
    """
    Draft a plain-language paragraph suitable for sharing with a clinician.
    Feeds the LLM the kept summaries and entry metadata from the window.
    Always a draft — caller displays it for user review before any export.
    """
    if not entries:
        return (
            "No kept summaries found in the selected date range. "
            "Write your own notes or generate this before your appointment."
        )

    total = len(entries)
    with_summary = sum(1 for e in entries if e.get("kept_summary"))
    quick_count = sum(1 for e in entries if e.get("mode") == "quick")
    detailed_count = sum(1 for e in entries if e.get("mode") == "detailed")

    # Build entry text: prefer transcription, fall back to notes, always include kept_summary
    def entry_text(e: dict) -> str:
        text = e.get("transcription") or e.get("notes") or ""
        summary = e.get("kept_summary") or ""
        if summary and summary not in text:
            text = text + "\n[reflection]: " + summary
        return f"[{e['entry_date']}] {text}"

    summaries_text = "\n\n".join(
        entry_text(e) for e in entries
    )

    system_prompt = (
        "You are a calm, objective writing assistant. Your only job is to synthesise "
        "a patient's journal entries into a short, readable paragraph for their clinician. "
        "Write in plain English. Do not pathologise normal variation. "
        "Do not speculate beyond what is in the entries. "
        "Do not use motivational language or reassurance. "
        "The entries contain raw dictation (verbatim thoughts) and an AI reflection. "
        "Treat both as a single coherent entry. "
        "Structure the paragraph around: how the patient has been feeling overall, "
        "any notable patterns or changes, and anything the patient might want their "
        "clinician to be aware of. Keep it concise — one or two short paragraphs. "
        "The patient will edit this before sharing, so write it as a draft."
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

    return _call_llm(system_prompt, user_prompt, max_tokens=600)


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

    system_prompt = (
        "You are a thoughtful, non-judgmental observer reviewing a person's journal entries. "
        "Your goal is to identify themes and patterns, not to diagnose or pathologise. "
        "Be specific where possible. Use phrases like 'there may be a tendency toward…' "
        "rather than absolute statements. "
        "When you identify a pattern, also note any exceptions or mitigating factors. "
        "Flag protective factors and strategies that seem to help, not just difficulties. "
        "Include positive deltas — moments of clarity, progress, relief, or wellbeing — alongside any challenges."
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

    raw = _call_llm(system_prompt, user_prompt, max_tokens=768)
    import json

    try:
        result = json.loads(raw)
    except Exception:
        # Fallback: return raw text in trends field
        result = {
            "trends": raw,
            "patterns": [],
            "insights": "(Could not parse structured response — see trends field.)",
        }

    return result

QUICK_PROMPT = (
    "You are a reflection aid for a neurodivergent adult. "
    "Given their daily notes and a short history of recent days, "
    "write a brief observation of 3-4 sentences. "
    "It is a draft for them to check, not a verdict. "
    "No diagnosis. Do not reassure or cheerlead for its own sake. "
    "Plain, grounded language. "
    "If you notice a possible pattern - including across recent days - "
    "raise it as a question, not a conclusion."
)

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


def _build_history_block(recent_history: list[dict]) -> str:
    if recent_history:
        history_lines = "\n".join(
            f"- {entry['entry_date']}: {entry['kept_summary']}"
            for entry in recent_history
        )
        return f"\nRecent days:\n{history_lines}\n"
    return "\n(No prior entries yet.)\n"


def build_prompt(notes: str, recent_history: list[dict], mode: str = "quick") -> str:
    system = QUICK_PROMPT if mode == "quick" else DETAILED_PROMPT
    instruction = (
        "Write a 3-4 sentence draft observation."
        if mode == "quick"
        else "Write the structured draft reflection."
    )
    history_block = _build_history_block(recent_history)
    return (
        f"{system}"
        f"{history_block}"
        f"Today's notes:\n{notes}\n\n"
        f"{instruction}"
    )


def generate_draft(notes: str, recent_history: list[dict], mode: str = "quick") -> str:
    prompt = build_prompt(notes, recent_history, mode)
    max_tokens = 256 if mode == "quick" else 512

    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "keep_alive": 0,  # unload immediately after response; future chat mode will manage per-session
        "options": {
            "temperature": 0.6,
            "num_predict": max_tokens,
        },
        "think": False,
    }

    with httpx.Client(timeout=180.0) as client:
        response = client.post(OLLAMA_URL, json=payload)
        response.raise_for_status()
        data = response.json()

    text = data.get("response", "").strip()

    # Strip any think-block the model might emit
    if text.startswith("<think>"):
        end = text.find("</think>")
        if end != -1:
            text = text[end + 8 :].strip()

    return text