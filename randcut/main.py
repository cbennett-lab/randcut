from fastapi import FastAPI, BackgroundTasks, Request
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
# WOW MOMENT CATEGORIES — stacked 9:16 mode
STACKED_CATEGORIES = {
    "layups": {
        "label": "My Best Layups",
        "vr_folder":  "https://drive.google.com/drive/folders/1EsturKJfa-bSLcWQmDEinfDIwOQWBxZI?usp=sharing",
        "music_file": "17jHoahZKgt-Evz-akGEEQVogqalnLnvA",  # Drive file ID
        "players": {
            "carrington": {
                "display": "ImDominus",
                "irl_folder": "https://drive.google.com/drive/folders/1GZ2crfsLuLVyb86l722nRwkbGMXcz6GS?usp=sharing",
            }
        }
    },
    "blocks": {
        "label": "My Best Blocks",
        "vr_folder":  "https://drive.google.com/drive/folders/1RfllJXMc3q1YQENXcUyKahf8h3rMs51_?usp=sharing",
        "music_file": "1FrdufGg3vpcwUibxv5wgM9EdBayeKW4Y",
        "players": {
            "carrington": {
                "display": "ImDominus",
                "irl_folder": "https://drive.google.com/drive/folders/1gMZ22YtOd-GSFJpia2piTGeimEJCfXEg?usp=sharing",
            }
        }
    },
}

NUM_PAIRS = 3
# ─────────────────────────────────────────────

OUTPUT_DIR = Path("outputs")
TEMP_DIR   = Path("temp")
OUTPUT_DIR.mkdir(exist_ok=True)
TEMP_DIR.mkdir(exist_ok=True)

job_status = {}


def extract_folder_id(link: str) -> str:
    match = re.search(r"/folders/([a-zA-Z0-9_-]+)", link)
    if not match:
        raise ValueError(f"Could not parse folder ID from: {link}")
    return match.group(1)


def list_drive_files(folder_id: str, mime_prefix: str) -> list[dict]:
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        raise ValueError("GOOGLE_API_KEY not set in Railway environment variables.")
    url = "https://www.googleapis.com/drive/v3/files"
    params = {
        "q": f"'{folder_id}' in parents and mimeType contains '{mime_prefix}'",
        "fields": "files(id, name)",
        "key": api_key,
        "pageSize": 200,
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json().get("files", [])


def download_drive_file(file_id: str, dest: Path):
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    session = requests.Session()
    response = session.get(url, stream=True, timeout=180)
    token = None
    for key, value in response.cookies.items():
        if key.startswith("download_warning"):
            token = value
            break
    if token:
        response = session.get(url, params={"confirm": token}, stream=True, timeout=180)
    with open(dest, "wb") as f:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)


