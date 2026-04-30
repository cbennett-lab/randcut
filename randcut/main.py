from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, Response
from fastapi.middleware.cors import CORSMiddleware
import subprocess
import uuid
import os
import random
import re
import requests
import threading
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
PLAYER_IMAGE_CACHE = {}  # player_key -> (bytes, content_type)

TITLE_FONT_FILE = str(Path(__file__).parent / "static" / "HelveticaNeueLTProHvCn.otf")

NUM_PAIRS = 3
CROSSFADE_SEC = 2.0
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


def read_drive_json(file_id: str) -> dict:
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&key={api_key}"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return resp.json()


def folder_id_to_url(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}?usp=sharing"


def populate_stacked_categories():
    """Auto-populate STACKED_CATEGORIES and PLAYER_IMAGE_IDS from the main Drive folder structure."""
    global STACKED_CATEGORIES, PLAYER_IMAGE_IDS
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        raise ValueError("GOOGLE_API_KEY not set in Railway environment variables.")

    category_folders = list_drive_files(MAIN_DRIVE_FOLDER_ID, "application/vnd.google-apps.folder")

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

    new_categories = {}
    combo_pending = []  # processed after regular categories are known

    for cat_folder in category_folders:
        if cat_folder["name"].startswith("_"):
            continue

        cat_key = cat_folder["name"].lower().replace(" ", "_")
        cat_id = cat_folder["id"]
        cat_label = cat_folder["name"]

        subfolders = list_drive_files(cat_id, "application/vnd.google-apps.folder")
        subfolder_map = {f["name"]: f["id"] for f in subfolders}

        # A recipe.json file marks this as a combo category
        cat_all_files = list_drive_files(cat_id, "")
        recipe_file = next((f for f in cat_all_files if f["name"] == "recipe.json"), None)
        if recipe_file:
            combo_pending.append((cat_key, cat_label, recipe_file["id"], subfolder_map))
            continue

        vr_folder_id = subfolder_map.get("Gameplay")
        if not vr_folder_id:
            continue
        vr_url = folder_id_to_url(vr_folder_id)

        music_file_id = None
        if "Music" in subfolder_map:
            music_files = list_drive_files(subfolder_map["Music"], "")
            if music_files:
                music_file_id = music_files[0]["id"]

        if not music_file_id:
            continue

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
            new_categories[cat_key] = {
                "label": cat_label,
                "vr_folder": vr_url,
                "music_file": music_file_id,
                "players": players,
            }

    # Process combo categories now that all regular categories are loaded
    for cat_key, cat_label, recipe_file_id, subfolder_map in combo_pending:
        try:
            recipe = read_drive_json(recipe_file_id)
            segments = recipe.get("segments", [])

            music_file_ids = []
            if "Music" in subfolder_map:
                music_files = list_drive_files(subfolder_map["Music"], "")
                music_files.sort(key=lambda f: f["name"])
                music_file_ids = [f["id"] for f in music_files]

            if not music_file_ids:
                print(f"Warning: no music files found for combo '{cat_label}'")
                continue

            common_players = None
            valid = True
            for seg in segments:
                src_key = seg["source"].lower().replace(" ", "_")
                if src_key not in new_categories:
                    print(f"Warning: source '{seg['source']}' not found for combo '{cat_label}'")
                    valid = False
                    break
                src_players = new_categories[src_key]["players"]
                common_players = dict(src_players) if common_players is None else {
                    k: v for k, v in common_players.items() if k in src_players
                }

            if not valid or not common_players:
                continue

            new_categories[cat_key] = {
                "label": cat_label,
                "type": "combo",
                "recipe": recipe,
                "music_files": music_file_ids,
                "players": common_players,
            }
        except Exception as e:
            print(f"Warning: Could not process combo '{cat_label}': {e}")

    STACKED_CATEGORIES = new_categories


def prefetch_player_images():
    """Download all player images into memory so they serve instantly."""
    global PLAYER_IMAGE_CACHE
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    new_cache = {}
    for player_key, file_id in PLAYER_IMAGE_IDS.items():
        try:
            url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&key={api_key}"
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            new_cache[player_key] = (resp.content, resp.headers.get("content-type", "image/jpeg"))
            print(f"Cached player image: {player_key}")
        except Exception as e:
            print(f"Warning: Could not cache image for {player_key}: {e}")
    PLAYER_IMAGE_CACHE = new_cache
    print(f"Player image cache ready: {len(new_cache)} images")


