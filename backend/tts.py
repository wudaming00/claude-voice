"""Text-to-speech using Microsoft Edge TTS (free, high-quality multilingual voices)."""
from __future__ import annotations

import os
from typing import AsyncIterator

import edge_tts

DEFAULT_VOICE = os.environ.get("EDGE_VOICE", "en-US-AriaNeural")
DEFAULT_RATE = os.environ.get("EDGE_RATE", "+10%")


async def synthesize_stream(text: str, voice: str = DEFAULT_VOICE, rate: str = DEFAULT_RATE) -> AsyncIterator[bytes]:
    """Yield MP3 audio chunks for the given text."""
    communicate = edge_tts.Communicate(text, voice, rate=rate)
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            yield chunk["data"]
