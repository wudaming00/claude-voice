#!/usr/bin/env bash
# =============================================================================
# Claude Voice — expose the local server to the internet over HTTPS.
#
# Why this script exists
# ----------------------
# Browsers refuse to open the microphone on any non-localhost origin unless
# it's served over HTTPS with a publicly trusted cert. Rather than asking
# every user to pick a reverse proxy, provision a domain, and renew certs,
# we wrap Tailscale Funnel — it's free, gives you a HTTPS cert for a
# *.ts.net hostname automatically, and needs no router or firewall work.
#
# What this does (default flow, no args)
# --------------------------------------
#   1. Detect Tailscale; if missing, print the one-liner install command.
#   2. Make sure the user is logged into Tailscale.
#   3. Ensure a MagicDNS name exists (required for funnel certs).
#   4. Issue/renew the TLS cert for that name (tailscale cert).
#   5. Point Tailscale Serve at the local Claude Voice port.
#   6. Enable Tailscale Funnel on 443 so the URL is reachable publicly.
#   7. Print the final HTTPS URL AND a terminal QR code containing a one-
#      tap login link so the user's phone can scan once and forget about
#      passwords for 30 days.
#
# Subcommands
# -----------
#   ./expose.sh            Set up tunnel + print URL + QR (idempotent).
#   ./expose.sh qr         Re-print the QR code without touching the tunnel.
#                          Use this to onboard a second device or when the
#                          user's phone token expired and they want to scan
#                          again without rotating the password.
#   ./expose.sh rotate     Generate a fresh AUTH_PASSWORD, restart the app
#                          server so the new password takes effect, and
#                          print a new QR. Every existing device will be
#                          logged out instantly.
#   ./expose.sh off        Take the tunnel down (leaves Tailscale running).
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"

# ---------------------------------------------------------------------------
# Env loading
#
# We source .env twice on purpose — once to pick up PORT/HOST for the tunnel
# itself, and again in any subcommand that needs AUTH_PASSWORD for the QR
# content. This keeps the flow correct even if the caller tweaked .env
# between commands.
# ---------------------------------------------------------------------------
load_env() {
  if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
  fi
}
load_env
PORT="${PORT:-7878}"

# ---------------------------------------------------------------------------
# Terminal colors (degraded gracefully for non-interactive shells).
# ---------------------------------------------------------------------------
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

# Prefer the project venv (qrcode lives there) but fall back to system
# python3 so `./expose.sh qr` still works if the user hasn't bootstrapped.
pick_python() {
  if [ -x ".venv/bin/python" ]; then
    echo ".venv/bin/python"
  else
    command -v python3 || true
  fi
}

# ---------------------------------------------------------------------------
# Public URL detection
#
# Priority order:
#   1. $PUBLIC_URL from .env — lets the user hard-code a cloudflared /
#      ngrok / custom-domain URL when not using Tailscale at all.
#   2. Tailscale MagicDNS name — the default for the normal expose flow.
# ---------------------------------------------------------------------------
detect_public_url() {
  if [ -n "${PUBLIC_URL:-}" ]; then
    # Strip trailing slash so we can predictably append ``/#login=…``.
    echo "${PUBLIC_URL%/}"
    return 0
  fi
  if command -v tailscale >/dev/null 2>&1; then
    local dns
    dns="$(tailscale status --json 2>/dev/null | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    print((d.get("Self",{}).get("DNSName","") or "").rstrip("."))
except Exception:
    print("")
' || true)"
    if [ -n "$dns" ]; then
      echo "https://$dns"
      return 0
    fi
  fi
  return 1
}

