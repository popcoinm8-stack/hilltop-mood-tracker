"""
faster-whisper singleton: loads on first use, idles out after ~2 min.
VAD on. float16 on RTX 50-series / Blackwell; fallback chain if needed.
Audio always stays in memory — never written to disk.
"""
import gc
import io
import struct
import threading
import time
from typing import Optional

ACTIVE_COMPUTE: str = "float16"  # set at load time, exposed for debugging/logging


def _decode_webm_to_wav(audio_bytes: bytes) -> bytes:
    """Decode webm/opus to 16 kHz mono PCM WAV in memory. No disk writes."""
    import av
    import numpy as np

    container = av.open(io.BytesIO(audio_bytes))
    audio_stream = next((s for s in container.streams if s.type == "audio"), None)
    if audio_stream is None:
        raise ValueError("No audio stream in blob")

    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    chunks: list[bytes] = []
    for packet in container.demux(audio_stream):
        for frame in packet.decode():
            resampled = resampler.resample(frame)
            for f in resampled:
                data: np.ndarray = f.to_ndarray()
                chunks.append(data.tobytes())

    container.close()
    pcm = b"".join(chunks)

    # Build a WAV header around the PCM
    wav = io.BytesIO()
    wav_len = len(pcm)
    wav.write(b"RIFF")
    wav.write(struct.pack("<I", 36 + wav_len))   # file size - 8
    wav.write(b"WAVE")
    wav.write(b"fmt ")
    wav.write(struct.pack("<I", 16))              # fmt chunk size
    wav.write(struct.pack("<HHIIHH", 1, 1, 16000, 32000, 2, 16))  # PCM format
    wav.write(b"data")
    wav.write(struct.pack("<I", wav_len))
    wav.write(pcm)
    wav.seek(0)
    return wav.read()


# --- Model singleton with idle-unload ---

_model: Optional["WhisperModel"] = None
_cuda_probe_done: bool = False
_lock = threading.Lock()
_idle_timer: Optional[threading.Timer] = None
_IDLE_SECONDS = 120  # free model 2 min after last use


def shutdown() -> None:
    """Force-unload the model and cancel the idle timer. Call on app shutdown."""
    global _model, _idle_timer, _cuda_probe_done
    with _lock:
        if _idle_timer is not None:
            _idle_timer.cancel()
            _idle_timer = None
        if _model is not None:
            del _model
            _model = None
            _cuda_probe_done = False
            import gc
            gc.collect()


def _unload_model() -> None:
    global _model, _idle_timer, _cuda_probe_done
    with _lock:
        if _model is not None:
            del _model
            _model = None
            gc.collect()
            _idle_timer = None
            _cuda_probe_done = False


def _reset_idle_timer() -> None:
    global _idle_timer
    with _lock:
        if _idle_timer is not None:
            _idle_timer.cancel()
        _idle_timer = threading.Timer(_IDLE_SECONDS, _unload_model)
        _idle_timer.daemon = True
        _idle_timer.start()


def _probe_cuda(model) -> bool:
    """Run a tiny transcribe through the encoder to check CUDA actually works.
    The cublas DLL error only fires when the encoder runs, not at model load."""
    import struct as _s
    _tiny = io.BytesIO()
    _tiny.write(b"RIFF")
    _tiny.write(_s.pack("<I", 36 + 16000))
    _tiny.write(b"WAVEfmt ")
    _tiny.write(_s.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16))
    _tiny.write(b"data")
    _tiny.write(_s.pack("<I", 16000))
    _tiny.write(b"\x00" * 16000)
    _tiny.seek(0)
    try:
        # vad_filter=False forces encoder to run even on silence
        list(model.transcribe(_tiny, language="en", vad_filter=False))
        return True
    except RuntimeError:
        return False


def _load_model(device: str = "auto") -> "WhisperModel":
    """
    Load model. If device is 'auto', tries CUDA then CPU.
    Probes CUDA by actually running inference (cublas errors only fire at that point).
    """
    global ACTIVE_COMPUTE, _cuda_probe_done
    from faster_whisper import WhisperModel

    if device == "cpu":
        _cuda_probe_done = True  # skip probe on CPU
        model = WhisperModel(
            "large-v3-turbo", device="cpu", compute_type="float32", local_files_only=False,
        )
        ACTIVE_COMPUTE = "cpu/float32"
        return model

    # Try CUDA configs, then CPU
    configs = [
        ("cuda", "float16"),
        ("cuda", "int8_float16"),
        ("cpu",  "float32"),
    ]
    for dev, ct in configs:
        try:
            model = WhisperModel(
                "large-v3-turbo", device=dev, compute_type=ct, local_files_only=False,
            )
        except Exception:
            continue

        # Model loaded — but does CUDA actually work?
        if dev == "cuda" and not _cuda_probe_done:
            if not _probe_cuda(model):
                del model; gc.collect()
                continue  # try next config
            _cuda_probe_done = True

        ACTIVE_COMPUTE = f"{dev}/{ct}"
        return model

    raise RuntimeError("faster-whisper: all device configs failed, including CPU")


def transcribe(audio_bytes: bytes) -> str:
    """
    Transcribe in-memory webm/opus audio.
    Keeps model warm for 2 min for back-to-back dictation; frees when idle.
    If CUDA fails at inference time, reloads on CPU and retries.
    """
    global _model
    _reset_idle_timer()

    with _lock:
        if _model is None:
            _model = _load_model()

    wav_bytes = _decode_webm_to_wav(audio_bytes)
    try:
        # Consume the generator inside try so RuntimeError is caught (error fires on iteration, not call)
        segments, _ = _model.transcribe(
            io.BytesIO(wav_bytes),
            language="en",
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
        )
        # Force iteration inside try/except
        return "".join(seg.text for seg in segments).strip()
    except RuntimeError:
        # CUDA DLL crash at inference time — fall back to CPU and retry
        with _lock:
            if _model is not None:
                del _model; gc.collect()
            _model = _load_model(device="cpu")
        segments, _ = _model.transcribe(
            io.BytesIO(wav_bytes),
            language="en",
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
        )
        return "".join(seg.text for seg in segments).strip()
