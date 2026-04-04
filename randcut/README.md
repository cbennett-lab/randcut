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
2. Hit **GENERATE**
3. Watch the status update in real time (shows which clips were randomly picked)
4. Hit **↓ DOWNLOAD VIDEO** when it's done
5. Hit **GENERATE** again for a different random combo

---

## Config Options (in main.py)

| Variable | Default | What it does |
|---|---|---|
| `DRIVE_FOLDER_LINK` | (your link) | Which Google Drive folder to pull from |
| `CLIP_DURATION` | `4` | Seconds to take from each clip |
| `NUM_CLIPS` | `3` | How many clips to randomly pick |
