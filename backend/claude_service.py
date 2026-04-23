"""Wraps the ``claude -p`` subprocess so the rest of the app can stream Claude.

Why a subprocess instead of the Anthropic Python SDK
-----------------------------------------------------
The whole point of Claude Voice is to bill voice turns to the user's existing
**Claude Code Max/Pro subscription**, not to their API account. The only way
to do that today is to invoke the ``claude`` CLI — when it's logged in with a
Claude.ai account, every ``claude -p`` invocation is metered against the
subscription's 5-hour window rather than against API credits.

That design choice has a few practical consequences we deal with below:

* Each turn spawns a fresh subprocess. We use ``--session-id`` on the first
  turn and ``--resume <session-id>`` on subsequent turns so Claude keeps its
  context across turns even though the process itself is new each time.
* We parse the CLI's ``--output-format stream-json`` line-by-line to surface
  partial text, tool-use events, and the final result back to the caller.
* We do sentence-level splitting here (not in the UI) because both the
  front-end renderer and the TTS pipeline want sentence boundaries.
"""
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

# Sentence boundary regex. Matches:
#   - Chinese full stop / exclamation / question (。！？)
#   - Western ., !, ? (any, followed by optional whitespace)
#   - One or more newlines (paragraph breaks count too)
#
# Kept deliberately simple: we'd rather split a little too eagerly (short
# sentences start playing faster) than hold back a whole paragraph hunting
# for the "right" boundary. False splits on things like "e.g." or "3.14"
# produce slightly choppier TTS but don't break anything.
SENTENCE_END = re.compile(r"[。！？!?\.]\s*|\n+")


def _discover_claude_bin() -> str:
    """Find the ``claude`` executable.

    Precedence (mirrored in ``setup.sh`` so the two agree):
      1. ``CLAUDE_BIN`` env var — explicit override wins, no questions asked.
      2. ``claude`` on ``PATH`` — recommended install path
         (``npm install -g @anthropic-ai/claude-code``).
      3. A VS Code / Cursor extension bundle — the editor ships its own copy
         of the binary; we find it as a last resort so the project works
         without a global npm install.
    """
    explicit = os.environ.get("CLAUDE_BIN")
    if explicit:
        return explicit

    on_path = shutil.which("claude")
    if on_path:
        return on_path

    # Glob for the extension-bundled binary under any installed version.
    # We sort descending so the newest version wins on machines that have
    # a stale copy kicking around from a prior upgrade.
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
    """One ongoing Claude conversation.

    ``session_id`` is passed to the CLI as ``--session-id`` on the first
    turn and ``--resume <session-id>`` thereafter. The CLI stores the
    actual transcript in its own state directory; we only hold the ID
    and a turn counter.
    """

    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    cwd: str = field(default_factory=lambda: str(Path.home()))
    turn_count: int = 0

    def to_dict(self) -> dict:
        return {"session_id": self.session_id, "cwd": self.cwd, "turn_count": self.turn_count}


