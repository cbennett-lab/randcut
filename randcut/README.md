# RandCut — Random Video Clip Stitcher

Hit one button → picks 3 random videos from your Google Drive folder → grabs the first 4 seconds of each → stitches into one 12-second MP4.

---

## Before You Deploy — Two Setup Steps

### 1. Set Your Google Drive Folder Link
Open `main.py` and replace this line:
```python
DRIVE_FOLDER_LINK = "https://drive.google.com/drive/folders/YOUR_FOLDER_ID_HERE"
```
With your actual folder link. Make sure the folder is set to **"Anyone with the link can view"**.

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

# Install Python dependencies
pip install -r requirements.txt

# Set your API key
export GOOGLE_API_KEY="your_key_here"

# Start the server
uvicorn main:app --reload --port 8000

# Open in browser
open http://localhost:8000
```

---

## How to Use

1. Open your deployed URL
2. Pick a moment, a player, a seed, and a layout
3. Set **how many** you want (stepper or the 3/5/7/10 presets) and hit **ADD TO QUEUE**
4. Renders run one at a time. Change the settings and add more any time — they stack up in the same queue
5. Grab each video with **↓ MP4** as it finishes, or **↓ DOWNLOAD ALL** for a zip of the whole group
6. **■ STOP RENDER** kills the render in progress and drops everything still waiting; **CLEAR FINISHED** empties the list and deletes those files from the server

The queue lives on the server, so a page refresh picks it back up where it left off.

---

## Config Options (in main.py)

| Variable | Default | What it does |
|---|---|---|
| `DRIVE_FOLDER_LINK` | (your link) | Which Google Drive folder to pull from |
| `CLIP_DURATION` | `4` | Seconds to take from each clip |
| `NUM_CLIPS` | `3` | How many clips to randomly pick |