def normalize_clip(input_path: Path, output_path: Path, width: int = 720, height: int = 640):
    """Re-encode a clip to exact dimensions, no audio, portrait-ready."""
    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black",
        "-an",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-threads", "1",
        "-movflags", "+faststart",
        str(output_path)
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def stack_clips(top_path: Path, bottom_path: Path, output_path: Path):
    """Stack two clips vertically into 9:16 (720x1280)."""
    cmd = [
        "ffmpeg", "-y",
        "-i", str(top_path),
        "-i", str(bottom_path),
        "-filter_complex", "[0:v][1:v]vstack=inputs=2[v]",
        "-map", "[v]",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-threads", "1",
        "-movflags", "+faststart",
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
        "-c", "copy",
        "-movflags", "+faststart",
        str(output_path)
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    list_file.unlink(missing_ok=True)


def add_audio(video_path: Path, audio_path: Path, output_path: Path):
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-i", str(audio_path),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        "-movflags", "+faststart",
        str(output_path)
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def match_irl_clip(vr_name: str, irl_files: list[dict], player_key: str) -> dict | None:
    """Find the IRL clip that matches a VR clip by name."""
    # VR clip: layup_001.mp4 → look for carrington_layup_001.mp4
    base = Path(vr_name).stem  # e.g. layup_001
    for f in irl_files:
        if f["name"].startswith(f"{player_key}_") and base in f["name"]:
            return f
    return None


def run_stacked_pipeline(job_id: str, category_key: str, player_key: str, vr_on_top: bool):
    temp_files = []
    try:
        cat = STACKED_CATEGORIES[category_key]
        player = cat["players"][player_key]

        job_status[job_id].update({"status": "working", "message": "Connecting to Google Drive..."})

        # List VR clips
        vr_folder_id = extract_folder_id(cat["vr_folder"])
        vr_files = list_drive_files(vr_folder_id, "video/")
        if not vr_files:
            raise ValueError("No VR clips found in the gameplay folder.")

        # List IRL clips
        irl_folder_id = extract_folder_id(player["irl_folder"])
        irl_files = list_drive_files(irl_folder_id, "video/")
        if not irl_files:
            raise ValueError("No IRL clips found for this player.")

        # Find matched pairs
        matched_pairs = []
        for vr in vr_files:
            irl = match_irl_clip(vr["name"], irl_files, player_key)
            if irl:
                matched_pairs.append((vr, irl))

        if len(matched_pairs) < NUM_PAIRS:
            raise ValueError(f"Only {len(matched_pairs)} matched pairs found — need at least {NUM_PAIRS}. Check filenames match.")

        chosen_pairs = random.sample(matched_pairs, NUM_PAIRS)
        pair_names = [f"{vr['name']} + {irl['name']}" for vr, irl in chosen_pairs]
        job_status[job_id]["clips_used"] = pair_names
        job_status[job_id]["message"] = f"Found {len(matched_pairs)} pairs. Downloading {NUM_PAIRS}..."

        stacked_clips = []
        for i, (vr, irl) in enumerate(chosen_pairs):
            job_status[job_id]["message"] = f"Downloading pair {i+1}/{NUM_PAIRS}..."

            vr_raw  = TEMP_DIR / f"{job_id}_{i}_vr_raw.mp4"
            irl_raw = TEMP_DIR / f"{job_id}_{i}_irl_raw.mp4"
            vr_norm  = TEMP_DIR / f"{job_id}_{i}_vr_norm.mp4"
            irl_norm = TEMP_DIR / f"{job_id}_{i}_irl_norm.mp4"
            stacked  = TEMP_DIR / f"{job_id}_{i}_stacked.mp4"
            temp_files += [vr_raw, irl_raw, vr_norm, irl_norm, stacked]

            download_drive_file(vr["id"], vr_raw)
            download_drive_file(irl["id"], irl_raw)

            job_status[job_id]["message"] = f"Processing pair {i+1}/{NUM_PAIRS}..."
            normalize_clip(vr_raw, vr_norm)
            normalize_clip(irl_raw, irl_norm)

            top  = vr_norm  if vr_on_top else irl_norm
            bot  = irl_norm if vr_on_top else vr_norm
            stack_clips(top, bot, stacked)
            stacked_clips.append(stacked)

        job_status[job_id]["message"] = "Stitching all pairs together..."
        silent_video = TEMP_DIR / f"{job_id}_silent.mp4"
        temp_files.append(silent_video)
        concat_clips(stacked_clips, silent_video)

        job_status[job_id]["message"] = "Downloading music..."
        audio_path = TEMP_DIR / f"{job_id}_audio"
        temp_files.append(audio_path)
        download_drive_file(cat["music_file"], audio_path)

        job_status[job_id]["message"] = "Adding music..."
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


@app.get("/stacked-categories")
async def get_stacked_categories():
    result = []
    for key, cat in STACKED_CATEGORIES.items():
        players = [{"key": pk, "display": pv["display"]} for pk, pv in cat["players"].items()]
        result.append({"key": key, "label": cat["label"], "players": players})
    return result


@app.post("/generate-stacked")
async def generate_stacked(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()
    category_key = body.get("category")
    player_key   = body.get("player")
    vr_on_top    = body.get("vr_on_top", True)

    if category_key not in STACKED_CATEGORIES:
        return JSONResponse(status_code=400, content={"error": f"Unknown category: {category_key}"})
    if player_key not in STACKED_CATEGORIES[category_key]["players"]:
        return JSONResponse(status_code=400, content={"error": f"Unknown player: {player_key}"})

    job_id = str(uuid.uuid4())[:8]
    job_status[job_id] = {"status": "queued", "message": "Starting...", "file": None, "clips_used": []}
    background_tasks.add_task(run_stacked_pipeline, job_id, category_key, player_key, vr_on_top)
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
