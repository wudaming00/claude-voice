"""Speech-to-text using faster-whisper.

The model is loaded lazily on first use and cached in-process — loading
large-v3 onto the GPU takes ~10 seconds, so we never want to pay that
cost more than once. Concurrent callers share the same instance;
faster-whisper's transcribe() holds the GIL for its synchronous parts,
so we offload to a thread to keep FastAPI responsive.

Why faster-whisper (and not plain Whisper)
------------------------------------------
faster-whisper is a ctranslate2 reimplementation of the Whisper decoder.
It's roughly 4× faster than the reference PyTorch implementation at the
same quality, uses less VRAM (fits on a 4 GB card at large-v3 with
int8_float16), and supports CPU inference with int8 quantisation that's
fast enough for short voice turns on a modern laptop.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import threading
import time
from typing import Optional

from faster_whisper import WhisperModel

log = logging.getLogger("claude_voice.stt")

_MODEL: Optional[WhisperModel] = None
_MODEL_LOCK = threading.Lock()
_LAST_USED_TS: float = 0.0
_IDLE_UNLOAD_SEC = float(os.environ.get("WHISPER_IDLE_UNLOAD_SEC", "300"))  # 5 min default
_UNLOAD_THREAD_STARTED = False


def _start_idle_unload_thread() -> None:
    """Background thread that unloads the model + clears CUDA cache when idle.

    Why: large-v3 sits on ~10GB VRAM permanently after first request, blocking
    other GPU workloads on the same machine. Five minutes of inactivity is a
    reasonable signal that no one is actively using voice-mode right now —
    pay the ~10s reload cost when they come back.

    Lower WHISPER_IDLE_UNLOAD_SEC (or set to 0 to disable) for chatty users.
    """
    global _UNLOAD_THREAD_STARTED
    if _UNLOAD_THREAD_STARTED or _IDLE_UNLOAD_SEC <= 0:
        return
    _UNLOAD_THREAD_STARTED = True

    def _loop():
        global _MODEL, _LAST_USED_TS
        while True:
            time.sleep(60)  # check minute-resolution; nothing time-critical
            with _MODEL_LOCK:
                if _MODEL is None or _LAST_USED_TS == 0:
                    continue
                idle = time.time() - _LAST_USED_TS
                if idle < _IDLE_UNLOAD_SEC:
                    continue
                log.info("Whisper idle for %.0fs (>= %.0fs) — unloading to free VRAM",
                         idle, _IDLE_UNLOAD_SEC)
                _MODEL = None
            # Clear CUDA cache outside the lock so we don't block transcribe calls
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                    torch.cuda.ipc_collect()
            except Exception:
                pass
    t = threading.Thread(target=_loop, daemon=True, name="whisper-idle-unload")
    t.start()

# Prime Whisper with common technical vocabulary.
#
# ``initial_prompt`` is used as previous-context for decoding, which biases
# the language model toward these tokens. It's the difference between "FastAPI"
# transcribing as "fast API" vs the correct spelling, and between "Claude"
# becoming "clawed" or "claud" in noisy audio.
#
# The prompt particularly helps with **code-switching**: Chinese/Japanese/
# Korean speech sprinkled with English tech terms, which is very common for
# developers and Whisper otherwise handles unevenly.
#
# Users can override via the ``WHISPER_INITIAL_PROMPT`` env var if they have
# a domain-specific jargon set (medical, legal, game dev, etc.).
DEFAULT_INITIAL_PROMPT = os.environ.get(
    "WHISPER_INITIAL_PROMPT",
    "A technical conversation, possibly mixing English programming terms "
    "including Claude, Python, TypeScript, JavaScript, Rust, Go, GitHub, "
    "FastAPI, WebSocket, Docker, API, PWA.",
)


class WhisperDisabledError(RuntimeError):
    """Raised when local STT is disabled by config — front-end should fall back
    to OS-level speech input (iOS keyboard mic, Android Gboard voice typing,
    macOS Dictation) which produces text directly into the chat input."""


def _get_model() -> WhisperModel:
    """Return the shared WhisperModel, loading it on first call (or after idle-unload).

    Loading is deliberately done on the first real request rather than at
    import time so that:
      - ``import stt`` stays cheap (useful during setup/tests)
      - Startup of the FastAPI server isn't blocked by a multi-second load
      - If the user never speaks (text-mode only), they never pay the
        load cost at all.

    After WHISPER_IDLE_UNLOAD_SEC of inactivity the background thread sets
    _MODEL = None; the next call here re-loads. ~10s reload cost vs 10GB
    VRAM held idle 24/7 — usually a fair trade on a shared GPU.

    Set WHISPER_ENABLED=0 to disable local STT entirely — useful when the GPU
    is needed for other workloads and the user is happy using OS keyboard
    voice input (iOS dictation / Android Gboard voice / macOS Dictation),
    which is comparable in quality and runs on-device for free.
    """
    if os.environ.get("WHISPER_ENABLED", "1") == "0":
        raise WhisperDisabledError(
            "Local Whisper is disabled (WHISPER_ENABLED=0). "
            "Use your OS keyboard's voice input to dictate text instead."
        )
    global _MODEL, _LAST_USED_TS
    _start_idle_unload_thread()  # idempotent — starts once
    with _MODEL_LOCK:
        if _MODEL is None:
            # Default 'medium' (~3GB) instead of large-v3 (~10GB) — short
            # voice turns rarely benefit from large-v3's accuracy gain;
            # users with strong opinions can override via WHISPER_MODEL=large-v3.
            size = os.environ.get("WHISPER_MODEL", "medium")
            device = os.environ.get("WHISPER_DEVICE", "auto")
            compute_type = os.environ.get("WHISPER_COMPUTE", "float16")
            log.info("Loading whisper model size=%s device=%s compute=%s", size, device, compute_type)
            _MODEL = WhisperModel(size, device=device, compute_type=compute_type)
        _LAST_USED_TS = time.time()
        return _MODEL


def _transcribe_sync(audio_bytes: bytes, language: Optional[str]) -> str:
    """Run faster-whisper on a buffer of audio bytes. Synchronous.

    The input is raw container bytes as the browser recorded them (usually
    WebM/Opus). faster-whisper accepts anything PyAV/ffmpeg can decode, so
    we just wrap the bytes in a BytesIO and hand them over; no manual
    resampling or PCM conversion needed.
    """
    model = _get_model()
    bio = io.BytesIO(audio_bytes)

    # Arguments chosen for a push-to-talk voice interface:
    #
    #   language=None
    #     Let Whisper auto-detect per utterance. This is essential for
    #     code-switching — if we locked to "zh" or "en", mixed speech
    #     would come out wrong. A small cost in accuracy on very short
    #     utterances, worth it for flexibility.
    #
    #   beam_size=5
    #     Default; good quality/speed trade for interactive use. Higher
    #     beams improve WER marginally but take longer, which matters
    #     when the user is holding their phone waiting for a reply.
    #
    #   vad_filter=True
    #     Silero VAD trims silence before transcription. Without it the
    #     model sometimes hallucinates "Thank you." at the end of silent
    #     trailing audio (a well-known Whisper failure mode).
    #
    #   vad_parameters
    #     min_silence_duration_ms=1500 is lenient — we don't want to
    #     split a single slow-spoken sentence into two.
    #     threshold=0.3 is looser than Silero's default 0.5 so we don't
    #     drop quiet speakers.
    #     The front-end already enforces a minimum record duration, so
    #     leniency here is safe.
    #
    #   condition_on_previous_text=False
    #     We re-create the model state per turn; conditioning on an
    #     in-buffer "previous text" (which doesn't exist here) would just
    #     confuse decoding.
    #
    #   initial_prompt=DEFAULT_INITIAL_PROMPT
    #     See DEFAULT_INITIAL_PROMPT above for rationale.
    segments, _info = model.transcribe(
        bio,
        language=language,
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 1500, "threshold": 0.3},
        condition_on_previous_text=False,
        initial_prompt=DEFAULT_INITIAL_PROMPT,
    )
    # ``segments`` is a generator — materialising it forces the full
    # transcription. Concatenating without spaces is correct because each
    # segment's text already has its own leading whitespace when needed.
    return "".join(seg.text for seg in segments).strip()


async def transcribe(audio_bytes: bytes, language: Optional[str] = None) -> str:
    """Async wrapper: run the CPU/GPU-bound work in a thread.

    faster-whisper's ``transcribe`` is synchronous and long-running. Calling
    it directly from the event loop would stall every other WebSocket
    handler for the duration of transcription. ``asyncio.to_thread``
    parks the work on the default thread pool executor while the loop
    keeps serving other connections.
    """
    return await asyncio.to_thread(_transcribe_sync, audio_bytes, language)
