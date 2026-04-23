# Claude Voice

Hands-free voice interface for **Claude Code**, billed to your **Max subscription** (not API credits). Hold a button on your phone, speak, hear Claude reply — all round-trip streamed, with per-sentence TTS so Claude starts talking before it's finished thinking.

![Claude Voice icon](frontend/icon.svg)

- Talk to Claude while driving, cooking, walking — or just when you don't want to type.
- Uses your existing Claude Code subscription (via the `claude` CLI), so no extra API bill.
- Streams speech in and out: local Whisper for STT, Edge TTS for speech synthesis, sentence-level pipelining for low latency.
- Installs to your phone home screen as a PWA.
- Works on macOS, Linux, Windows (WSL), with NVIDIA GPU acceleration when available.

## Why

Claude Code is powerful, but desktop-typed. If you're already on a Max plan, you're paying for usage you can't easily tap away from the keyboard. This project gives your subscription a second interface: a push-to-talk mic on any phone in your house (or anywhere on the internet) that feeds straight into the real `claude` agent, with full tool use, file edits, git pushes — everything the CLI can do.

## Architecture

```
 Phone browser (PWA, push-to-talk)
        │  WebSocket over HTTPS
        ▼
 FastAPI server (your machine)
        ├─ faster-whisper       (STT, local, CPU or CUDA)
        ├─ claude -p --resume   (subscription-billed Claude Code)
        └─ edge-tts             (TTS, per-sentence streaming)
```

One FastAPI process hosts the PWA, runs Whisper locally, shells out to the `claude` CLI for each turn (passing `--resume <session-id>` to keep context), and pipes Claude's streaming output through Edge TTS sentence-by-sentence back to the browser.

## Prerequisites

- **Python 3.10+** — `python3 --version`
- **Node.js 18+** and **Claude Code CLI** — `npm install -g @anthropic-ai/claude-code`, then `claude login` with your Max subscription.
- **ffmpeg** (for decoding phone-recorded WebM/Opus audio)
  - macOS: `brew install ffmpeg`
  - Debian/Ubuntu/WSL: `sudo apt install ffmpeg`
  - Windows native: `winget install Gyan.FFmpeg`
- **Modern browser** on the device you'll talk from (Chrome, Edge, Safari 14+, Firefox).
- **Optional**: NVIDIA GPU + CUDA for faster Whisper inference (CPU also works, just slower).

## Quick start

```bash
git clone https://github.com/<you>/claude-voice.git
cd claude-voice

# 1. Python environment
python3 -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. Config
cp .env.example .env
#   → edit CLAUDE_CWD to point at the project you want Claude to work in.
#   → leave CLAUDE_BIN blank to auto-discover `claude`.

# 3. Run
./run.sh
```

Open `http://127.0.0.1:7878/` **on the same machine**. Press and hold the blue button, talk, release. Localhost is the only plain-HTTP origin browsers allow microphone access from — to use it from your phone, see the next section.

## Using it from your phone

Browsers require HTTPS for mic access on any non-localhost origin. Two easy ways to expose the server:

### Tailscale Funnel (recommended, free, valid certs, zero router config)

```bash
tailscale up
tailscale cert $(tailscale status --json | jq -r .Self.DNSName | sed 's/\.$//')
tailscale serve --https=443 --bg http://127.0.0.1:7878
tailscale funnel --bg 443
tailscale funnel status     # prints the public HTTPS URL
```

Open the printed URL on your phone → Safari/Chrome "Add to Home Screen" → the app launches full-screen as a PWA.

### Cloudflare Tunnel (no account needed, one-shot URL)

```bash
cloudflared tunnel --url http://127.0.0.1:7878
```

Prints a random `*.trycloudflare.com` HTTPS URL. Good for testing.

### Self-signed HTTPS

If you're on a LAN you trust and don't want a tunnel, you can run any local HTTPS terminator (Caddy, nginx, `mkcert` + a tiny proxy) and accept the cert on your phone. Details out of scope here.

## How the subscription billing works

The `claude` CLI, when logged in via `claude login` with a Claude.ai account, bills every `claude -p` invocation to your Max/Pro subscription instead of the API. You can verify with:

```bash
claude auth status
# "authMethod": "claude.ai"
# "apiKeySource": "none"
```

