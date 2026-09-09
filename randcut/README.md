# RandCut — Random Video Clip Stitcher

Hit one button → picks 3 random videos from your Google Drive folder → grabs the first 4 seconds of each → stitches into one 12-second MP4.

---

## Before You Deploy — Two Setup Steps

### 1. Set Your Google Drive Folder
Open `main.py` and point this at your main folder:
```python
MAIN_DRIVE_FOLDER_ID = "1wsEs_t4F3SqdKtGLiYLtIUrfvIll0Ldr"
```
Make sure the folder is set to **"Anyone with the link can view"** — the app reads Drive
with an API key, not OAuth, so private folders are invisible to it.

Categories and players are discovered from the folder structure at startup, nothing is
hardcoded: each top-level folder is a category needing `Gameplay/` and `Music/`
subfolders, `IRL/<Player Name>/` gives the players, `_Character Images/<Player Name>/`
gives their avatar, and a `recipe.json` in a category makes it a combo built from other
categories. Folders starting with `_` are skipped.

### 2. Get a Google API Key (free)
The app uses Google's Drive API to list your videos. You need a free API key:

1. Go to https://console.cloud.google.com
2. Create a new project (call it anything)
3. Go to **APIs & Services → Library**
4. Search for "Google Drive API" → Enable it
5. Go to **APIs & Services → Credentials**
6. Click **Create Credentials → API Key**
7. Copy the key — you'll paste it into Railway as an environment variable

---

## Deploy to Railway (Recommended)

1. Push this folder to a GitHub repo
2. Go to https://railway.app → New Project → Deploy from GitHub
3. Select your repo
4. Go to your project → **Variables** → Add:
   ```
   GOOGLE_API_KEY = your_api_key_here
   ```
5. Railway builds and deploys automatically (~2 min)
6. Click the generated URL → you're live

---

## Run Locally

```bash
# Install FFmpeg
brew install ffmpeg        # Mac
sudo apt install ffmpeg    # Ubuntu/Linux

# Put your API key in randcut/.env  (copy the template, then fill it in)
cp randcut/.env.example randcut/.env

# Launch — creates .venv, installs deps, serves with autoreload
./run.sh                   # from the repo root; PORT=8001 ./run.sh to change port
```

Then open http://localhost:8000.

`.env` is gitignored, so your key never gets committed. `main.py` loads it at import
time and never overrides variables already in the environment — which is why the same
code picks up Railway's Variables in production with no changes.

---

## How to Use

1. Open your deployed URL
2. Pick an **influencer**, then a **video type** — only the types that influencer has clips for are listed
3. Hit **ADD TO QUEUE** for one render, or **STANDARD WEEK** for 7 spread across all of their video types
4. **Advanced** holds the first-clip seed and the gameplay top/bottom layout
5. Renders run one at a time. Change the settings and add more any time — they stack up in the same queue
6. Grab each video with **SAVE** as it finishes, or **DOWNLOAD ALL** for a zip named `YYYY-MM-DD_influencer.zip`
7. **STOP RENDER** kills the render in progress and drops everything still waiting; **CLEAR FINISHED** empties the list and deletes those files from the server

The queue lives on the server, so a page refresh picks it back up where it left off.

---

## Config Options (in main.py)

| Variable | Default | What it does |
|---|---|---|
| `MAIN_DRIVE_FOLDER_ID` | (your folder) | Which Google Drive folder to pull from |
| `NUM_PAIRS` | `3` | How many gameplay/IRL pairs go into one highlight |
| `CROSSFADE_SEC` | `2.0` | Audio crossfade between segments |
| `MAX_BATCH` | `20` | Ceiling on renders enqueued by one request |
