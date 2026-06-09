"""Kokoro TTS: loads once on first use, stays resident. CPU-only (keeps GPU free for Whisper/LLM)."""

from pathlib import Path
from typing import Optional

_KOKORO_DIR = Path(__file__).resolve().parent.parent / "kokoro"
MODEL_PATH = str(_KOKORO_DIR / "kokoro-v1.0.fp16.onnx")
VOICES_PATH = str(_KOKORO_DIR / "voices-v1.0.bin")

VOICE = "bf_emma"  # British English female — change here to switch voice

_kokoro: Optional["Kokoro"] = None


def _get_kokoro():
    global _kokoro
    if _kokoro is None:
        from kokoro_onnx import Kokoro
        _kokoro = Kokoro(MODEL_PATH, VOICES_PATH)
    return _kokoro


def speak(text: str) -> tuple[list[float], int]:
    """Return (samples, sample_rate) for the given text. Never writes to disk."""
    k = _get_kokoro()
    samples, sr = k.create(text, voice=VOICE, speed=1.0)
    return samples.tolist(), sr