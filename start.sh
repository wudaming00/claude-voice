#!/usr/bin/env bash
# =============================================================================
# Claude Voice — one-command launcher.
#
# What this does
# --------------
# A typical "I want to use this from my phone" flow used to be four steps
# across two terminals:
#   1. ./run.sh                                   # start the server
#   2. cloudflared tunnel --url http://127…       # expose it (different term)
#   3. Copy the new *.trycloudflare.com URL       # …by hand
#   4. Paste it into .env as PUBLIC_URL + ./expose.sh qr   # to print the QR
#
# Ugly. This script collapses all of it:
#   ./start.sh
#
# Does: starts the app server, starts cloudflared, parses the tunnel URL
# out of its logs, writes it to .env as PUBLIC_URL, prints the login QR,
# then parks in the foreground tailing both logs. Ctrl+C stops everything
# cleanly.
#
# If you want a stable URL instead of a random one every restart, set up
# Tailscale and use `./run.sh` + `./expose.sh` directly — that's the
# longer-term deployment story. start.sh is the "I'm testing from my
# phone right now" convenience.
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"

# ---- Env + colours --------------------------------------------------------
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi
PORT="${PORT:-7878}"

if [ -t 1 ] && command -v tput >/dev/null 2>&1 && [ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]; then
  BOLD="$(tput bold)"; RED="$(tput setaf 1)"; GREEN="$(tput setaf 2)"
  YELLOW="$(tput setaf 3)"; BLUE="$(tput setaf 4)"; RESET="$(tput sgr0)"
else
  BOLD=""; RED=""; GREEN=""; YELLOW=""; BLUE=""; RESET=""
fi
say()  { printf "%s%s%s\n" "$BLUE"  "$*" "$RESET"; }
ok()   { printf "%s✓%s %s\n" "$GREEN" "$RESET" "$*"; }
warn() { printf "%s!%s %s\n" "$YELLOW" "$RESET" "$*"; }
fail() { printf "%s✗%s %s\n" "$RED"    "$RESET" "$*"; }

SERVER_LOG="/tmp/claude-voice.server.log"
TUNNEL_LOG="/tmp/claude-voice.tunnel.log"
SERVER_PID=""
TUNNEL_PID=""

# ---- Cleanup on exit ------------------------------------------------------
# Both children are process-group leaders of their own subtrees (the venv
# Python forks uvicorn workers; cloudflared forks its quic workers). We
# kill by PID first and fall back to pkill so stragglers don't keep port
# 7878 bound on the next run.
cleanup() {
  echo
  say "Stopping..."
  if [ -n "$TUNNEL_PID" ]; then kill "$TUNNEL_PID" 2>/dev/null || true; fi
  if [ -n "$SERVER_PID" ]; then kill "$SERVER_PID" 2>/dev/null || true; fi
  # Belt-and-braces: occasionally uvicorn's workers outlive the parent.
  pkill -f "python.*backend/main.py" 2>/dev/null || true
  exit 0
}
trap cleanup INT TERM

# ---- Pre-flight -----------------------------------------------------------
if ! command -v cloudflared >/dev/null 2>&1; then
  fail "cloudflared not found."
  warn "Install it (one of):"
  warn "  Linux:  curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o ~/.local/bin/cloudflared && chmod +x ~/.local/bin/cloudflared"
  warn "  macOS:  brew install cloudflared"
  warn "Then re-run ./start.sh."
  exit 1
fi

# Bail early if something else owns the port — probably an old ./run.sh we
# forgot about. Better to say so than to silently race it.
if curl -sf -o /dev/null "http://127.0.0.1:$PORT/"; then
  fail "Something is already serving on http://127.0.0.1:$PORT/"
  warn "Stop it first, or run ./expose.sh qr if it's the Claude Voice server already."
  exit 1
fi

# ---- 1. Start the app server ---------------------------------------------
say "Starting server on :$PORT ..."
# Redirect to file so the user can still tail it; we don't read it for
# readiness — we probe the HTTP endpoint instead.
./run.sh >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

