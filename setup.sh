#!/usr/bin/env bash
# =============================================================================
# Claude Voice — first-run bootstrap script.
#
# What this does, in order:
#   1. Verifies the operating system and core tooling (Python 3.10+, Node,
#      ffmpeg) are present. Missing tools are NOT installed automatically;
#      instead we print the exact command for the user's platform. Auto-sudo
#      is intentionally avoided.
#   2. Checks that the `claude` CLI is installed and logged in. If not, we
#      give the user a one-liner to install it, then wait for them to run
#      `claude login` with their Max/Pro subscription.
#   3. Creates a Python virtual environment at .venv and installs the
#      project's dependencies from requirements.txt.
#   4. Detects whether an NVIDIA GPU with CUDA is usable (via nvidia-smi),
#      and chooses a sensible Whisper model/device/compute combination. The
#      user can still override anything in .env afterwards.
#   5. Creates .env from .env.example if it doesn't already exist, and
#      prompts for the most important knob: CLAUDE_CWD (the project Claude
#      Voice will operate in — its entire blast radius).
#   6. Pre-downloads the chosen Whisper model weights so that the first
#      voice turn doesn't stall for minutes while the user waits.
#
# Non-goals:
#   - Installing system packages. On a typical dev box the user has (or can
#     sudo-install) what's missing faster than we could safely script it.
#   - Logging into `claude` for the user. That's an interactive OAuth flow
#     we don't want to proxy through; we just detect and instruct.
# =============================================================================
set -euo pipefail

# Move to the directory containing this script so relative paths are stable
# regardless of where the user runs it from.
cd "$(dirname "$0")"

# ---------------------------------------------------------------------------
# Terminal colors. We guard on `tput` so the script still works under CI or
# other non-interactive shells that lack a terminfo database.
# ---------------------------------------------------------------------------
if [ -t 1 ] && command -v tput >/dev/null 2>&1 && [ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]; then
  BOLD="$(tput bold)"; DIM="$(tput dim)"; RED="$(tput setaf 1)"; GREEN="$(tput setaf 2)"
  YELLOW="$(tput setaf 3)"; BLUE="$(tput setaf 4)"; RESET="$(tput sgr0)"
else
  BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; BLUE=""; RESET=""
fi

say()   { printf "%s%s%s\n" "$BLUE" "$*" "$RESET"; }
ok()    { printf "%s✓%s %s\n" "$GREEN" "$RESET" "$*"; }
warn()  { printf "%s!%s %s\n" "$YELLOW" "$RESET" "$*"; }
fail()  { printf "%s✗%s %s\n" "$RED" "$RESET" "$*"; }
step()  { printf "\n%s== %s ==%s\n" "$BOLD" "$*" "$RESET"; }

# ---------------------------------------------------------------------------
# Detect the host OS family. We only need a coarse split: macOS vs. a
# Linux-like environment (including WSL, which acts like Linux for our
# tooling but has some quirks worth surfacing in error messages).
# ---------------------------------------------------------------------------
OS="$(uname -s)"
IS_WSL=0
case "$OS" in
  Darwin) PLATFORM="macos" ;;
  Linux)
    PLATFORM="linux"
    if grep -qiE "microsoft|wsl" /proc/version 2>/dev/null; then
      IS_WSL=1
      PLATFORM="wsl"
    fi
    ;;
  *)
    fail "Unsupported OS: $OS. This script targets macOS, Linux, and WSL."
    exit 1
    ;;
esac

printf "%s" "$BOLD"
cat <<'BANNER'
  ┌─────────────────────────────────────────────┐
  │  Claude Voice — setup                       │
  │  Voice interface for Claude Code (your Max) │
  └─────────────────────────────────────────────┘
BANNER
printf "%s\n" "$RESET"
say "Detected platform: $PLATFORM"

# ===========================================================================
# 1. Python 3.10+
# ===========================================================================
step "1/6  Python"

# Pick the best python binary we can find. Prefer python3.12 > 3.11 > 3.10 > 3
# so that if the user has multiple, we land on a supported one.
PY=""
for candidate in python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PY="$candidate"
    break
  fi
done

if [ -z "$PY" ]; then
  fail "No python3 found on PATH."
  case "$PLATFORM" in
    macos) warn "Install with:  brew install python@3.12" ;;
    wsl|linux) warn "Install with:  sudo apt install python3 python3-venv python3-pip" ;;
  esac
  exit 1
fi

# Extract "major.minor" and compare numerically; 3.10 is our minimum because
# we use PEP 604 syntax (X | Y) and structural pattern matching elsewhere.
PY_VERSION="$("$PY" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PY_MAJOR="${PY_VERSION%%.*}"
PY_MINOR="${PY_VERSION##*.}"
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
  fail "Python $PY_VERSION is too old. Claude Voice needs Python 3.10 or newer."
  exit 1
fi
ok "Python $PY_VERSION at $(command -v "$PY")"

