<p align="center">
  <img src="frontend/icon.svg" alt="Claude Voice" width="96" height="96">
</p>

# Claude Voice

Hands-free voice interface for **Claude Code**, billed to your **Max subscription** (not API credits). Hold a button on your phone, speak, hear Claude reply — all round-trip streamed, with per-sentence TTS so Claude starts talking before it's finished thinking.

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
git clone https://github.com/wudaming00/claude-voice.git
cd claude-voice
./setup.sh       # creates .venv, installs deps, generates AUTH_PASSWORD
./start.sh       # runs the server, opens a cloudflared tunnel, prints the login QR
```

`./start.sh` leaves the process attached to your terminal; scan the QR from your phone and you're in. Ctrl+C stops the server and tunnel together.

If you prefer to run things by hand or want a stable Tailscale URL instead of a random cloudflared one, see [Using it from your phone](#using-it-from-your-phone) below.

## Using it from your phone

Browsers require HTTPS for mic access on any non-localhost origin, so the server has to be exposed over a real HTTPS URL. Two easy paths:

### Tailscale Funnel (recommended, free, valid certs, zero router config)

```bash
./expose.sh
```

That script installs-checks Tailscale, provisions a cert, opens a Funnel on port 443, and at the end prints both the HTTPS URL and a **QR code containing a one-tap login link** (see below). Scan it with your phone camera, the PWA opens, and you're signed in for 30 days. No typing the 22-character password.

### Cloudflare Tunnel (no account needed, one-shot URL)

```bash
./start.sh
```

That starts the server, opens a `cloudflared` tunnel, parses the random `*.trycloudflare.com` URL out of its logs, writes it to `.env` as `PUBLIC_URL`, prints the login QR, and stays in the foreground. Ctrl+C cleans everything up. The URL changes every time `cloudflared` restarts, which is why this is the "testing from my phone right now" path rather than the long-term deployment path.

If you'd rather drive the pieces by hand:

```bash
./run.sh &                                           # terminal 1
cloudflared tunnel --url http://127.0.0.1:7878 &     # terminal 2, note the URL it prints
# edit .env: PUBLIC_URL=https://<that-url>
./expose.sh qr                                       # prints the QR
```

### Running as a service (auto-start on boot)

If you want Claude Voice to come back up after reboots without someone typing anything, see [docs/autostart.md](docs/autostart.md) for systemd / WSL2 / launchd templates. Pair it with Tailscale, not `cloudflared` — random tunnel URLs don't survive a reboot.

### Self-signed HTTPS

If you're on a LAN you trust and don't want a tunnel, you can run any local HTTPS terminator (Caddy, nginx, `mkcert` + a tiny proxy) and accept the cert on your phone. Details out of scope here.

## Login, passwords, and the QR flow

The server gates every request (including the WebSocket) on a bearer token. Tokens are stateless — they're HMAC-signed with a secret derived from `AUTH_PASSWORD`, so a password rotation invalidates every token instantly without any server-side bookkeeping.

### First-time login

1. `setup.sh` generates a strong random `AUTH_PASSWORD` and writes it to `.env`. You never need to remember it or type it on the phone.
2. `expose.sh` prints a QR code that encodes `https://<your-url>/#login=<password>`. The password rides in the URL fragment, which browsers never send to servers or to proxies — so it doesn't leak into access logs.
3. Scan the QR with the phone camera. The PWA opens, reads the fragment, exchanges the password for a 30-day token, and immediately wipes the fragment from the address bar.

### Keeping the session alive

When the token has less than 7 days of life left, the front-end silently calls `/auth/refresh` to mint a fresh 30-day token. As long as you open the PWA at least once every 23 days, you never see the login screen again.

### Losing access