That means this project costs you nothing beyond the subscription you already have. The standard Max rate window (5 hours) applies — a normal voice conversation stays well under it.

## Configuration

All knobs live in `.env`. See [.env.example](.env.example) for the full list. Highlights:

| Variable | Purpose |
|---|---|
| `CLAUDE_BIN` | Leave blank to auto-discover. Set if you want to pin a specific binary. |
| `CLAUDE_CWD` | The project Claude will see. **Scope this tightly** — this is the blast radius. |
| `CLAUDE_MODEL` | `sonnet`, `haiku`, `opus`, or blank for your account default. |
| `CLAUDE_PERMISSION_MODE` | `bypassPermissions` for hands-free use; `default` will stall waiting for UI prompts you can't see. |
| `WHISPER_MODEL` | `large-v3` (best, slow), `medium`, `small`, `base`, `tiny`. |
| `WHISPER_DEVICE` | `auto` / `cpu` / `cuda`. |
| `WHISPER_COMPUTE` | `float16` on GPU, `int8` on CPU is a good default. |
| `EDGE_VOICE` | Any voice from `edge-tts --list-voices`. |

## Safety

`CLAUDE_PERMISSION_MODE=bypassPermissions` lets Claude run shell commands, edit files, push to git, delete things — all without asking. It has to, because you can't see or tap a permission prompt from a phone mic. Mitigations:

- Scope `CLAUDE_CWD` to a single project directory, not your home.
- Run the server as a dedicated OS user with no sudo and no access to unrelated repos.
- Keep the Funnel URL private — don't post it publicly. Anyone who reaches it can drive your Claude.
- Consider a small "confirmation word" wrapper if you want an extra brake on destructive operations.

If you're not comfortable with `bypassPermissions`, set `CLAUDE_PERMISSION_MODE=default` — Claude will halt on tool use and you'll be limited to read-only style conversations (still useful for thinking-out-loud sessions).

## Roadmap

- [x] Push-to-talk voice loop with subscription billing
- [x] Per-sentence TTS streaming (start speaking mid-response)
- [x] Session continuity via `--resume`
- [x] Voice / text mode toggle
- [x] Dynamic `claude` binary discovery (no hardcoded paths)
- [ ] Barge-in (user starts talking → Claude stops mid-sentence)
- [ ] VAD + wake word for fully hands-free mode
- [ ] Spoken tool-use progress ("reading main.py…", "running tests…")
- [ ] Multi-project switching via voice command
- [ ] Local-only TTS (Piper, CosyVoice) for offline use

PRs welcome — see [Contributing](#contributing).

## Troubleshooting

**"Could not find the `claude` executable"** — run `npm install -g @anthropic-ai/claude-code` and `claude login`. If you insist on using the VS Code extension binary, point `CLAUDE_BIN` to its full path (version number included).

**Mic permission denied on phone** — you're on HTTP, not HTTPS. Use Tailscale Funnel or `cloudflared`.

**Whisper first-time download is slow** — `large-v3` is ~3 GB. It caches; subsequent starts are instant. If you don't have the bandwidth or disk, set `WHISPER_MODEL=small` in `.env`.

**STT returns empty transcript** — recording was too short or silence-heavy. Hold the button longer. You can also loosen `vad_parameters` in `backend/stt.py`.

**TTS latency spikes** — Edge TTS talks to a Microsoft endpoint. On a slow link, consider a local TTS (Piper is small and fast) or cache common phrases.

**"rate_limit" from `claude`** — you've hit the Max 5-hour window. The CLI prints a `resetsAt` timestamp.

**WSL: `claude` spawns but never responds** — make sure the `claude` process started under WSL, not the Windows host. Run `which claude` inside WSL.

## Contributing

This project is licensed under **AGPL-3.0**. Any derivative — including running a modified version as a network service — must be released under the same license. Contributions are welcome under the same terms.

If you want to:
- Add a language / voice preset
- Plug in a different STT or TTS backend
- Improve the PWA (offline shell, proper install prompt, iOS quirks)
- Add barge-in or wake-word support

… open an issue first describing the approach, then send a PR.

## License

[AGPL-3.0](LICENSE) © 2026 Claude Voice contributors.

This is a strong copyleft license: if you distribute a modified version, or host it as a service, you must release your source under the same license. The goal is to keep the project and all derivatives free and open to everyone.
