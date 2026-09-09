#!/usr/bin/env bash
#
# Local dev launcher.  ./run.sh   (or  PORT=8001 ./run.sh)
#
# Creates .venv on first run, installs requirements when they change, then
# serves main.py with autoreload. API keys come from randcut/.env, which
# main.py loads itself — nothing is exported here.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$REPO_DIR/randcut"
VENV_DIR="$REPO_DIR/.venv"
STAMP="$VENV_DIR/.requirements-stamp"
PORT="${PORT:-8000}"

# main.py resolves outputs/, temp/ and static/ relative to the working directory
cd "$APP_DIR"

# ffmpeg does the actual rendering — every job fails without it
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg not found. Install it first:" >&2
  echo "    brew install ffmpeg" >&2
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created randcut/.env from the template."
  echo "Add your GOOGLE_API_KEY to it, then run ./run.sh again." >&2
  exit 1
fi

if ! grep -qE '^GOOGLE_API_KEY=.+' .env; then
  echo "GOOGLE_API_KEY is empty in randcut/.env — the app can't reach Drive without it." >&2
  exit 1
fi

if [ ! -d "$VENV_DIR" ]; then
  echo "Creating .venv ..."
  python3 -m venv "$VENV_DIR"
fi

# reinstall whenever requirements.txt is newer than the last install
if [ ! -f "$STAMP" ] || [ requirements.txt -nt "$STAMP" ]; then
  echo "Installing dependencies ..."
  "$VENV_DIR/bin/pip" install --quiet --upgrade pip
  "$VENV_DIR/bin/pip" install --quiet -r requirements.txt
  touch "$STAMP"
fi

echo "RandCut → http://localhost:$PORT"
exec "$VENV_DIR/bin/uvicorn" main:app --reload --port "$PORT"