| What happened | What to do |
|---|---|
| New device (iPad, second phone) — want it to sign in | On the server machine, run `./expose.sh qr`. Scan from the new device. The same QR works for any number of devices. |
| Phone's token expired (haven't opened the PWA for 30+ days) | You have to touch the server machine. SSH in if needed, run `./expose.sh qr`, rescan from the phone. This is by design — the 30-day ceiling is your cap on "device was stolen and the owner forgot about it." |
| URL leaked, want to lock everyone out | `./expose.sh rotate`. A new random password is generated, the server restarts, every previously issued token stops working instantly. A new QR is printed for you to rescan. |
| Forgot what the password is | `./expose.sh rotate` prints the new one (and `.env` always holds the current one). You should never need to read it. |
| Cloudflared URL changed | Update `PUBLIC_URL=...` in `.env`, then `./expose.sh qr` regenerates the QR with the new URL. The password is unchanged. |
| Server restart | Tokens survive. Nothing to do — HMAC validation is stateless. |

### Skipping auth altogether

If you're only reachable over a trusted network (e.g. Tailscale Serve on your tailnet, no Funnel), blank `AUTH_PASSWORD=` in `.env`. The login modal and WS gate both collapse into no-ops. A start-up warning reminds you the server is bound to `0.0.0.0` without a password — switch to `HOST=127.0.0.1` if you go this route.

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
| `AUTH_PASSWORD` | Shared-secret password for the PWA. `setup.sh` generates one automatically. Leave blank only for tailnet-only deployments. |
| `PUBLIC_URL` | The externally reachable HTTPS URL (e.g. `https://…trycloudflare.com`). Used by `./expose.sh qr` to build the login link. Auto-detected from Tailscale when blank. |

## Safety

`CLAUDE_PERMISSION_MODE=bypassPermissions` lets Claude run shell commands, edit files, push to git, delete things — all without asking. It has to, because you can't see or tap a permission prompt from a phone mic. That makes network-level access control critical.

**Pick one of these access models** before you expose the server anywhere beyond localhost:

### 1. Public URL + password (default, no extra app required)

`setup.sh` generates an `AUTH_PASSWORD` for you on first run and prints it once. The PWA shows a login screen the first time a phone opens it; the browser remembers the token after that. Brute-force protection: 5 wrong guesses from one IP in 15 minutes triggers a 15-minute lockout. This is the right choice if you want to open the URL from any network — hotel Wi-Fi, your car's LTE, a friend's house — without installing anything extra.

### 2. Tailnet-only (no password, but requires Tailscale on your phone)

If your phone is already on your tailnet, you can skip the password entirely and rely on Tailscale for authentication. Swap Funnel for Serve so the server is only reachable from your own devices:

```bash
# Bind loopback only in .env:  HOST=127.0.0.1
# Then:
tailscale serve --https=443 --bg http://127.0.0.1:7878
# No `tailscale funnel` → the URL is tailnet-only.
```

Leave `AUTH_PASSWORD=` blank. The server will still print a warning at startup if it detects it's bound publicly without a password, as a backstop against a misconfiguration.

### Additional mitigations (apply to both models)

- Scope `CLAUDE_CWD` to a single project directory, not your home.
- Run the server as a dedicated OS user with no sudo and no access to unrelated repos.
- If you're not comfortable with `bypassPermissions`, set `CLAUDE_PERMISSION_MODE=default` — Claude will halt on tool use and you'll be limited to read-only style conversations (still useful for thinking-out-loud sessions).

## Roadmap

- [x] Push-to-talk voice loop with subscription billing
- [x] Per-sentence TTS streaming (start speaking mid-response)
- [x] Session continuity via `--resume`
- [x] Voice / text mode toggle
- [x] Dynamic `claude` binary discovery (no hardcoded paths)
- [x] Password auth with QR login + 30-day auto-refreshing tokens
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

**Phone says "Session expired. Please sign in again."** — your token is older than 30 days. On the server machine, run `./expose.sh qr`, rescan from the phone, done.

**Locked out — the PWA keeps rejecting the password** — you've triggered the 5-strikes lockout. Wait 15 minutes (the message tells you how long) or clear it by restarting the server. To avoid typing mistakes entirely, use the QR flow: `./expose.sh qr` prints it fresh.

**"AUTH_PASSWORD is empty" warning on startup** — either set one (`./expose.sh rotate` will generate one and restart for you) or bind to `HOST=127.0.0.1` if you're deliberately tailnet-only.

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