# ---------------------------------------------------------------------------
# QR printing
#
# The QR content is ``<PUBLIC_URL>/#login=<AUTH_PASSWORD>``. Putting the
# password in the URL fragment (after #) keeps it out of server access
# logs and HTTP Referer headers — the browser never sends fragments to
# servers. The client JS reads ``location.hash`` and exchanges it for a
# bearer token immediately, then wipes the fragment from the URL bar.
# ---------------------------------------------------------------------------
print_qr_for_login() {
  local url="$1"
  local pwd="$2"
  local py
  py="$(pick_python)"
  if [ -z "$py" ]; then
    warn "No python3 available — can't render a QR code. Login URL below:"
    printf "  %s%s/#login=%s%s\n\n" "$BOLD" "$url" "$pwd" "$RESET"
    return 0
  fi

  # Run qrcode's terminal renderer inline. ``invert=True`` makes the
  # dark modules use the terminal's foreground colour, which is what
  # every QR scanner expects regardless of theme. If the Python fails
  # (module missing, etc.), fall back to printing the raw login URL.
  if ! PY_URL="$url" PY_PWD="$pwd" "$py" - <<'PY'
import os, sys
try:
    import qrcode
except ImportError:
    print("[qrcode module missing — install with: pip install qrcode]", file=sys.stderr)
    sys.exit(1)
login_url = os.environ["PY_URL"] + "/#login=" + os.environ["PY_PWD"]
qr = qrcode.QRCode(border=1, error_correction=qrcode.constants.ERROR_CORRECT_M)
qr.add_data(login_url)
qr.make(fit=True)
qr.print_ascii(invert=True)
PY
  then
      warn "Could not render a QR code. Falling back to plain URL:"
      printf "  %s%s/#login=%s%s\n\n" "$BOLD" "$url" "$pwd" "$RESET"
  fi
}

# ---------------------------------------------------------------------------
# Subcommand: ./expose.sh qr
#
# Doesn't touch the tunnel; just shows the current login QR.
# ---------------------------------------------------------------------------
if [ "${1:-}" = "qr" ]; then
  load_env
  url="$(detect_public_url || true)"
  if [ -z "$url" ]; then
    fail "Couldn't determine a public URL."
    warn "Either set PUBLIC_URL in .env, or run ./expose.sh to bring up a Tailscale Funnel."
    exit 1
  fi
  if [ -z "${AUTH_PASSWORD:-}" ]; then
    fail "AUTH_PASSWORD is empty in .env."
    warn "Run ./setup.sh (first time) or ./expose.sh rotate (generate new) to create one."
    exit 1
  fi
  echo
  say "Scan to sign in on your phone (login expires after 30 days of inactivity):"
  echo
  print_qr_for_login "$url" "$AUTH_PASSWORD"
  echo
  printf "  URL: %s%s%s\n" "$BOLD" "$url" "$RESET"
  echo
  exit 0
fi

# ---------------------------------------------------------------------------
# Subcommand: ./expose.sh rotate
#
# Generate a new AUTH_PASSWORD, patch .env in place, restart the running
# server so the new password takes effect, and print the new QR. Every
# existing device is immediately logged out because the HMAC secret is
# derived from AUTH_PASSWORD.
# ---------------------------------------------------------------------------
if [ "${1:-}" = "rotate" ]; then
  py="$(pick_python)"
  if [ -z "$py" ]; then
    fail "python3 not found — can't generate a password."
    exit 1
  fi

  new_pwd="$("$py" -c 'import secrets; print(secrets.token_urlsafe(16))')"
  if [ -z "$new_pwd" ]; then
    fail "Password generation failed."
    exit 1
  fi

  if [ ! -f .env ]; then
    fail ".env doesn't exist. Run ./setup.sh first."
    exit 1
  fi

  # Patch AUTH_PASSWORD=... in place; if the line is missing, append it.
  if grep -q "^AUTH_PASSWORD=" .env; then
    awk -v pw="$new_pwd" '
      /^AUTH_PASSWORD=/ { print "AUTH_PASSWORD=" pw; next }
      { print }
    ' .env > .env.tmp && mv .env.tmp .env
  else
    printf "\nAUTH_PASSWORD=%s\n" "$new_pwd" >> .env
  fi
  ok "Rotated AUTH_PASSWORD in .env"

  # Restart the app server so Auth picks up the new secret. We only
  # restart if we find a process we started ourselves (pattern-matches
  # ``python … backend/main.py``). If the user is running it under
  # systemd / docker / a different path, we can't safely relaunch —
  # just warn and let them restart by hand.
  if pgrep -f "python.*backend/main.py" >/dev/null 2>&1; then
    say "Restarting app server so the new password takes effect..."
    pkill -f "python.*backend/main.py" 2>/dev/null || true
    sleep 1
    nohup "$py" backend/main.py > /tmp/claude-voice.log 2>&1 &
    sleep 2
    if pgrep -f "python.*backend/main.py" >/dev/null 2>&1; then
      ok "Server restarted (logs at /tmp/claude-voice.log)"
    else
      warn "Server did not come back up — inspect /tmp/claude-voice.log and run ./run.sh manually."
    fi
  else
    warn "Couldn't find a running app server. Start it with ./run.sh (or restart your systemd unit) for the new password to take effect."
  fi

  # Re-load .env so AUTH_PASSWORD reflects what we just wrote.
  load_env

  url="$(detect_public_url || true)"
  if [ -z "$url" ]; then
    warn "Public URL unknown — set PUBLIC_URL in .env or run ./expose.sh first."
    warn "New password: $new_pwd"
    exit 0
  fi

  echo
  say "Password rotated. All previously issued tokens are now invalid."
  say "Scan to sign in with the new credentials:"
  echo
  print_qr_for_login "$url" "$new_pwd"
  echo
  printf "  URL: %s%s%s\n" "$BOLD" "$url" "$RESET"
  echo
  exit 0