for _ in $(seq 1 60); do
  if curl -sf -o /dev/null "http://127.0.0.1:$PORT/"; then
    ok "Server up (pid $SERVER_PID)"
    break
  fi
  # If the server died during boot (e.g. port in use, venv missing), give
  # up now rather than spinning the full 30 seconds.
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    fail "Server exited during startup. Last log lines:"
    tail -20 "$SERVER_LOG"
    cleanup
  fi
  sleep 0.5
done

if ! curl -sf -o /dev/null "http://127.0.0.1:$PORT/"; then
  fail "Server didn't become reachable. See $SERVER_LOG"
  cleanup
fi

# ---- 2. Start cloudflared tunnel -----------------------------------------
# Truncate the log up-front so we don't match a URL from a previous run
# that's still sitting in the file.
: > "$TUNNEL_LOG"
say "Starting cloudflared tunnel..."
cloudflared tunnel --url "http://127.0.0.1:$PORT" --no-autoupdate >>"$TUNNEL_LOG" 2>&1 &
TUNNEL_PID=$!

# Parse the random *.trycloudflare.com URL out of cloudflared's log. The
# line format has varied between versions, so just grep the domain.
TUNNEL_URL=""
for _ in $(seq 1 60); do
  TUNNEL_URL="$(grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null | head -1 || true)"
  [ -n "$TUNNEL_URL" ] && break
  if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
    fail "cloudflared exited. Last log lines:"
    tail -20 "$TUNNEL_LOG"
    cleanup
  fi
  sleep 0.5
done

if [ -z "$TUNNEL_URL" ]; then
  fail "Couldn't read tunnel URL from $TUNNEL_LOG after 30s."
  cleanup
fi
ok "Tunnel: $TUNNEL_URL"

# Cloudflared prints the URL before the edge is actually serving it, so
# wait a beat until the tunnel is reachable from outside. Probing the
# tunnel URL itself is the cleanest confirmation.
say "Waiting for tunnel to go live..."
for _ in $(seq 1 30); do
  if curl -sf -o /dev/null "$TUNNEL_URL/"; then
    ok "Tunnel is serving requests"
    break
  fi
  sleep 1
done

# ---- 3. Persist URL to .env so expose.sh qr uses the right link ----------
if [ -f .env ]; then
  if grep -q "^PUBLIC_URL=" .env; then
    awk -v u="$TUNNEL_URL" '/^PUBLIC_URL=/{print "PUBLIC_URL=" u; next} {print}' .env > .env.tmp && mv .env.tmp .env
  else
    printf "\nPUBLIC_URL=%s\n" "$TUNNEL_URL" >> .env
  fi
  export PUBLIC_URL="$TUNNEL_URL"
  ok "Wrote PUBLIC_URL to .env"
else
  warn ".env doesn't exist. Run ./setup.sh first for a proper install."
  export PUBLIC_URL="$TUNNEL_URL"
fi

# ---- 4. Print login QR ----------------------------------------------------
echo
if [ -n "${AUTH_PASSWORD:-}" ]; then
  ./expose.sh qr
else
  warn "AUTH_PASSWORD is empty — server is open to anyone who learns the URL."
  warn "Rotate one with: ./expose.sh rotate"
  printf "\n  %sOpen URL:%s %s\n\n" "$BOLD" "$RESET" "$TUNNEL_URL"
fi

# ---- 5. Park in foreground -----------------------------------------------
cat <<EOF
${BOLD}Running.${RESET}  ${YELLOW}Ctrl+C${RESET} to stop server + tunnel.

  Server log:  $SERVER_LOG
  Tunnel log:  $TUNNEL_LOG
  Tail live :  tail -f $SERVER_LOG $TUNNEL_LOG

EOF

# ``wait`` without args blocks until any background child exits. That's
# the right semantic here: if either the server or the tunnel dies on us,
# the whole stack is broken and we should say so and clean up — no point
# staying up with half the pipeline.
wait -n
fail "A child process exited unexpectedly. Cleaning up."
cleanup
