from fastapi import FastAPI, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import subprocess
import uuid
import os
import random
import re
import requests
from pathlib import Path

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────
# CONFIG — paste your Google Drive folder link here
# The folder must be set to "Anyone with the link can view"
DRIVE_FOLDER_LINK = "https://drive.google.com/drive/folders/1bmqiC7FCn534qzak0gv6ZKWFqvQzWrwr?usp=sharing"
NUM_CLIPS = 3       # how many clips to randomly pick
# ─────────────────────────────────────────────

OUTPUT_DIR = Path("outputs")
TEMP_DIR = Path("temp")
OUTPUT_DIR.mkdir(exist_ok=True)
TEMP_DIR.mkdir(exist_ok=True)

job_status = {}


def extract_folder_id(link: str) -> str:
    match = re.search(r"/folders/([a-zA-Z0-9_-]+)", link)
    if not match:
        raise ValueError("Could not parse folder ID from Drive link.")
    return match.group(1)


def list_drive_videos(folder_id: str) -> list[dict]:
    """List all videos in a public Google Drive folder via the Drive API."""
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        raise ValueError("GOOGLE_API_KEY environment variable not set. Add it in Railway.")

    url = "https://www.googleapis.com/drive/v3/files"
    params = {
        "q": f"'{folder_id}' in parents and mimeType contains 'video/'",
        "fields": "files(id, name)",
        "key": api_key,
        "pageSize": 200,
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    files = resp.json().get("files", [])
    if not files:
        raise ValueError("No video files found in the Drive folder.")
    return files


def download_drive_file(file_id: str, dest: Path):
    """Download a public Google Drive file by ID."""
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    session = requests.Session()
    response = session.get(url, stream=True, timeout=120)

    # Handle Google's large-file confirmation cookie
    token = None
    for key, value in response.cookies.items():
        if key.startswith("download_warning"):
            token = value
            break
    if token:
        response = session.get(url, params={"confirm": token}, stream=True, timeout=120)

    with open(dest, "wb") as f:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)


def trim_clip(input_path: Path, output_path: Path):
    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-c:v", "libx264",
        "-preset", "ultrafast",  # lowest memory usage
        "-crf", "28",            # slightly lower quality = less memory
        "-vf", "scale=1280:-2",  # cap resolution to 720p width
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        "-threads", "1",         # single thread = much less RAM
        str(output_path)
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def concat_clips(clip_paths: list[Path], output_path: Path):
    list_file = output_path.parent / f"{output_path.stem}_list.txt"
    with open(list_file, "w") as f:
        for p in clip_paths:
            f.write(f"file '{p.resolve()}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",   # just join the files, no re-encoding = very low memory
        "-movflags", "+faststart",
        str(output_path)
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    list_file.unlink(missing_ok=True)


def run_pipeline(job_id: str):
    temp_files = []
    try:
        job_status[job_id].update({"status": "working", "message": "Connecting to Google Drive..."})

        folder_id = extract_folder_id(DRIVE_FOLDER_LINK)
        all_videos = list_drive_videos(folder_id)

        if len(all_videos) < NUM_CLIPS:
            raise ValueError(f"Only {len(all_videos)} videos in folder — need at least {NUM_CLIPS}.")

        chosen = random.sample(all_videos, NUM_CLIPS)
        clip_names = [v["name"] for v in chosen]
        job_status[job_id]["clips_used"] = clip_names
        job_status[job_id]["message"] = f"Selected {NUM_CLIPS} random clips. Downloading..."

        trimmed = []
        for i, video in enumerate(chosen):
            job_status[job_id]["message"] = f"Downloading clip {i+1}/{NUM_CLIPS}: {video['name']}"
            raw = TEMP_DIR / f"{job_id}_{i}_raw.mp4"
            trimmed_path = TEMP_DIR / f"{job_id}_{i}_trim.mp4"
            temp_files += [raw, trimmed_path]

            download_drive_file(video["id"], raw)

            job_status[job_id]["message"] = f"Trimming clip {i+1}/{NUM_CLIPS}..."
            trim_clip(raw, trimmed_path)
            trimmed.append(trimmed_path)

        job_status[job_id]["message"] = "Stitching into final video..."
        out_name = f"{job_id}_final.mp4"
        concat_clips(trimmed, OUTPUT_DIR / out_name)

        job_status[job_id].update({"status": "done", "message": "Ready!", "file": out_name})

    except Exception as e:
        job_status[job_id].update({"status": "error", "message": str(e)})

    finally:
        for f in temp_files:
            f.unlink(missing_ok=True)


@app.post("/generate")
async def generate(background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())[:8]
    job_status[job_id] = {"status": "queued", "message": "Starting...", "file": None, "clips_used": []}
    background_tasks.add_task(run_pipeline, job_id)
    return {"job_id": job_id}


@app.get("/status/{job_id}")
async def get_status(job_id: str):
    return job_status.get(job_id, {"error": "Job not found"})


@app.get("/download/{filename}")
async def download(filename: str):
    path = OUTPUT_DIR / filename
    if not path.exists():
        return JSONResponse(status_code=404, content={"error": "File not found"})
    return FileResponse(path, media_type="video/mp4", filename=filename)


app.mount("/", StaticFiles(directory="static", html=True), name="static")
