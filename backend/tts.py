"""Text-to-speech using Microsoft Edge TTS (free, high-quality multilingual voices)."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import AsyncIterator

import edge_tts
from edge_tts.exceptions import NoAudioReceived

log = logging.getLogger("claude_voice.tts")

DEFAULT_VOICE = os.environ.get("EDGE_VOICE", "en-US-AvaMultilingualNeural")
DEFAULT_RATE = os.environ.get("EDGE_RATE", "+10%")
MAX_ATTEMPTS = 4


async def synthesize_stream(text: str, voice: str = DEFAULT_VOICE, rate: str = DEFAULT_RATE) -> AsyncIterator[bytes]:
    """Yield MP3 audio chunks for the given text.

    Edge TTS occasionally returns NoAudioReceived (transient MS-side rate limiting
    or a bad region on the anycast pool). Retry with backoff before giving up.
    """
    last_err: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        buffer: list[bytes] = []
        try:
            communicate = edge_tts.Communicate(text, voice, rate=rate)
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    buffer.append(chunk["data"])
            if not buffer:
                raise NoAudioReceived("empty audio stream")
            for data in buffer:
                yield data
            return
        except NoAudioReceived as e:
            last_err = e
            log.warning("edge-tts NoAudioReceived (attempt %d/%d): %s", attempt, MAX_ATTEMPTS, e)
            if attempt < MAX_ATTEMPTS:
                await asyncio.sleep(0.4 * attempt)
    assert last_err is not None
    raise last_err