fi

# ---------------------------------------------------------------------------
# Subcommand: ./expose.sh off
#
# Tear-down path. Must be idempotent: repeated calls after the routes are
# gone must not error out.
# ---------------------------------------------------------------------------
if [ "${1:-}" = "off" ]; then
  if ! command -v tailscale >/dev/null 2>&1; then
    warn "Tailscale isn't installed — nothing to tear down."
    exit 0
  fi
  say "Disabling funnel + serve..."
  # Tailscale 1.70+ uses `reset`; the older `--https=443 off` form is
  # still accepted on 1.60 but we try `reset` first so either works.
  sudo tailscale funnel reset >/dev/null 2>&1 \
    || sudo tailscale funnel --https=443 off >/dev/null 2>&1 || true
  sudo tailscale serve  reset >/dev/null 2>&1 \
    || sudo tailscale serve  --https=443 off >/dev/null 2>&1 || true
  ok "Claude Voice is no longer publicly exposed."
  exit 0
fi

# ---------------------------------------------------------------------------
# Main flow: set up the Tailscale Funnel.
# ---------------------------------------------------------------------------

# 1. Tailscale installed?
if ! command -v tailscale >/dev/null 2>&1; then
  fail "Tailscale not found."
  warn "Install it with the official one-liner:"
  warn "  curl -fsSL https://tailscale.com/install.sh | sh"
  warn "Then re-run this script."
  exit 1
fi
ok "Tailscale at $(command -v tailscale)"

# 2. Logged in?
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

# 3. MagicDNS name
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

# 4. TLS certificate
say "Provisioning TLS cert (this is a no-op if one already exists)..."
sudo tailscale cert "$DNS_NAME" >/dev/null
ok "Certificate ready for $DNS_NAME"

# 5 + 6. Funnel (HTTPS on :443 → local port)
#
# Tailscale 1.70+ collapsed ``serve --https=443 <target>`` +
# ``funnel --bg 443`` into a single ``funnel --bg <target>`` form. The
# old syntax on new versions quietly mis-parses (``--https=443`` is read
# as the target, leaving the actual URL floating). We reset first so a
# prior mis-configured state is wiped cleanly, then issue the one-liner.
say "Routing https://$DNS_NAME → http://127.0.0.1:$PORT"
sudo tailscale funnel reset >/dev/null 2>&1 || true
sudo tailscale serve  reset >/dev/null 2>&1 || true
sudo tailscale funnel --bg "http://127.0.0.1:$PORT" >/dev/null
ok "Funnel is open on :443"

# ---------------------------------------------------------------------------
# Final step: print URL + login QR
# ---------------------------------------------------------------------------
PUBLIC_URL_FOR_QR="https://$DNS_NAME"
echo
printf "%s%s" "$GREEN" "$BOLD"
cat <<EOF
Claude Voice is live at:

  $PUBLIC_URL_FOR_QR/
EOF
printf "%s\n" "$RESET"

if [ -n "${AUTH_PASSWORD:-}" ]; then
  say "Scan this QR on your phone to sign in (good for 30 days of activity):"
  echo
  print_qr_for_login "$PUBLIC_URL_FOR_QR" "$AUTH_PASSWORD"
  echo
  cat <<EOF
Anyone with the QR or password can drive your Claude agent. If you leak
it, rotate: ./expose.sh rotate
EOF
else
  warn "AUTH_PASSWORD is empty in .env — anyone with this URL can reach your server."
  warn "Generate one and show a new QR with:  ./expose.sh rotate"
fi

echo
cat <<EOF
Useful commands:
  ./expose.sh qr       Re-print the QR (e.g. to onboard another device)
  ./expose.sh rotate   Rotate password, restart server, print new QR
  ./expose.sh off      Take the public tunnel down
EOF
