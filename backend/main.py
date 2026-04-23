"""FastAPI server: HTTP page + WebSocket for voice turn I/O."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketState

from claude_service import ClaudeService, Session
from stt import transcribe
from tts import synthesize_stream

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("claude_voice")

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT / "frontend"
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

VOICE_MODE_PROMPT = (
    "You are speaking with the user through a voice interface. Your replies "
    "will be spoken aloud by text-to-speech. Follow these rules strictly:\n"
    "- Do not use any markdown: no asterisks, backticks, pound signs, bullet "
    "dashes, tables, or code blocks.\n"
    "- Do not read URLs, file paths, command-line arguments, long identifiers, "
    "hashes, or raw code aloud.\n"
    "- Keep each sentence short and conversational, like chatting with a "
    "friend. Aim for under 25 words per sentence.\n"
    "- For longer tasks (editing code, researching, analysing files), do the "
    "work silently and summarise in one or two sentences when done. Do not "
    "narrate progress step by step.\n"
    "- For content that needs the screen (code snippets, URLs, long lists, "
    "structured data), do not read it aloud. Tell the user you have written "
    "it down and they can look when convenient.\n"
    "- Do not ask filler questions like 'do you want me to continue' or "
    "'should I expand'. State your conclusion directly.\n"
    "- Only ask a question when you genuinely need confirmation, and keep it "
    "to one short yes/no.\n"
    "- Reply in the same language the user spoke."
)

app = FastAPI(title="Claude Voice")

claude = ClaudeService(
    claude_bin=os.environ.get("CLAUDE_BIN") or None,
    default_cwd=os.environ.get("CLAUDE_CWD") or None,
    model=os.environ.get("CLAUDE_MODEL") or None,
    permission_mode=os.environ.get("CLAUDE_PERMISSION_MODE", "bypassPermissions"),
)


@app.get("/")
async def index():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/favicon.ico")
async def favicon():
    icon = FRONTEND_DIR / "icon-192.png"
    if icon.exists():
        return FileResponse(icon)
    return Response(status_code=204)


app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


async def _send_json(ws: WebSocket, payload: dict) -> None:
    await ws.send_text(json.dumps(payload, ensure_ascii=False))


async def _speak_sentence(ws: WebSocket, text: str) -> None:
    """Synthesize one sentence and push audio frames to the client."""
    await _send_json(ws, {"type": "tts_start", "text": text})
    try:
        async for chunk in synthesize_stream(text):
            await ws.send_bytes(chunk)
    except Exception as e:
        log.exception("TTS failed")
        await _send_json(ws, {"type": "tts_error", "message": str(e)})
        return
    await _send_json(ws, {"type": "tts_end"})


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    session: Optional[Session] = None
    audio_buf = bytearray()
    audio_format = "webm"
    mode = "voice"  # "voice" or "text"

    try:
        while True:
            if ws.client_state != WebSocketState.CONNECTED:
                break
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if "bytes" in msg and msg["bytes"] is not None:
                audio_buf.extend(msg["bytes"])
                continue
            if "text" not in msg or msg["text"] is None:
                continue

            try:
                data = json.loads(msg["text"])
            except json.JSONDecodeError:
                continue

            mtype = data.get("type")

            if mtype == "hello":
                cwd = data.get("cwd") or None
                if data.get("mode") in ("voice", "text"):
                    mode = data["mode"]
                if session is None:
                    session = claude.new_session(cwd=cwd)
                await _send_json(ws, {"type": "ready", "session_id": session.session_id, "cwd": session.cwd, "mode": mode})

            elif mtype == "set_mode":
                if data.get("mode") in ("voice", "text"):
                    mode = data["mode"]
                await _send_json(ws, {"type": "mode", "mode": mode})

            elif mtype == "new_session":
                session = claude.new_session(cwd=data.get("cwd"))
                await _send_json(ws, {"type": "ready", "session_id": session.session_id, "cwd": session.cwd, "mode": mode})

            elif mtype == "audio_start":
                audio_buf = bytearray()
                audio_format = data.get("format", "webm")

            elif mtype == "audio_end":
                if session is None:
                    session = claude.new_session()
                    await _send_json(ws, {"type": "ready", "session_id": session.session_id, "cwd": session.cwd, "mode": mode})

                if not audio_buf:
                    await _send_json(ws, {"type": "error", "message": "no audio received"})
                    continue

                await _send_json(ws, {"type": "transcribing"})
                try:
                    transcript = await transcribe(bytes(audio_buf), language=data.get("language") or None)
                except Exception as e:
                    log.exception("STT failed")
                    await _send_json(ws, {"type": "error", "message": f"STT failed: {e}"})
                    audio_buf = bytearray()
                    continue

                audio_buf = bytearray()

                if not transcript:
                    await _send_json(ws, {"type": "error", "message": "empty transcript"})
                    continue

                await _send_json(ws, {"type": "transcript", "text": transcript})
                await _run_claude_turn(ws, session, transcript, mode)

            elif mtype == "text":
                if session is None:
                    session = claude.new_session()
                prompt = (data.get("text") or "").strip()
                if not prompt:
                    continue
                await _send_json(ws, {"type": "transcript", "text": prompt})
                await _run_claude_turn(ws, session, prompt, mode)

    except WebSocketDisconnect:
        log.info("client disconnected")
    except Exception:
        log.exception("ws handler error")
        try:
            await _send_json(ws, {"type": "error", "message": "server error"})
        except Exception:
            pass


async def _run_claude_turn(ws: WebSocket, session: Session, prompt: str, mode: str = "voice") -> None:
    await _send_json(ws, {"type": "thinking"})
    tts_queue: asyncio.Queue[Optional[str]] = asyncio.Queue()

    async def tts_worker():
        while True:
            sentence = await tts_queue.get()
            if sentence is None:
                return
            await _speak_sentence(ws, sentence)

    worker = asyncio.create_task(tts_worker())
    system_prompt = VOICE_MODE_PROMPT if mode == "voice" else None

    try:
        async for event in claude.ask_stream(session, prompt, system_prompt=system_prompt):
            etype = event["type"]
            if etype == "text_delta":
                await _send_json(ws, {"type": "text_delta", "text": event["text"]})
            elif etype == "sentence":
                if mode == "voice":
                    await tts_queue.put(event["text"])
            elif etype == "tool_use":
                await _send_json(ws, {"type": "tool_use", "name": event["name"]})
            elif etype == "done":
                await tts_queue.put(None)
                await worker
                await _send_json(ws, {"type": "done", "session_id": event["session_id"]})
            elif etype == "error":
                await tts_queue.put(None)
                await worker
                await _send_json(ws, {"type": "error", "message": event["message"]})
    except Exception as e:
        log.exception("turn failed")
        await tts_queue.put(None)
        try:
            await worker
        except Exception:
            pass
        await _send_json(ws, {"type": "error", "message": f"turn failed: {e}"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "7878")),
        reload=False,
    )
