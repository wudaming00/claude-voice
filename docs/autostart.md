# Running Claude Voice as a service

If you want Claude Voice to come back up on its own after a reboot, a power
blip, or an accidental `Ctrl+C`, wire it into your OS's service manager.
Three platforms covered below.

Before you start, **use Tailscale, not cloudflared quick tunnels.** The
`./start.sh` / `cloudflared` combo gives you a fresh random URL every time
it restarts — which makes auto-starting useless, since you'd have to re-scan
a new QR after every reboot. If you're setting up auto-start, you almost
certainly want the stable `https://<machine>.<tailnet>.ts.net` URL that
`./expose.sh` provisions. Do that setup once; Tailscale's own daemon keeps
the `serve` / `funnel` routes alive across reboots, so you only need a
service for the Python app itself.

---

## Linux (systemd)

Works on any distro with systemd (Debian, Ubuntu, Fedora, Arch, …) and on
WSL2 with systemd enabled — see the next section for the WSL-specific bit.

### 1. Create the unit file

Drop this into `/etc/systemd/system/claude-voice.service`, replacing
`/home/you/claude-voice` and `you` with your actual path and username:

```ini
[Unit]
Description=Claude Voice — voice interface for Claude Code
# We want the network up before we start, and we prefer Tailscale to be
# ready so the tunnel is serving the moment we bind. "Wants" is soft:
# if tailscaled isn't installed, we still start.
After=network-online.target tailscaled.service
Wants=network-online.target tailscaled.service

[Service]
Type=simple
User=you
WorkingDirectory=/home/you/claude-voice
# Source .env the same way run.sh does. systemd's EnvironmentFile parses
# KEY=VALUE lines natively, so this is equivalent.
EnvironmentFile=/home/you/claude-voice/.env
ExecStart=/home/you/claude-voice/.venv/bin/python backend/main.py
Restart=on-failure
RestartSec=3
# Don't let a runaway server eat the machine.
MemoryMax=2G

# Hardening (optional but cheap). The app doesn't need privileged access,
# and this blocks a bad-day scenario where Claude writes somewhere it
# shouldn't via bypassPermissions.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
# But it DOES need to read/write CLAUDE_CWD. Add those directories here:
ReadWritePaths=/home/you/claude-voice /home/you/claude-voice/data
# If CLAUDE_CWD is a separate project dir, list it too:
# ReadWritePaths=/home/you/claude-voice /home/you/claude-voice/data /home/you/my-project

[Install]
WantedBy=multi-user.target
```

### 2. Enable it

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now claude-voice
```

### 3. Verify + common ops

```bash
sudo systemctl status claude-voice            # is it running?
sudo journalctl -u claude-voice -f            # tail logs (Ctrl+C to exit)
sudo systemctl restart claude-voice           # e.g. after editing .env
sudo systemctl disable --now claude-voice     # stop + don't start on boot
```

### 4. The tunnel

Tailscale's `serve --bg` and `funnel --bg` flags persist to
`/var/lib/tailscale/tailscaled.state`, which is loaded at boot. So as long
as you ran `./expose.sh` once on this machine, the HTTPS URL will be live
again automatically the next time `tailscaled` starts. Nothing else to
configure.

---

## WSL2 (Windows)

Two reasonable approaches depending on whether you want the server up even
when you haven't opened a terminal.

### Option A: enable systemd in WSL, then follow the Linux section

This is the cleanest option on Windows 11 (and recent Windows 10). Edit
`/etc/wsl.conf` inside your WSL distro:

```ini
[boot]
systemd=true
```

Restart WSL (`wsl --shutdown` in PowerShell, then reopen), then follow the
[Linux (systemd)](#linux-systemd) recipe above.

Caveat: WSL only starts when a Windows process asks for it. "On boot" means
"the first time you open a WSL terminal after login" unless you also set
Windows to launch WSL at login (Task Scheduler, below).

### Option B: Windows Task Scheduler launches `start.sh`

If systemd in WSL is too invasive for you, have Windows itself kick off the
server when you log in.

Open Task Scheduler → Create Basic Task:

- **Trigger**: When I log on
- **Action**: Start a program
  - Program/script: `wsl.exe`
  - Add arguments: `-d <your-distro-name> -u <your-username> -- bash -lc "cd ~/claude-voice && ./run.sh"`
  - (Use `./run.sh`, not `./start.sh`, because `start.sh` needs a real
    foreground terminal for its log tailing; `run.sh` is happy
    backgrounded.)

You still want Tailscale for the tunnel side. Tailscale's own Windows
installer handles its auto-start.

---

## macOS (launchd)

Put a per-user agent at `~/Library/LaunchAgents/com.claudevoice.server.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.claudevoice.server</string>

  <!-- launchd doesn't parse .env files, so we lean on run.sh to source
       .env for us. Point this at the absolute path of run.sh. -->
  <key>ProgramArguments</key>
  <array>
    <string>/Users/you/claude-voice/run.sh</string>
  </array>

  <key>WorkingDirectory</key>
  <string>/Users/you/claude-voice</string>

  <key>RunAtLoad</key>
  <true/>

  <!-- Respawn if the process dies unexpectedly, but stop bouncing on
       clean exits so `launchctl unload` works normally. -->
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>

  <key>StandardOutPath</key>
  <string>/tmp/claude-voice.out.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/claude-voice.err.log</string>
</dict>
</plist>
```

Load it:

```bash
launchctl load -w ~/Library/LaunchAgents/com.claudevoice.server.plist
```

Verify + common ops:

```bash
launchctl list | grep claudevoice          # is it running?
tail -f /tmp/claude-voice.*.log            # tail logs
launchctl kickstart -k gui/$(id -u)/com.claudevoice.server   # restart
launchctl unload -w ~/Library/LaunchAgents/com.claudevoice.server.plist
```

Tailscale has a macOS app that auto-starts at login; its serve/funnel
routes persist the same way as on Linux.

---

## Things to know

- **Rotating the password** (`./expose.sh rotate`) writes a new
  `AUTH_PASSWORD` into `.env`. Services that loaded `.env` at start time
  need a restart to pick it up: `systemctl restart claude-voice` /
  `launchctl kickstart -k gui/$UID/com.claudevoice.server`. The
  `expose.sh rotate` script tries to restart a server it spawned itself,
  but it won't touch a systemd/launchd instance — it can't tell whether
  killing it is safe. Restart manually.
- **Log rotation.** `/tmp/claude-voice.*.log` are not rotated. If the
  server runs for months the files grow unbounded. Run-of-the-mill fix:
  point `StandardOutPath` / journald redirection at a location
  `logrotate` watches, or add a weekly cron `truncate -s 0`.
- **Subscription billing still applies.** A service running 24/7 won't
  spam Claude on its own (it only calls out when you speak to it), but
  remember the 5-hour Max rate window if someone else knows the password
  and tries to abuse it. The brute-force lockout in `auth.py` and the
  HMAC token expiry are your backstops.
