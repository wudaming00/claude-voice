#!/usr/bin/env bash
# =============================================================================
# Claude Voice — expose the local server to the internet over HTTPS.
#
# Why this script exists:
#   Browsers refuse to open the microphone on any non-localhost origin unless
#   it's served over HTTPS with a publicly trusted certificate. Rather than
#   asking every user to pick a reverse proxy, provision a domain, and renew
#   certs, we wrap Tailscale Funnel — it's free, gives you a HTTPS cert for
#   a *.ts.net hostname automatically, and needs no router or firewall work.
#
# What this does:
#   1. Detect Tailscale; if missing, print the one-liner install command.
#   2. Make sure the user is logged into Tailscale.
#   3. Ensure a MagicDNS name exists (required for funnel certs).
#   4. Issue/renew the TLS cert for that name (tailscale cert).
#   5. Point Tailscale serve at the local Claude Voice port.
#   6. Enable Tailscale Funnel on 443 so the URL is reachable from the
#      public internet.
#   7. Print the final HTTPS URL the user should open on their phone.
#
# Tear-down:
#   Run:  ./expose.sh off
#   That removes the serve/funnel routes for this port but leaves Tailscale
#   itself running — you probably want that for other use cases.
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"

# Load PORT/HOST from .env if present, so the script lines up with whatever
# port run.sh is using. Default to 7878 to match backend/main.py.
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi
PORT="${PORT:-7878}"

# Terminal colors (degraded gracefully for non-interactive shells).
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

# ---------------------------------------------------------------------------
# Tear-down path. Running `./expose.sh off` should be idempotent: repeated
# calls after the routes are gone must not error out.
# ---------------------------------------------------------------------------
if [ "${1:-}" = "off" ]; then
  if ! command -v tailscale >/dev/null 2>&1; then
    warn "Tailscale isn't installed — nothing to tear down."
    exit 0
  fi
  say "Disabling funnel + serve on port 443..."
  sudo tailscale funnel --https=443 off >/dev/null 2>&1 || true
  sudo tailscale serve  --https=443 off >/dev/null 2>&1 || true
  ok "Claude Voice is no longer publicly exposed."
  exit 0
fi

# ---------------------------------------------------------------------------
# 1. Tailscale installed?
# ---------------------------------------------------------------------------
if ! command -v tailscale >/dev/null 2>&1; then
  fail "Tailscale not found."
  warn "Install it with the official one-liner:"
  warn "  curl -fsSL https://tailscale.com/install.sh | sh"
  warn "Then re-run this script."
  exit 1
fi
ok "Tailscale at $(command -v tailscale)"

# ---------------------------------------------------------------------------
# 2. Logged in?
#
# `tailscale status --json` returns a "BackendState" field we can check.
# "NeedsLogin" or "Stopped" both mean the daemon isn't usefully connected.
# jq is nice-to-have but not guaranteed to be installed, so we parse with
# a minimal Python one-liner instead (Python is already required for the
# server anyway, so this is free).
# ---------------------------------------------------------------------------
BACKEND_STATE="$(tailscale status --json 2>/dev/null | python3 -c '
import sys, json
try:
    print(json.load(sys.stdin).get("BackendState",""))
except Exception:
    print("")
' || true)"

if [ "$BACKEND_STATE" != "Running" ]; then
  warn "Tailscale isn't logged in (state: ${BACKEND_STATE:-unknown})."
  say  "Running: sudo tailscale up"
  say  "Follow the URL it prints in your browser to authenticate, then come back."
  sudo tailscale up
fi
ok "Tailscale is up"

# ---------------------------------------------------------------------------
# 3. MagicDNS name
#
# Tailscale Funnel requires the node to have a MagicDNS name (typically of
# the form machine.tailnet-abcd.ts.net). If the user's tailnet has MagicDNS
# disabled, funnel will flat-out refuse. Surface that as a clear error.
# ---------------------------------------------------------------------------
DNS_NAME="$(tailscale status --json 2>/dev/null | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    print((d.get("Self",{}).get("DNSName","") or "").rstrip("."))
except Exception:
    print("")
' || true)"

if [ -z "$DNS_NAME" ]; then
  fail "Couldn't determine this machine's MagicDNS name."
  warn "Enable MagicDNS in your Tailscale admin console (DNS tab) and re-run."
  exit 1
fi
ok "MagicDNS name: $DNS_NAME"

# ---------------------------------------------------------------------------
# 4. TLS certificate
#
# `tailscale cert <name>` provisions a Let's-Encrypt-backed cert via
# Tailscale's control plane. It's a no-op if a valid cert is already cached,
# so re-running this script is cheap.
# ---------------------------------------------------------------------------
say "Provisioning TLS cert (this is a no-op if one already exists)..."
# The cert is written to /var/lib/tailscale/certs which requires root.
sudo tailscale cert "$DNS_NAME" >/dev/null
ok "Certificate ready for $DNS_NAME"

# ---------------------------------------------------------------------------
# 5. Serve the local port + 6. enable funnel
#
# `tailscale serve` proxies HTTPS traffic on this machine to the given
# backend URL. Running it with --bg detaches so the script can exit.
# `tailscale funnel` then flips the public-exposure switch for port 443.
# ---------------------------------------------------------------------------
say "Routing https://$DNS_NAME → http://127.0.0.1:$PORT"
sudo tailscale serve --bg --https=443 "http://127.0.0.1:$PORT" >/dev/null
ok "Tailscale serve is running"

say "Opening funnel on 443 (public internet)..."
sudo tailscale funnel --bg 443 >/dev/null
ok "Funnel is open"

# ---------------------------------------------------------------------------
# Final hint.
# ---------------------------------------------------------------------------
echo
printf "%s%s" "$GREEN" "$BOLD"
cat <<EOF
Claude Voice is live at:

  https://$DNS_NAME/

Open that URL on your phone, grant microphone permission, and "Add to Home
Screen" to launch it as a PWA.

Anyone who knows this URL can reach your local Claude agent. Treat it like
a password — don't paste it in public channels.

To take it down later:
  ./expose.sh off
EOF
printf "%s\n" "$RESET"