def get_video_duration(file_path: Path) -> float:
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
    vr_duration = get_video_duration(vr_raw_path)
    irl_duration = get_video_duration(irl_raw_path)
    min_duration = min(vr_duration, irl_duration)

    if vr_on_top:
        filter_str = (
            f"[0:v]fps=60,trim=duration={min_duration},scale=2160:2160:force_original_aspect_ratio=increase,crop=2160:2160[vr];"
            f"[1:v]fps=60,trim=duration={min_duration},scale=2160:1680:force_original_aspect_ratio=increase,crop=2160:1680:0:0[irl];"
            "[vr][irl]vstack=inputs=2[v]"
        )
    else:
        filter_str = (
            f"[0:v]fps=60,trim=duration={min_duration},scale=2160:2160:force_original_aspect_ratio=increase,crop=2160:2160[vr];"
            f"[1:v]fps=60,trim=duration={min_duration},scale=2160:1680:force_original_aspect_ratio=increase,crop=2160:1680:0:0[irl];"
            "[irl][vr]vstack=inputs=2[v]"
        )

    cmd = [
        "ffmpeg", "-y",
        "-i", str(vr_raw_path),
        "-i", str(irl_raw_path),
        "-filter_complex", filter_str,
        "-map", "[v]",
        "-an",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16",
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


def build_crossfaded_audio(music_paths: list[Path], segment_durations: list[float], output_path: Path):
    """Crossfade multiple music tracks so each transition aligns with the segment boundary."""
    if len(music_paths) == 1:
        cmd = ["ffmpeg", "-y", "-i", str(music_paths[0]), "-c:a", "copy", str(output_path)]
        subprocess.run(cmd, check=True, capture_output=True)
        return

    cf = CROSSFADE_SEC
    inputs = []
    for p in music_paths:
        inputs += ["-i", str(p)]

    filter_parts = []
    # Trim each track. Non-last tracks get +cf seconds so acrossfade starts
    # exactly at the segment boundary (acrossfade begins cf seconds before track end).
    for i, dur in enumerate(segment_durations):
        trim_dur = dur + cf if i < len(segment_durations) - 1 else dur
        filter_parts.append(f"[{i}:a]atrim=duration={trim_dur:.3f},asetpts=PTS-STARTPTS[a{i}]")

    prev = "[a0]"
    for i in range(1, len(music_paths)):
        out = "[aout]" if i == len(music_paths) - 1 else f"[amid{i}]"
        filter_parts.append(f"{prev}[a{i}]acrossfade=d={cf}:c1=tri:c2=tri{out}")
        prev = out if i < len(music_paths) - 1 else None

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", ";".join(filter_parts),
        "-map", "[aout]",
        "-c:a", "libmp3lame", "-b:a", "192k",
        str(output_path)
    ]
    subprocess.run(cmd, check=True, capture_output=True)


TITLE_FONT_SIZE = 140

def wrap_title(text: str, frame_width: int = 2160, max_fraction: float = 0.7, font_size: int = TITLE_FONT_SIZE) -> list[str]:
    """Word-wrap text into lines fitting within max_fraction of frame width."""
    avg_char_w = font_size * 0.52
    space_w = font_size * 0.25
    max_w = frame_width * max_fraction

    words = text.split()
    lines = []
    current: list[str] = []
    width = 0.0

    for word in words:
        word_w = len(word) * avg_char_w
        gap = space_w if current else 0.0
        if current and width + gap + word_w > max_w:
            lines.append(" ".join(current))
            current = [word]
            width = word_w
        else:
            current.append(word)
            width += gap + word_w

    if current:
        lines.append(" ".join(current))

    return lines