# ===========================================================================
# 2. Node.js + Claude Code CLI
# ===========================================================================
step "2/6  Claude Code CLI"

CLAUDE_BIN_FOUND=""

# First, look for `claude` directly on PATH — the recommended install path.
if command -v claude >/dev/null 2>&1; then
  CLAUDE_BIN_FOUND="$(command -v claude)"
  ok "claude on PATH: $CLAUDE_BIN_FOUND"
else
  # Fall back to VS Code / Cursor extension bundles, matching the same
  # discovery order used by backend/claude_service.py so the runtime won't
  # disagree with setup about which binary it sees.
  shopt -s nullglob
  candidates=(
    "$HOME"/.vscode-server/extensions/anthropic.claude-code-*-linux-*/resources/native-binary/claude
    "$HOME"/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude
    "$HOME"/.cursor-server/extensions/anthropic.claude-code-*/resources/native-binary/claude
  )
  shopt -u nullglob
  if [ "${#candidates[@]}" -gt 0 ]; then
    # Sort descending so newest version wins; mirrors claude_service._discover_claude_bin.
    IFS=$'\n' sorted=($(printf '%s\n' "${candidates[@]}" | sort -r))
    unset IFS
    CLAUDE_BIN_FOUND="${sorted[0]}"
    ok "claude via VS Code extension: $CLAUDE_BIN_FOUND"
  fi
fi

if [ -z "$CLAUDE_BIN_FOUND" ]; then
  fail "claude CLI not found."
  warn "Install it with:"
  warn "  npm install -g @anthropic-ai/claude-code"
  warn "Then log in with your Max/Pro subscription:"
  warn "  claude login"
  warn "Re-run this script after logging in."
  exit 1
fi

# Probe auth status. Empty or 'none' means the user hasn't logged in yet —
# the runtime would spawn claude and it would immediately bail. Catch it now.
AUTH_JSON=""
if AUTH_JSON="$("$CLAUDE_BIN_FOUND" auth status --json 2>/dev/null)"; then
  AUTH_METHOD="$(printf '%s' "$AUTH_JSON" | "$PY" -c 'import sys,json
try:
    print(json.load(sys.stdin).get("authMethod",""))
except Exception:
    print("")
' 2>/dev/null || true)"
  if [ -z "$AUTH_METHOD" ] || [ "$AUTH_METHOD" = "none" ]; then
    warn "claude is installed but not logged in."
    warn "Run:  claude login   (pick 'Claude.ai account' for Max/Pro billing)"
    warn "Re-run this script afterwards."
    exit 1
  fi
  ok "claude authenticated via: $AUTH_METHOD"
else
  # Older claude CLIs may not support `auth status --json`; don't treat
  # that as fatal, just nudge the user to verify interactively.
  warn "Couldn't verify claude auth programmatically. If you haven't already,"
  warn "run 'claude login' before starting the server."
fi

# ===========================================================================
# 3. ffmpeg (phone records WebM/Opus; PyAV needs ffmpeg's libs to decode it)
# ===========================================================================
step "3/6  ffmpeg"

if command -v ffmpeg >/dev/null 2>&1; then
  ok "ffmpeg at $(command -v ffmpeg)"
else
  fail "ffmpeg not found."
  case "$PLATFORM" in
    macos) warn "Install with:  brew install ffmpeg" ;;
    wsl|linux) warn "Install with:  sudo apt install ffmpeg" ;;
  esac
  warn "ffmpeg is required to decode audio recorded by the phone browser."
  exit 1
fi

# ===========================================================================
# 4. Virtualenv + Python dependencies
# ===========================================================================
step "4/6  Python virtualenv + dependencies"

if [ ! -d ".venv" ]; then
  say "Creating .venv with $PY..."
  "$PY" -m venv .venv
  ok ".venv created"
else
  ok ".venv already exists — reusing it"
fi

# Activate inline (source) so the rest of this script uses the venv's pip.
# We don't `set -u` inside activate because virtualenv's activate script
# references unbound vars on some shells.
set +u
# shellcheck disable=SC1091
. .venv/bin/activate
set -u

say "Upgrading pip (quiet)..."
python -m pip install --upgrade pip --quiet

say "Installing requirements.txt (this can take a few minutes the first time)..."
python -m pip install -r requirements.txt --quiet
ok "Python dependencies installed"

# ===========================================================================
# 5. GPU detection + .env bootstrap
# ===========================================================================
step "5/6  GPU detection + .env"

HAS_CUDA=0
# nvidia-smi being present *and* returning zero is a reasonable proxy for
# "CUDA-capable GPU is visible to this process". On WSL2 the Windows driver
# supplies nvidia-smi via /usr/lib/wsl, and that path works for PyTorch too.
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
  HAS_CUDA=1
  GPU_NAME="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -n1 || true)"
  ok "CUDA GPU detected: ${GPU_NAME:-unknown}"
  DEFAULT_WHISPER_MODEL="large-v3"
  DEFAULT_WHISPER_DEVICE="cuda"
  DEFAULT_WHISPER_COMPUTE="float16"
