"""Wraps `claude -p` subprocess for streaming, subscription-billed Claude calls."""
from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Optional

log = logging.getLogger("claude_voice.claude_service")

SENTENCE_END = re.compile(r"[。！？!?\.]\s*|\n+")


def _discover_claude_bin() -> str:
    """Find the `claude` executable.

    Precedence:
      1. CLAUDE_BIN env var (explicit override).
      2. `claude` on PATH (recommended — install via `npm i -g @anthropic-ai/claude-code`).
      3. VS Code / VS Code Server extension native binary (any version).
    """
    explicit = os.environ.get("CLAUDE_BIN")
    if explicit:
        return explicit

    on_path = shutil.which("claude")
    if on_path:
        return on_path

    home = Path.home()
    patterns = [
        home / ".vscode-server/extensions/anthropic.claude-code-*-linux-*/resources/native-binary/claude",
        home / ".vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude",
        home / ".cursor-server/extensions/anthropic.claude-code-*/resources/native-binary/claude",
    ]
    candidates: list[str] = []
    for pat in patterns:
        candidates.extend(glob.glob(str(pat)))
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0]

    raise RuntimeError(
        "Could not find the `claude` executable. Install Claude Code CLI "
        "(`npm install -g @anthropic-ai/claude-code`) and run `claude login`, "
        "or set CLAUDE_BIN in your .env to an absolute path."
    )


@dataclass
class Session:
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    cwd: str = field(default_factory=lambda: str(Path.home()))
    turn_count: int = 0

    def to_dict(self) -> dict:
        return {"session_id": self.session_id, "cwd": self.cwd, "turn_count": self.turn_count}


class ClaudeService:
    """Spawns `claude -p` per turn with --resume for continuity."""

    def __init__(
        self,
        claude_bin: Optional[str] = None,
        default_cwd: Optional[str] = None,
        model: Optional[str] = None,
        permission_mode: str = "bypassPermissions",
    ):
        self.claude_bin = claude_bin or _discover_claude_bin()
        self.default_cwd = default_cwd or str(Path.home())
        self.model = model
        self.permission_mode = permission_mode
        log.info("Using claude binary: %s", self.claude_bin)
        log.info("Default working directory: %s", self.default_cwd)

    def new_session(self, cwd: Optional[str] = None) -> Session:
        return Session(cwd=cwd or self.default_cwd)

    async def ask_stream(
        self,
        session: Session,
        prompt: str,
        system_prompt: Optional[str] = None,
    ) -> AsyncIterator[dict]:
        """Yield events: {'type': 'text_delta', 'text': str}, {'type': 'tool_use', 'name': str},
        {'type': 'sentence', 'text': str}, {'type': 'done', 'result': str}, {'type': 'error', 'message': str}.
        """
        args = [self.claude_bin, "-p",
                "--output-format", "stream-json",
                "--include-partial-messages",
                "--verbose",
                "--permission-mode", self.permission_mode]

        if session.turn_count == 0:
            args += ["--session-id", session.session_id]
        else:
            args += ["--resume", session.session_id]

        if self.model:
            args += ["--model", self.model]

        if system_prompt:
            args += ["--append-system-prompt", system_prompt]

        args.append(prompt)

        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=session.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )

        buffer = ""
        final_text = ""
        assert proc.stdout is not None

        try:
            async for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type")

                if etype == "stream_event":
                    inner = event.get("event", {})
                    if inner.get("type") == "content_block_delta":
                        delta = inner.get("delta", {})
                        if delta.get("type") == "text_delta":
                            chunk = delta.get("text", "")
                            if chunk:
                                final_text += chunk
                                buffer += chunk
                                yield {"type": "text_delta", "text": chunk}
                                while True:
                                    m = SENTENCE_END.search(buffer)
                                    if not m:
                                        break
                                    end = m.end()
                                    sentence = buffer[:end].strip()
                                    buffer = buffer[end:]
                                    if sentence:
                                        yield {"type": "sentence", "text": sentence}

                elif etype == "assistant":
                    msg = event.get("message", {})
                    for block in msg.get("content", []):
                        if block.get("type") == "tool_use":
                            yield {"type": "tool_use", "name": block.get("name", "?")}

                elif etype == "result":
                    result = event.get("result", "") or final_text
                    tail = buffer.strip()
                    if tail:
                        yield {"type": "sentence", "text": tail}
                        buffer = ""
                    session.turn_count += 1
                    yield {"type": "done", "result": result, "session_id": session.session_id}

        finally:
            stderr_data = b""
            if proc.returncode is None:
                try:
                    stderr_data = await asyncio.wait_for(proc.stderr.read(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()

            if proc.returncode and proc.returncode != 0:
                yield {
                    "type": "error",
                    "message": f"claude exited with code {proc.returncode}: {stderr_data.decode('utf-8', errors='replace')[:500]}",
                }
