from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
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
# WOW MOMENT CATEGORIES — auto-populated from Drive
MAIN_DRIVE_FOLDER_ID = "1wsEs_t4F3SqdKtGLiYLtIUrfvIll0Ldr"  # Main folder
STACKED_CATEGORIES = {}
PLAYER_IMAGE_IDS = {}

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


def folder_id_to_url(folder_id: str) -> str:
    """Convert a Google Drive folder ID to a shareable folder URL."""
    return f"https://drive.google.com/drive/folders/{folder_id}?usp=sharing"


def populate_stacked_categories():
    """Auto-populate STACKED_CATEGORIES and PLAYER_IMAGE_IDS from the main Drive folder structure."""
    global STACKED_CATEGORIES, PLAYER_IMAGE_IDS
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        raise ValueError("GOOGLE_API_KEY not set in Railway environment variables.")

    # List top-level folders (categories) in the main folder
    category_folders = list_drive_files(MAIN_DRIVE_FOLDER_ID, "application/vnd.google-apps.folder")

    # Populate player images from _Character Images folder
    new_image_ids = {}
    char_folder = next((f for f in category_folders if f["name"] == "_Character Images"), None)
    if char_folder:
        player_img_folders = list_drive_files(char_folder["id"], "application/vnd.google-apps.folder")
        for pf in player_img_folders:
            player_key = pf["name"].lower().replace(" ", "_")
            files = list_drive_files(pf["id"], "image/")
            if files:
                new_image_ids[player_key] = files[0]["id"]
    PLAYER_IMAGE_IDS = new_image_ids
    
    for cat_folder in category_folders:
        # Skip folders starting with "_"
        if cat_folder["name"].startswith("_"):
            continue
        
        cat_key = cat_folder["name"].lower().replace(" ", "_")
        cat_id = cat_folder["id"]
        cat_label = cat_folder["name"]
        
        # List subfolders in this category (Gameplay, Music, IRL)
        subfolders = list_drive_files(cat_id, "application/vnd.google-apps.folder")
        subfolder_map = {f["name"]: f["id"] for f in subfolders}
        
        # Get VR folder
        vr_folder_id = subfolder_map.get("Gameplay")
        if not vr_folder_id:
            continue
        vr_url = folder_id_to_url(vr_folder_id)
        
        # Get music file (first file in Music folder)
        music_file_id = None
        if "Music" in subfolder_map:
            music_files = list_drive_files(subfolder_map["Music"], "")
            if music_files:
                music_file_id = music_files[0]["id"]
        
        if not music_file_id:
            continue
        
        # Get players from IRL folder
        players = {}
        if "IRL" in subfolder_map:
            player_folders = list_drive_files(subfolder_map["IRL"], "application/vnd.google-apps.folder")
            for pf in player_folders:
                player_key = pf["name"].lower().replace(" ", "_")
                players[player_key] = {
                    "display": pf["name"],
                    "irl_folder": folder_id_to_url(pf["id"]),
                }
        
        if players:
            STACKED_CATEGORIES[cat_key] = {
                "label": cat_label,
                "vr_folder": vr_url,
                "music_file": music_file_id,
                "players": players,
            }


def get_video_duration(file_path: Path) -> float:
    """Get duration of a video file in seconds using ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1:nokey=1",
        str(file_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return float(result.stdout.strip())


def stack_clips_from_raw(vr_raw_path: Path, irl_raw_path: Path, output_path: Path, vr_on_top: bool = True):
    """Normalize and stack two raw clips in a single encoding pass, trimmed to shorter duration."""
    # Get durations and use the shorter one
    vr_duration = get_video_duration(vr_raw_path)
    irl_duration = get_video_duration(irl_raw_path)
    min_duration = min(vr_duration, irl_duration)
    
    if vr_on_top:
        filter_str = (
            f"[0:v]fps=60,trim=duration={min_duration},scale=2160:2160:force_original_aspect_ratio=increase,crop=2160:2160[vr];"
            f"[1:v]fps=60,trim=duration={min_duration},scale=2160:1680:force_original_aspect_ratio=increase,crop=2160:1680[irl];"
            "[vr][irl]vstack=inputs=2[v]"
        )
    else:
        filter_str = (
            f"[0:v]fps=60,trim=duration={min_duration},scale=2160:2160:force_original_aspect_ratio=increase,crop=2160:2160[vr];"
            f"[1:v]fps=60,trim=duration={min_duration},scale=2160:1680:force_original_aspect_ratio=increase,crop=2160:1680[irl];"
            "[irl][vr]vstack=inputs=2[v]"
        )
    
    cmd = [
        "ffmpeg", "-y",
        "-i", str(vr_raw_path),
        "-i", str(irl_raw_path),
        "-filter_complex", filter_str,
        "-map", "[v]",
        "-an",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22",
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
        "-f", "mp3", "-i", str(audio_path),
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
    """Find the IRL clip that matches a VR clip by the 3-digit number in the filename."""
    # Extract 3-digit number from VR clip name (e.g., 001 from "layup_001.mp4")
    match = re.search(r"(\d{3})", vr_name)
    if not match:
        return None
    vr_number = match.group(1)
    
    # Find IRL clip with the same 3-digit number
    for f in irl_files:
        if vr_number in f["name"]:
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
            stacked  = TEMP_DIR / f"{job_id}_{i}_stacked.mp4"
            temp_files += [vr_raw, irl_raw, stacked]

            download_drive_file(vr["id"], vr_raw)
            download_drive_file(irl["id"], irl_raw)

            job_status[job_id]["message"] = f"Processing pair {i+1}/{NUM_PAIRS}..."
            stack_clips_from_raw(vr_raw, irl_raw, stacked, vr_on_top)
            stacked_clips.append(stacked)

        job_status[job_id]["message"] = "Stitching all pairs together..."
        silent_video = TEMP_DIR / f"{job_id}_silent.mp4"
        temp_files.append(silent_video)
        concat_clips(stacked_clips, silent_video)

        job_status[job_id]["message"] = "Downloading music..."
        audio_path = TEMP_DIR / f"{job_id}_audio.mp3"
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


@app.on_event("startup")
async def startup_event():
    """Populate categories from Drive on app startup."""
    try:
        populate_stacked_categories()
    except Exception as e:
        print(f"Warning: Could not auto-populate categories from Drive: {e}")


@app.get("/stacked-categories")
async def get_stacked_categories():
    try:
        populate_stacked_categories()
    except Exception as e:
        print(f"Warning: Could not refresh categories from Drive: {e}")
    result = []
    for key, cat in STACKED_CATEGORIES.items():
        players = [{"key": pk, "display": pv["display"]} for pk, pv in cat["players"].items()]
        result.append({"key": key, "label": cat["label"], "players": players})
    return result


@app.get("/player-image/{player_key}")
async def get_player_image(player_key: str):
    if player_key not in PLAYER_IMAGE_IDS:
        return JSONResponse(status_code=404, content={"error": "No image for this player"})
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    file_id = PLAYER_IMAGE_IDS[player_key]
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&key={api_key}"
    resp = requests.get(url, stream=True, timeout=30)
    resp.raise_for_status()
    content_type = resp.headers.get("content-type", "image/jpeg")
    return StreamingResponse(resp.iter_content(chunk_size=65536), media_type=content_type)


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
