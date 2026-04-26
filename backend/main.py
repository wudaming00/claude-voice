"""FastAPI server — HTTP entry point + WebSocket for voice/text turns.

High-level flow of one voice turn
---------------------------------
1. The PWA front-end records Opus/WebM audio while the user holds the
   push-to-talk button.
2. On release it opens (or reuses) a WebSocket to ``/ws`` and sends:
     - a JSON ``audio_start`` message with a fresh ``turn_id``
     - one or more binary frames containing the audio bytes
     - a JSON ``audio_end`` message
3. This server buffers the bytes, feeds them to ``stt.transcribe`` (local
   faster-whisper), then hands the transcript to ``claude_service.ClaudeService``.
4. ``claude_service`` spawns ``claude -p --resume <session-id>`` and
   streams JSON events back. Whenever a full sentence boundary is detected
   in Claude's reply, it is pushed onto a TTS queue.
5. A background worker pulls sentences off the queue and streams MP3 audio
   chunks back to the browser via the same WebSocket, so Claude's voice
   starts playing while it is still thinking about the rest of the reply.

The session lives for the lifetime of the WebSocket connection. On
reconnect the client can send a ``new_session`` message to start fresh;
otherwise the same ``session_id`` is resumed so Claude keeps its context.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketState

from auth import Auth, AuthConfig, client_ip
from claude_service import ClaudeService, Session
from stt import transcribe
from tts import synthesize_stream

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("claude_voice")

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT / "frontend"
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

# System prompt appended to every voice-mode turn.
#
# Why it's needed: Claude's default output is markdown-heavy and can be
# paragraphs long. Both are awful over TTS — asterisks become "asterisk
# asterisk", code blocks get read character-by-character, and long
# replies keep the user waiting. This prompt reshapes Claude's style
# for speech: short spoken sentences, no markup, no URLs/paths, no
# filler questions.
#
# It's only injected when ``mode == "voice"``. The text-mode toggle on
# the front-end skips it so typed chat gets normal Claude behaviour.
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

# ClaudeService is a thin wrapper around the `claude` CLI — spawning a
# subprocess per turn and parsing its streaming JSON output. We build it
# once at startup from environment variables so every WebSocket
# connection shares the same config.
claude = ClaudeService(
    claude_bin=os.environ.get("CLAUDE_BIN") or None,
    default_cwd=os.environ.get("CLAUDE_CWD") or None,
    model=os.environ.get("CLAUDE_MODEL") or None,
    permission_mode=os.environ.get("CLAUDE_PERMISSION_MODE", "bypassPermissions"),
)

auth_cfg = AuthConfig()
auth = Auth(auth_cfg)

if not auth_cfg.enabled and os.environ.get("HOST", "0.0.0.0") == "0.0.0.0":
    # Belt-and-braces warning: if the server is bound to every interface
    # AND auth is off, the operator almost certainly meant to set a
    # password. A tailnet-only setup usually pairs with HOST=127.0.0.1 +
    # a Tailscale Serve proxy, so this warning won't fire in that case.
    log.warning(
        "AUTH_PASSWORD is empty and HOST=0.0.0.0. Anyone who reaches this "
        "server can drive Claude. Set AUTH_PASSWORD in .env, or bind to "
        "127.0.0.1 and front it with Tailscale Serve."
    )


@app.get("/")
async def index():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/favicon.ico")
async def favicon():
    # Browsers request /favicon.ico eagerly. Return the PWA icon so it
    # shows up as the tab icon too, and fall back to 204 so we don't
    # spam 404s in logs if the icon is missing.
    icon = FRONTEND_DIR / "icon-192.png"
    if icon.exists():
        return FileResponse(icon)
    return Response(status_code=204)


# Serve the PWA assets (manifest, icons, JS, CSS) from /static.
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/auth/status")
async def auth_status():
    """Tell the front-end whether to show a login gate.

    We only expose the boolean, never the password. The client uses this
    to decide whether to render the login modal on first load; if auth
    is disabled the modal is skipped entirely.
    """
    return {"auth_required": auth_cfg.enabled}


@app.post("/auth/login")
async def auth_login(request: Request):
    ip = client_ip(request)
    try:
        data = await request.json()
    except Exception:
        data = {}
    password = (data.get("password") if isinstance(data, dict) else "") or ""
    token, err = auth.login(ip, password)
    if err:
        log.warning("login failed from %s", ip)
        return JSONResponse({"error": err}, status_code=401)
    return {"token": token, "ttl_seconds": auth_cfg.token_ttl_seconds}


@app.post("/auth/refresh")
async def auth_refresh(request: Request):
    """Exchange a still-valid token for a fresh one with a full TTL.

    The client calls this when the existing token's remaining life drops
    below ``refresh_window_seconds`` (currently 7 days). As long as the
    user keeps opening the PWA every 23 days their token treadmills
    forward and they never see the login screen. Expired tokens are
    refused — the 30-day ceiling is a feature.
    """
    try:
        data = await request.json()
    except Exception:
        data = {}
    old_token = (data.get("token") if isinstance(data, dict) else "") or ""
    new_token = auth.refresh_token(old_token)
    if not new_token:
        return JSONResponse(
            {"error": "Token expired or invalid — please log in again."},
            status_code=401,
        )
    return {"token": new_token, "ttl_seconds": auth_cfg.token_ttl_seconds}


def _ws_alive(ws: WebSocket) -> bool:
    """Cheap check before writing to a socket that might already be closed.

    FastAPI/Starlette keeps two state fields: ``client_state`` is what the
    browser has done, ``application_state`` is what we've done. Either
    being non-CONNECTED means a write will either error or vanish, so we
    short-circuit before attempting it. This prevents noisy tracebacks
    when the user disconnects mid-turn.
    """
    return (
        ws.client_state == WebSocketState.CONNECTED
        and ws.application_state == WebSocketState.CONNECTED
    )


async def _send_json(ws: WebSocket, payload: dict) -> bool:
    """Send JSON, swallowing disconnect errors.

    Returns True on success, False if the peer is gone. We return a
    boolean (rather than raising) because the caller usually wants to
    abort the current turn cleanly, not propagate a cascade of
    ``WebSocketDisconnect`` exceptions up the stack.
    """
    if not _ws_alive(ws):
        return False
    try:
        # ``ensure_ascii=False`` is important for Chinese/Japanese/Korean
        # content — we want real characters on the wire, not \uXXXX escapes.
        await ws.send_text(json.dumps(payload, ensure_ascii=False))
        return True
    except (WebSocketDisconnect, RuntimeError):
        return False


async def _send_bytes(ws: WebSocket, data: bytes) -> bool:
    """Binary counterpart of ``_send_json``. Same disconnect-swallowing contract."""
    if not _ws_alive(ws):
        return False
    try:
        await ws.send_bytes(data)
        return True
    except (WebSocketDisconnect, RuntimeError):
        return False


async def _speak_sentence(ws: WebSocket, text: str) -> None:
    """Synthesize one sentence and push audio frames to the client.

    Protocol framing on the wire:
        {"type": "tts_start", "text": "..."}   # lets the UI display the sentence
        <binary MP3 chunk>, <binary MP3 chunk>, ...
        {"type": "tts_end"}                    # the UI can mark this sentence done

    If the client disconnects partway, we abort quietly — there's no
    point finishing TTS for an audience that left. Exceptions from the
    Edge TTS call are reported to the client as ``tts_error`` so the UI
    can fall back to showing the text without blocking on audio.
    """
    if not await _send_json(ws, {"type": "tts_start", "text": text}):
        return
    try:
        async for chunk in synthesize_stream(text):
            if not await _send_bytes(ws, chunk):
                return
    except Exception as e:
        log.exception("TTS failed")
        await _send_json(ws, {"type": "tts_error", "message": str(e)})
        return
    await _send_json(ws, {"type": "tts_end"})


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    """The only WebSocket route. Handles the full lifecycle of one client.

    Message types accepted from the client:
        hello        — initial handshake, optionally sets cwd and mode
        set_mode     — toggle between voice and text mode mid-session
        new_session  — discard the current Claude session and start fresh
        audio_start  — begin a new voice turn (with a turn_id for dedup)
        audio_end    — mark the end of a voice turn; trigger STT + Claude
        text         — text-mode input (skip STT, go straight to Claude)
        <binary>     — raw audio bytes (expected between audio_start/end)

    The ``processed_turn_ids`` set defends against a browser bug that
    sometimes fires ``audio_end`` twice for the same recording — without
    dedup we would charge the user's subscription for the same transcript
    twice and speak the same reply back to them.
    """
    # Auth gate: accept the handshake first so we can close with a custom
    # 4401 application code that the browser actually surfaces (browsers
    # collapse pre-accept rejections into a generic 1006, making it
    # indistinguishable from a network blip and breaking the "bad token →
    # show login modal" branch on the client).
    if auth_cfg.enabled:
        token = ws.query_params.get("token")
        if not auth.validate_token(token):
            log.info("ws rejected: bad/missing token from %s", client_ip(ws))
            await ws.accept()
            await ws.close(code=4401, reason="unauthorized")
            return

    await ws.accept()
    session: Optional[Session] = None
    audio_buf = bytearray()
    audio_format = "webm"
    mode = "voice"  # "voice" or "text"
    current_turn_id: Optional[str] = None
    processed_turn_ids: set[str] = set()

    try:
        while True:
            if ws.client_state != WebSocketState.CONNECTED:
                break
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break

            # WebSocket frames from Starlette come with either a "bytes"
            # or "text" field set. Binary bytes are only ever audio, so
            # just append and wait for the next JSON envelope.
            if "bytes" in msg and msg["bytes"] is not None:
                audio_buf.extend(msg["bytes"])
                continue
            if "text" not in msg or msg["text"] is None:
                continue

            try:
                data = json.loads(msg["text"])
            except json.JSONDecodeError:
                # Ignore malformed frames rather than disconnect — a client
                # with a bug shouldn't be able to kill its own session.
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
                # Claude's context (files read, prior instructions) lives
                # inside the subprocess's view of --resume <session-id>.
                # Creating a new Session here forces the next turn to pass
                # --session-id instead, giving Claude a clean slate.
                session = claude.new_session(cwd=data.get("cwd"))
                await _send_json(ws, {"type": "ready", "session_id": session.session_id, "cwd": session.cwd, "mode": mode})

            elif mtype == "audio_start":
                audio_buf = bytearray()
                audio_format = data.get("format", "webm")
                current_turn_id = data.get("turn_id")
                log.info("audio_start turn_id=%s format=%s", current_turn_id, audio_format)

            elif mtype == "audio_end":
                incoming_turn_id = data.get("turn_id") or current_turn_id
                log.info("audio_end turn_id=%s buf_bytes=%d", incoming_turn_id, len(audio_buf))

                # Dedup duplicate audio_end frames (see set-up in docstring).
                if incoming_turn_id and incoming_turn_id in processed_turn_ids:
                    log.warning("dropping duplicate audio_end for turn_id=%s", incoming_turn_id)
                    audio_buf = bytearray()
                    continue

                # Lazy session creation: a client that skips `hello` and
                # jumps straight to audio still gets a working session.
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
                    # Distinguish disabled-by-config vs real failure so the front-end
                    # can prompt user to use OS keyboard voice input instead.
                    from stt import WhisperDisabledError
                    if isinstance(e, WhisperDisabledError):
                        log.info("STT request received but disabled — instructing user to use OS keyboard voice input")
                        await _send_json(ws, {
                            "type": "error",
                            "code": "stt_disabled",
                            "message": "本地语音识别已关闭。请用键盘的麦克风按钮(iOS 听写 / Gboard 语音输入)说话,文字会出现在输入框,然后点发送即可。",
                        })
                    else:
                        log.exception("STT failed")
                        await _send_json(ws, {"type": "error", "message": f"STT failed: {e}"})
                    audio_buf = bytearray()
                    continue

                # Always reset the buffer before processing the transcript.
                # Otherwise a failed turn would concatenate with the next one
                # and produce a garbled second attempt.
                audio_buf = bytearray()

                if not transcript:
                    await _send_json(ws, {"type": "error", "message": "empty transcript"})
                    continue

                if incoming_turn_id:
                    processed_turn_ids.add(incoming_turn_id)
                    # Bound the dedup set. Clients don't replay turn IDs
                    # arbitrarily far into the past, so forgetting the
                    # oldest entries is safe. ``set.pop()`` removes an
                    # arbitrary element which is fine for this purpose.
                    if len(processed_turn_ids) > 64:
                        processed_turn_ids.pop()

                log.info("transcript turn_id=%s chars=%d", incoming_turn_id, len(transcript))
                await _send_json(ws, {"type": "transcript", "text": transcript})
                await _run_claude_turn(ws, session, transcript, mode)

            elif mtype == "text":
                # Text mode skips STT entirely. Useful on a laptop, and for
                # debugging without needing a working microphone.
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
        # Best-effort error frame. If the socket is already gone this is
        # a no-op thanks to the disconnect-swallowing helpers above.
        try:
            await _send_json(ws, {"type": "error", "message": "server error"})
        except Exception:
            pass


async def _run_claude_turn(ws: WebSocket, session: Session, prompt: str, mode: str = "voice") -> None:
    """Drive one turn through Claude and stream sentences back as they land.

    Pipeline:
        Claude CLI stdout (JSON events)
            └─> sentence-boundary detector in claude_service
                    └─> tts_queue (asyncio.Queue)
                            └─> background tts_worker → _speak_sentence → WebSocket

    The queue decouples Claude's text-production rate from Edge TTS's
    audio-production rate. If Claude is faster (short sentences, network
    cache hits) the queue fills briefly; if TTS is faster it drains. Either
    way the user starts hearing audio within ~1 sentence of Claude starting
    to speak.
    """
    await _send_json(ws, {"type": "thinking"})
    tts_queue: asyncio.Queue[Optional[str]] = asyncio.Queue()

    async def tts_worker():
        # Drain sentences sequentially. If the client has already
        # disconnected we keep pulling from the queue but don't send, so
        # the producer is never blocked by a dead consumer.
        while True:
            sentence = await tts_queue.get()
            if sentence is None:
                return
            if not _ws_alive(ws):
                continue
            await _speak_sentence(ws, sentence)

    worker = asyncio.create_task(tts_worker())
    system_prompt = VOICE_MODE_PROMPT if mode == "voice" else None
    claude_stream = claude.ask_stream(session, prompt, system_prompt=system_prompt)

    async def stop_worker():
        # Send a None sentinel so the worker exits its loop, then await
        # it so the task is reaped cleanly (prevents "Task was destroyed
        # but it is pending" warnings at shutdown).
        await tts_queue.put(None)
        try:
            await worker
        except Exception:
            pass

    try:
        async for event in claude_stream:
            if not _ws_alive(ws):
                log.info("client disconnected mid-turn, aborting stream")
                break
            etype = event["type"]
            if etype == "text_delta":
                # Forward the text delta so the UI can render Claude's
                # reply incrementally. TTS runs off whole sentences only.
                await _send_json(ws, {"type": "text_delta", "text": event["text"]})
            elif etype == "sentence":
                if mode == "voice":
                    await tts_queue.put(event["text"])
            elif etype == "tool_use":
                # Surface tool use in the UI so the user can see Claude is
                # doing something (e.g. "reading main.py") rather than
                # staring at silence while Claude runs bash commands.
                await _send_json(ws, {"type": "tool_use", "name": event["name"]})
            elif etype == "done":
                await stop_worker()
                await _send_json(ws, {"type": "done", "session_id": event["session_id"]})
            elif etype == "error":
                await stop_worker()
                await _send_json(ws, {"type": "error", "message": event["message"]})
    except Exception as e:
        log.exception("turn failed")
        await stop_worker()
        await _send_json(ws, {"type": "error", "message": f"turn failed: {e}"})
    finally:
        # Defensive cleanup: even if the loop broke from an early return
        # or exception, make sure the worker is stopped and the Claude
        # subprocess is reaped. Otherwise repeated turns can leak both
        # asyncio tasks and OS processes.
        await stop_worker()
        try:
            await claude_stream.aclose()
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "7878")),
        reload=False,
    )