class ClaudeService:
    """Spawns ``claude -p`` per turn with ``--resume`` for continuity."""

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
        """Yield structured events as Claude generates its reply.

        Event shapes
        ------------
        ``{"type": "text_delta", "text": "..."}``
            A chunk of Claude's spoken reply. Multiple deltas concatenate
            into the full reply text.
        ``{"type": "sentence", "text": "..."}``
            A full sentence boundary has been observed in the accumulated
            deltas. Emitted in addition to the deltas (not instead of), so
            consumers can choose to render text per-delta but TTS
            per-sentence.
        ``{"type": "tool_use", "name": "Bash"}``
            Claude invoked a tool. Used by the UI to show activity while
            Claude's own voice is silent (e.g. while bash runs).
        ``{"type": "done", "result": "...", "session_id": "..."}``
            The CLI finished cleanly. ``result`` is the full reply.
        ``{"type": "error", "message": "..."}``
            The CLI exited non-zero. Usually means auth, rate limit, or a
            malformed prompt; the message contains stderr for debugging.
        """
        # Build the argv. Notes on flags:
        #   --output-format stream-json       line-delimited JSON events
        #   --include-partial-messages        emit text_delta events as
        #                                     tokens arrive, not just at the end
        #   --verbose                         include tool_use and other
        #                                     auxiliary events we surface in UI
        #   --permission-mode <mode>          see .env.example; usually
        #                                     bypassPermissions for voice use
        args = [self.claude_bin, "-p",
                "--output-format", "stream-json",
                "--include-partial-messages",
                "--verbose",
                "--permission-mode", self.permission_mode]

        # Session handling: the first turn creates the session, every
        # subsequent turn resumes it. The CLI rejects --session-id on
        # resume and --resume on create, which is why we branch here.
        if session.turn_count == 0:
            args += ["--session-id", session.session_id]
        else:
            args += ["--resume", session.session_id]

        if self.model:
            args += ["--model", self.model]

        if system_prompt:
            # --append-system-prompt layers on top of Claude's default
            # system prompt; we don't want to overwrite, only add the
            # voice-style rules.
            args += ["--append-system-prompt", system_prompt]

        # User prompt must be last — it's a positional argument.
        args.append(prompt)

        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=session.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # DEVNULL stdin so the CLI can't ever block waiting for input
            # (it shouldn't, given -p, but belt-and-braces).
            stdin=asyncio.subprocess.DEVNULL,
        )

        # ``buffer`` accumulates text deltas until we spot a sentence
        # boundary; ``final_text`` is the full reply we fall back to if
        # the CLI's result field is empty for some reason.
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
                    # Malformed lines are almost always transient — log
                    # nothing and keep reading rather than abort.
                    continue

                etype = event.get("type")

                if etype == "stream_event":
                    # Anthropic's streaming wire format nests the actual
                    # event inside `.event`. We care about text_delta
                    # pieces inside `content_block_delta`.
                    inner = event.get("event", {})
                    if inner.get("type") == "content_block_delta":
                        delta = inner.get("delta", {})
                        if delta.get("type") == "text_delta":
                            chunk = delta.get("text", "")
                            if chunk:
                                final_text += chunk
                                buffer += chunk
                                yield {"type": "text_delta", "text": chunk}
                                # Greedy sentence extraction: keep slicing
                                # off the front of the buffer as long as
                                # we keep finding boundaries. Handles the
                                # case where a single delta contains two
                                # short sentences.
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
                    # The `assistant` event contains the whole structured
                    # message including tool_use blocks. We only forward
                    # the tool-use name — the UI renders a small
                    # "reading main.py…" style badge.
                    msg = event.get("message", {})
                    for block in msg.get("content", []):
                        if block.get("type") == "tool_use":
                            yield {"type": "tool_use", "name": block.get("name", "?")}

                elif etype == "result":
                    # Final "done" marker. ``result`` should be the full
                    # reply text; fall back to our accumulated deltas if
                    # the CLI omitted it for any reason.
                    result = event.get("result", "") or final_text
                    # Flush any trailing text that didn't end with a
                    # sentence terminator, so TTS speaks the last clause.
                    tail = buffer.strip()
                    if tail:
                        yield {"type": "sentence", "text": tail}
                        buffer = ""
                    session.turn_count += 1
                    yield {"type": "done", "result": result, "session_id": session.session_id}

        finally:
            # Reap the subprocess. If generation was cancelled midway
            # (e.g. client disconnected) the pipe closes, but the process
            # can linger — explicit wait + kill guarantees it goes away.
            stderr_data = b""
            if proc.returncode is None:
                try:
                    # Grab a snippet of stderr for the error path below.
                    # Short timeout because we don't want to block
                    # shutdown if stderr is empty.
                    stderr_data = await asyncio.wait_for(proc.stderr.read(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()

            if proc.returncode and proc.returncode != 0:
                # Surface the exit code + leading stderr so the UI can
                # show something actionable. Truncated to keep the
                # message reasonable when stderr is huge.
                yield {
                    "type": "error",
                    "message": f"claude exited with code {proc.returncode}: {stderr_data.decode('utf-8', errors='replace')[:500]}",
                }
