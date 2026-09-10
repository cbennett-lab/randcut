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

# SECRET_KEY signs session cookies and encrypts saved credentials, so it's
# needed in every mode. Generate one on first run rather than making it a chore.
if ! grep -qE '^SECRET_KEY=.+' .env; then
  echo "Generating SECRET_KEY in randcut/.env ..."
  python3 - <<'PY'
import pathlib, re, secrets
p = pathlib.Path(".env")
text = p.read_text()
line = "SECRET_KEY=" + secrets.token_urlsafe(48)
if re.search(r"(?m)^SECRET_KEY=.*$", text):
    text = re.sub(r"(?m)^SECRET_KEY=.*$", line, text)
else:
    text = text.rstrip("\n") + "\n" + line + "\n"
p.write_text(text)
PY
fi

# Locally we run with the login gate off and read Drive with the API key.
# Set the OAuth vars instead to exercise the real sign-in flow.
if grep -qE '^DEV_NO_AUTH=1' .env; then
  if ! grep -qE '^GOOGLE_API_KEY=.+' .env; then
    echo "GOOGLE_API_KEY is empty in randcut/.env — with DEV_NO_AUTH=1 the app reads Drive" >&2
    echo "with that key. Add it, or configure the OAuth vars and unset DEV_NO_AUTH." >&2
    exit 1
  fi
  echo "note: DEV_NO_AUTH=1 — login gate is OFF (local only)"
elif ! grep -qE '^SECRET_KEY=.+' .env || ! grep -qE '^GOOGLE_CLIENT_ID=.+' .env; then
  echo "Sign-in needs GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and SECRET_KEY in randcut/.env" >&2
  echo "(or set DEV_NO_AUTH=1 to skip the gate locally)." >&2
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
