"""Speech-to-text using faster-whisper. Loaded once, used per turn."""
from __future__ import annotations

import asyncio
import io
import logging
import os
from typing import Optional

from faster_whisper import WhisperModel

log = logging.getLogger("claude_voice.stt")

_MODEL: Optional[WhisperModel] = None

# Prime Whisper with common technical vocabulary. This is used as previous-context
# and biases decoding toward these tokens, noticeably improving code-switched
# recognition (e.g. mixed Chinese/Japanese/Korean with English tech terms).
DEFAULT_INITIAL_PROMPT = os.environ.get(
    "WHISPER_INITIAL_PROMPT",
    "A technical conversation, possibly mixing English programming terms "
    "including Claude, Python, TypeScript, JavaScript, Rust, Go, GitHub, "
    "FastAPI, WebSocket, Docker, API, PWA.",
)


def _get_model() -> WhisperModel:
    global _MODEL
    if _MODEL is None:
        size = os.environ.get("WHISPER_MODEL", "large-v3")
        device = os.environ.get("WHISPER_DEVICE", "auto")
        compute_type = os.environ.get("WHISPER_COMPUTE", "float16")
        log.info("Loading whisper model size=%s device=%s compute=%s", size, device, compute_type)
        _MODEL = WhisperModel(size, device=device, compute_type=compute_type)
    return _MODEL


def _transcribe_sync(audio_bytes: bytes, language: Optional[str]) -> str:
    model = _get_model()
    bio = io.BytesIO(audio_bytes)
    # language=None lets Whisper detect per utterance — essential for code-switching.
    # VAD is kept on but lenient so we do not drop short utterances;
    # the client already gates on minimum record duration.
    segments, _info = model.transcribe(
        bio,
        language=language,
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 1500, "threshold": 0.3},
        condition_on_previous_text=False,
        initial_prompt=DEFAULT_INITIAL_PROMPT,
    )
    return "".join(seg.text for seg in segments).strip()


async def transcribe(audio_bytes: bytes, language: Optional[str] = None) -> str:
    return await asyncio.to_thread(_transcribe_sync, audio_bytes, language)