def add_audio(video_path: Path, audio_path: Path, output_path: Path,
              title: str = "", cut_y: int = 2160):
    cmd = ["ffmpeg", "-y", "-i", str(video_path), "-f", "mp3", "-i", str(audio_path),
           "-map", "0:v:0", "-map", "1:a:0"]

    if title:
        clean_title = re.sub(r"[^\x20-\x7E]", "", title).strip()
        lines = wrap_title(clean_title)

        font_size = TITLE_FONT_SIZE
        cap_height = int(font_size * 0.72)
        line_gap = cap_height + 25
        total_h = cap_height + (len(lines) - 1) * line_gap
        first_y = cut_y - total_h // 2

        font_path = Path(TITLE_FONT_FILE)
        font_part = f"fontfile={TITLE_FONT_FILE}:" if font_path.exists() else ""

        dt_filters = []
        for i, line in enumerate(lines):
            safe = line.replace("\\", "\\\\").replace(":", "\\:")
            y = first_y + i * line_gap
            dt_filters.append(
                f"drawtext={font_part}"
                f"text={safe}:"
                f"fontsize={font_size}:"
                f"fontcolor=white:"
                f"borderw=8:"
                f"bordercolor=black:"
                f"x=(w-tw)/2:"
                f"y={y}"
            )

        cmd += ["-vf", ",".join(dt_filters), "-c:v", "libx264", "-preset", "fast", "-crf", "22"]
    else:
        cmd += ["-c:v", "copy"]

    cmd += ["-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(output_path)]
    subprocess.run(cmd, check=True, capture_output=True)


def match_irl_clip(vr_name: str, irl_files: list[dict], player_key: str) -> dict | None:
    """Find the IRL clip that matches a VR clip by the 3-digit number in the filename."""
    match = re.search(r"(\d{3})", vr_name)
    if not match:
        return None
    vr_number = match.group(1)
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

        vr_folder_id = extract_folder_id(cat["vr_folder"])
        vr_files = list_drive_files(vr_folder_id, "video/")
        if not vr_files:
            raise ValueError("No VR clips found in the gameplay folder.")

        irl_folder_id = extract_folder_id(player["irl_folder"])
        irl_files = list_drive_files(irl_folder_id, "video/")
        if not irl_files:
            raise ValueError("No IRL clips found for this player.")

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

        job_status[job_id]["message"] = "Adding music and title..."
        last_cat_word = cat["label"].split()[-1].lower()
        clip_nums = ""
        for vr, _ in chosen_pairs:
            m = re.search(r"(\d{3})", vr["name"])
            clip_nums += f"{int(m.group(1)):02d}" if m else "00"
        out_name = f"{player_key}_{last_cat_word}_{clip_nums}.mp4"
        title = cat["label"].upper() + " IN VR"
        cut_y = 2160 if vr_on_top else 1680
        add_audio(silent_video, audio_path, OUTPUT_DIR / out_name, title, cut_y)

        job_status[job_id].update({"status": "done", "message": "Ready!", "file": out_name})

    except Exception as e:
        job_status[job_id].update({"status": "error", "message": str(e)})
    finally:
        for f in temp_files:
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass


def run_combo_pipeline(job_id: str, category_key: str, player_key: str, vr_on_top: bool):
    temp_files = []
    try:
        cat = STACKED_CATEGORIES[category_key]
        segments = cat["recipe"]["segments"]

        job_status[job_id].update({"status": "working", "message": "Connecting to Google Drive..."})

        all_clips_used = []
        all_chosen_vr = []
        segment_videos = []
        segment_durations = []

        for seg_idx, segment in enumerate(segments):
            src_key = segment["source"].lower().replace(" ", "_")
            count = segment["count"]
            src_cat = STACKED_CATEGORIES[src_key]
            player = src_cat["players"][player_key]

            vr_folder_id = extract_folder_id(src_cat["vr_folder"])
            vr_files = list_drive_files(vr_folder_id, "video/")
            if not vr_files:
                raise ValueError(f"No VR clips found in '{segment['source']}'.")

            irl_folder_id = extract_folder_id(player["irl_folder"])
            irl_files = list_drive_files(irl_folder_id, "video/")
            if not irl_files:
                raise ValueError(f"No IRL clips found for {player_key} in '{segment['source']}'.")

            matched_pairs = []
            for vr in vr_files:
                irl = match_irl_clip(vr["name"], irl_files, player_key)
                if irl:
                    matched_pairs.append((vr, irl))

            if len(matched_pairs) < count:
                raise ValueError(
                    f"Only {len(matched_pairs)} matched pairs in '{segment['source']}' — need {count}."
                )

            chosen_pairs = random.sample(matched_pairs, count)
            all_clips_used += [f"{vr['name']} + {irl['name']}" for vr, irl in chosen_pairs]
            all_chosen_vr += [vr for vr, _ in chosen_pairs]

            stacked_clips = []
            for i, (vr, irl) in enumerate(chosen_pairs):
                job_status[job_id]["message"] = (
                    f"Segment {seg_idx+1}/{len(segments)} ({segment['source']}): "
                    f"downloading clip {i+1}/{count}..."
                )
                vr_raw  = TEMP_DIR / f"{job_id}_s{seg_idx}_{i}_vr.mp4"
                irl_raw = TEMP_DIR / f"{job_id}_s{seg_idx}_{i}_irl.mp4"
                stacked = TEMP_DIR / f"{job_id}_s{seg_idx}_{i}_stacked.mp4"
                temp_files += [vr_raw, irl_raw, stacked]

                download_drive_file(vr["id"], vr_raw)
                download_drive_file(irl["id"], irl_raw)

                job_status[job_id]["message"] = (
                    f"Segment {seg_idx+1}/{len(segments)} ({segment['source']}): "
                    f"processing clip {i+1}/{count}..."
                )
                stack_clips_from_raw(vr_raw, irl_raw, stacked, vr_on_top)
                stacked_clips.append(stacked)

            seg_vid = TEMP_DIR / f"{job_id}_seg{seg_idx}.mp4"
            temp_files.append(seg_vid)
            concat_clips(stacked_clips, seg_vid)
            segment_durations.append(get_video_duration(seg_vid))
            segment_videos.append(seg_vid)

        job_status[job_id]["clips_used"] = all_clips_used
        job_status[job_id]["message"] = "Stitching segments together..."
        silent_video = TEMP_DIR / f"{job_id}_silent.mp4"
        temp_files.append(silent_video)
        concat_clips(segment_videos, silent_video)

        job_status[job_id]["message"] = "Downloading music..."
        music_paths = []
        for m_idx, music_id in enumerate(cat["music_files"]):
            mp = TEMP_DIR / f"{job_id}_music{m_idx}.mp3"
            temp_files.append(mp)
            download_drive_file(music_id, mp)
            music_paths.append(mp)

        job_status[job_id]["message"] = "Building crossfaded audio..."
        audio_path = TEMP_DIR / f"{job_id}_audio.mp3"
        temp_files.append(audio_path)
        build_crossfaded_audio(music_paths, segment_durations, audio_path)

        job_status[job_id]["message"] = "Adding music and title..."
        clip_nums = ""
        for vr in all_chosen_vr:
            m = re.search(r"(\d{3})", vr["name"])
            clip_nums += f"{int(m.group(1)):02d}" if m else "00"
        recipe_name = cat["recipe"].get("name", "combo")
        out_name = f"{player_key}_{recipe_name}_{clip_nums}.mp4"
        title = cat["label"].upper() + " IN VR"
        cut_y = 2160 if vr_on_top else 1680
        add_audio(silent_video, audio_path, OUTPUT_DIR / out_name, title, cut_y)

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
        threading.Thread(target=prefetch_player_images, daemon=True).start()
    except Exception as e:
        print(f"Warning: Could not auto-populate categories from Drive: {e}")


@app.get("/stacked-categories")
async def get_stacked_categories():
    try:
        populate_stacked_categories()
        threading.Thread(target=prefetch_player_images, daemon=True).start()
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
    if player_key in PLAYER_IMAGE_CACHE:
        content, content_type = PLAYER_IMAGE_CACHE[player_key]
        return Response(content=content, media_type=content_type)
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
    cat = STACKED_CATEGORIES[category_key]
    if cat.get("type") == "combo":
        background_tasks.add_task(run_combo_pipeline, job_id, category_key, player_key, vr_on_top)
    else:
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
