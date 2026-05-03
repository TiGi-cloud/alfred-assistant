#!/usr/bin/env bash
# Alfred installer — quick start for non-developers.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/TiGi-cloud/alfred-assistant/main/install.sh | bash
#   # or, after cloning:
#   ./install.sh
#
# What it does:
#   1. Verifies macOS + Python 3.11+
#   2. Installs Python dependencies into a local venv (./venv)
#   3. Installs ffmpeg + imagesnap via Homebrew if available
#   4. Reminds the user to install the Claude CLI
#   5. Launches the setup wizard at http://localhost:8080
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
cd "$SCRIPT_DIR"

bold()  { printf "\033[1m%s\033[0m\n" "$*"; }
ok()    { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn()  { printf "  \033[33m!\033[0m %s\n" "$*"; }
err()   { printf "  \033[31m✗\033[0m %s\n" "$*" >&2; exit 1; }

bold "🎩 Alfred installer"

# 1. macOS check (warn but don't fail — the bot works on Linux for non-Mac-features)
if [[ "$(uname)" != "Darwin" ]]; then
  warn "Not running on macOS — most features (screenshots, AppleScript, Vision OCR) will be unavailable."
fi

# 2. Python 3.11+ check
if ! command -v python3 >/dev/null; then
  err "python3 not found. Install Python 3.11+ from https://www.python.org/downloads/"
fi
PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
PY_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')
if (( PY_MAJOR < 3 )) || { (( PY_MAJOR == 3 )) && (( PY_MINOR < 11 )); }; then
  err "Python 3.11+ required (found ${PY_MAJOR}.${PY_MINOR})."
fi
ok "Python ${PY_MAJOR}.${PY_MINOR}"

# 3. Venv + dependencies
if [[ ! -d venv ]]; then
  python3 -m venv venv
  ok "created venv at ./venv"
fi
# shellcheck disable=SC1091
source venv/bin/activate
python3 -m pip install --upgrade pip --quiet
python3 -m pip install -r requirements.txt --quiet
ok "installed Python dependencies"

# 4. Optional macOS helpers via Homebrew
if [[ "$(uname)" == "Darwin" ]] && command -v brew >/dev/null; then
  for tool in ffmpeg imagesnap; do
    if ! command -v "$tool" >/dev/null; then
      warn "installing $tool via Homebrew…"
      brew install "$tool" >/dev/null 2>&1 || warn "  (failed to install $tool — install it manually if you need it)"
    fi
  done
  ok "macOS helpers ready"
elif [[ "$(uname)" == "Darwin" ]]; then
  warn "Homebrew not found. Install ffmpeg + imagesnap manually for /record and /camera support."
fi

# 5. Claude CLI reminder
if ! command -v claude >/dev/null; then
  warn "Claude CLI not in PATH."
  warn "  Install it from https://claude.com/claude-code, then re-run this script."
  warn "  (Alfred can't run without it.)"
fi

# 6. Launch the wizard
bold ""
bold "Launching the setup wizard…"
bold ""
exec python3 app.py --setup