else
  warn "No CUDA GPU detected — falling back to CPU inference."
  warn "That's fine, but Whisper will be slower. Consider model=small if it lags."
  DEFAULT_WHISPER_MODEL="small"
  DEFAULT_WHISPER_DEVICE="cpu"
  DEFAULT_WHISPER_COMPUTE="int8"
fi

# Only create .env if the user hasn't made one yet. Overwriting it on every
# run would stomp their carefully chosen CLAUDE_CWD.
if [ ! -f ".env" ]; then
  cp .env.example .env
  ok ".env created from .env.example"

  # Write the GPU-aware defaults straight into the new .env by replacing the
  # lines shipped in .env.example. sed -i has different semantics on macOS
  # vs GNU, so we go through a temp file to stay portable.
  awk -v model="$DEFAULT_WHISPER_MODEL" \
      -v device="$DEFAULT_WHISPER_DEVICE" \
      -v compute="$DEFAULT_WHISPER_COMPUTE" '
    /^WHISPER_MODEL=/   { print "WHISPER_MODEL=" model;   next }
    /^WHISPER_DEVICE=/  { print "WHISPER_DEVICE=" device; next }
    /^WHISPER_COMPUTE=/ { print "WHISPER_COMPUTE=" compute; next }
    { print }
  ' .env > .env.tmp && mv .env.tmp .env

  # Ask for CLAUDE_CWD interactively. This is the single most important
  # setting for safety — it bounds what Claude can touch. Defaulting to
  # $HOME would let Claude rummage through the whole account.
  echo
  say "Claude Voice will run the \`claude\` CLI inside a working directory."
  say "This is the full blast radius — Claude can read/edit/delete inside it."
  printf "%sCLAUDE_CWD%s [default: %s]: " "$BOLD" "$RESET" "$HOME"
  read -r USER_CWD || USER_CWD=""
  USER_CWD="${USER_CWD:-$HOME}"
  # Tilde isn't expanded by `read`, so do it manually if the user types ~.
  USER_CWD="${USER_CWD/#\~/$HOME}"
  if [ ! -d "$USER_CWD" ]; then
    warn "Directory does not exist: $USER_CWD"
    warn "Falling back to $HOME. You can edit CLAUDE_CWD in .env later."
    USER_CWD="$HOME"
  fi
  awk -v cwd="$USER_CWD" '
    /^CLAUDE_CWD=/ { print "CLAUDE_CWD=" cwd; next }
    { print }
  ' .env > .env.tmp && mv .env.tmp .env
  ok "CLAUDE_CWD set to: $USER_CWD"
else
  ok ".env already exists — leaving your settings alone"
fi

# ===========================================================================
# 6. Pre-download Whisper weights
# ===========================================================================
step "6/6  Whisper model pre-download"

# We source .env here so we pick up whatever the user has configured, not
# the GPU-derived defaults — they might have edited it between runs.
set +u
# shellcheck disable=SC1091
. ./.env
set -u

WHISPER_MODEL="${WHISPER_MODEL:-$DEFAULT_WHISPER_MODEL}"
WHISPER_DEVICE="${WHISPER_DEVICE:-$DEFAULT_WHISPER_DEVICE}"
WHISPER_COMPUTE="${WHISPER_COMPUTE:-$DEFAULT_WHISPER_COMPUTE}"

say "Pre-loading Whisper ($WHISPER_MODEL on $WHISPER_DEVICE)..."
say "First run downloads ~3 GB for large-v3, or ~500 MB for small. Subsequent runs are instant."

# Load the model via faster-whisper once. This populates the HuggingFace
# cache so the first real voice turn doesn't stall for minutes. We catch
# failures softly: a broken download shouldn't block the rest of setup —
# the server will redownload on startup if needed.
python - <<PY || warn "Whisper pre-download failed; server will retry on first use."
from faster_whisper import WhisperModel
try:
    WhisperModel("$WHISPER_MODEL", device="$WHISPER_DEVICE", compute_type="$WHISPER_COMPUTE")
    print("ok")
except Exception as e:
    print(f"warn: {e}")
    raise
PY
ok "Whisper ready"

# ===========================================================================
# Done
# ===========================================================================
echo
printf "%s%s" "$GREEN" "$BOLD"
cat <<'EOF'
Setup complete.

Next steps:
  1. Start the server:
       ./run.sh

  2. On the same machine, open:
       http://127.0.0.1:7878/

  3. To use it from your phone (needs HTTPS for mic access), run:
       ./expose.sh
     This will set up a public URL via Tailscale Funnel.

Edit .env at any time to change the model, voice, working directory, etc.
EOF
printf "%s\n" "$RESET"
