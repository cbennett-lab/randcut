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
# CONFIG
DRIVE_FOLDER_LINK  = "https://drive.google.com/drive/folders/1bmqiC7FCn534qzak0gv6ZKWFqvQzWrwr?usp=sharing"
AUDIO_FOLDER_LINK  = "https://drive.google.com/drive/folders/1gRwAUaSpGJfT2ZbjU6A6hqD5RHIrmtNG?usp=sharing"
NUM_CLIPS = 3       # how many video clips to randomly pick
# ─────────────────────────────────────────────

OUTPUT_DIR = Path("outputs")
TEMP_DIR   = Path("temp")
OUTPUT_DIR.mkdir(exist_ok=True)
TEMP_DIR.mkdir(exist_ok=True)

job_status = {}


def extract_folder_id(link: str) -> str:
    match = re.search(r"/folders/([a-zA-Z0-9_-]+)", link)
    if not match:
        raise ValueError("Could not parse folder ID from Drive link.")
    return match.group(1)


def list_drive_files(folder_id: str, mime_prefix: str) -> list[dict]:
    """List files in a public Google Drive folder filtered by mime type prefix."""
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        raise ValueError("GOOGLE_API_KEY environment variable not set. Add it in Railway.")

    url = "https://www.googleapis.com/drive/v3/files"
    params = {
        "q": f"'{folder_id}' in parents and mimeType contains '{mime_prefix}'",
        "fields": "files(id, name)",
        "key": api_key,
        "pageSize": 200,
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    files = resp.json().get("files", [])
    return files


def download_drive_file(file_id: str, dest: Path):
    """Download a public Google Drive file by ID."""
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    session = requests.Session()
    response = session.get(url, stream=True, timeout=120)

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


def process_clip(input_path: Path, output_path: Path):
    """Re-encode a video clip to a consistent format, stripping its original audio."""
    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-an",                   # strip original audio — we'll add music later
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "28",
        "-vf", "scale=1280:-2",
        "-movflags", "+faststart",
        "-threads", "1",
        str(output_path)
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def concat_clips(clip_paths: list[Path], output_path: Path):
    """Concatenate video clips (no audio yet)."""
    list_file = output_path.parent / f"{output_path.stem}_list.txt"
    with open(list_file, "w") as f:
        for p in clip_paths:
            f.write(f"file '{p.resolve()}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        "-movflags", "+faststart",
        str(output_path)
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    list_file.unlink(missing_ok=True)


def add_audio(video_path: Path, audio_path: Path, output_path: Path):
    """Mix a random audio track over the full video, trimming audio to video length."""
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-i", str(audio_path),
        "-map", "0:v:0",         # video from clip
        "-map", "1:a:0",         # audio from music track
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",             # trim audio to match video length
        "-movflags", "+faststart",
        str(output_path)
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def run_pipeline(job_id: str):
    temp_files = []
    try:
        job_status[job_id].update({"status": "working", "message": "Connecting to Google Drive..."})

        # --- Videos ---
        video_folder_id = extract_folder_id(DRIVE_FOLDER_LINK)
        all_videos = list_drive_files(video_folder_id, "video/")
        if not all_videos:
            raise ValueError("No video files found in the video Drive folder.")
        if len(all_videos) < NUM_CLIPS:
            raise ValueError(f"Only {len(all_videos)} videos in folder — need at least {NUM_CLIPS}.")

        # --- Audio ---
        audio_folder_id = extract_folder_id(AUDIO_FOLDER_LINK)
        all_audio = list_drive_files(audio_folder_id, "audio/")
        if not all_audio:
            raise ValueError("No audio files found in the audio Drive folder.")

        chosen_videos = random.sample(all_videos, NUM_CLIPS)
        chosen_audio  = random.choice(all_audio)

        clip_names = [v["name"] for v in chosen_videos]
        job_status[job_id]["clips_used"] = clip_names
        job_status[job_id]["message"] = f"Selected {NUM_CLIPS} clips + audio: {chosen_audio['name']}"

        # --- Download & process video clips ---
        processed = []
        for i, video in enumerate(chosen_videos):
            job_status[job_id]["message"] = f"Downloading clip {i+1}/{NUM_CLIPS}: {video['name']}"
            raw  = TEMP_DIR / f"{job_id}_{i}_raw.mp4"
            proc = TEMP_DIR / f"{job_id}_{i}_proc.mp4"
            temp_files += [raw, proc]

            download_drive_file(video["id"], raw)

            job_status[job_id]["message"] = f"Processing clip {i+1}/{NUM_CLIPS}..."
            process_clip(raw, proc)
            processed.append(proc)

        # --- Stitch clips together ---
        job_status[job_id]["message"] = "Stitching clips together..."
        silent_video = TEMP_DIR / f"{job_id}_silent.mp4"
        temp_files.append(silent_video)
        concat_clips(processed, silent_video)

        # --- Download audio ---
        job_status[job_id]["message"] = f"Downloading audio: {chosen_audio['name']}..."
        audio_path = TEMP_DIR / f"{job_id}_audio"
        temp_files.append(audio_path)
        download_drive_file(chosen_audio["id"], audio_path)

        # --- Mix audio over video ---
        job_status[job_id]["message"] = "Adding audio track..."
        out_name = f"{job_id}_final.mp4"
        add_audio(silent_video, audio_path, OUTPUT_DIR / out_name)

        job_status[job_id].update({"status": "done", "message": "Ready!", "file": out_name})

    except Exception as e:
        job_status[job_id].update({"status": "error", "message": str(e)})

    finally:
        for f in temp_files:
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass


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
