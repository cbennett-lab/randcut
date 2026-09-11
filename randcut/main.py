from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import (FileResponse, JSONResponse, RedirectResponse,
                               StreamingResponse, Response)
from fastapi.middleware.cors import CORSMiddleware
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
import subprocess
import uuid
import os
import random
import re
import secrets
import shutil
from urllib.parse import quote, urlsplit
import requests
import threading
import queue
import time
import zipfile
from pathlib import Path

# Local dev reads keys from randcut/.env. On Railway the variables are already
# in the environment and win — load_dotenv never overwrites what's set.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass  # not installed in prod images built before dotenv was added

# Imported *after* load_dotenv: auth reads its config at module level, so it
# would otherwise never see anything from .env.
import auth  # noqa: E402
import buffer_api  # noqa: E402

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

# ─────────────────────────────────────────────
# RENDER QUEUE
# Jobs run one at a time on a single worker thread — Railway's memory ceiling
# can't handle two ffmpeg passes at once.
MAX_BATCH = 20

# Cookies go Secure in production; plain http://localhost would reject them.
REQUIRE_SECURE_COOKIES = os.environ.get("OAUTH_REDIRECT_URI", "").startswith("https://")

WORK_QUEUE: "queue.Queue[str]" = queue.Queue()
JOB_ORDER: list[str] = []          # submission order, drives the UI list
QUEUE_LOCK = threading.RLock()
CANCELLED: set[str] = set()        # job_ids the user asked to stop
CURRENT = {"job_id": None, "proc": None}   # job + ffmpeg process running right now


class JobCancelled(Exception):
    """Raised inside a pipeline when the user cancels the job mid-render."""


def check_cancel():
    job_id = CURRENT["job_id"]
    if job_id is not None and job_id in CANCELLED:
        raise JobCancelled()


def run_ffmpeg(cmd: list[str]):
    """Run an ffmpeg/ffprobe command so it can be killed by a cancel request."""
    check_cancel()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with QUEUE_LOCK:
        CURRENT["proc"] = proc
    try:
        out, err = proc.communicate()
    finally:
        with QUEUE_LOCK:
            CURRENT["proc"] = None
    if proc.returncode != 0:
        check_cancel()  # a non-zero exit because we killed it is a cancel, not a failure
        raise subprocess.CalledProcessError(proc.returncode, cmd, out, err)
    return out


def kill_current_proc():
    with QUEUE_LOCK:
        proc = CURRENT["proc"]
    if proc and proc.poll() is None:
        try:
            proc.kill()
        except Exception:
            pass
# ─────────────────────────────────────────────


def extract_folder_id(link: str) -> str:
    match = re.search(r"/folders/([a-zA-Z0-9_-]+)", link)
    if not match:
        raise ValueError(f"Could not parse folder ID from: {link}")
    return match.group(1)


def list_drive_files(folder_id: str, mime_prefix: str) -> list[dict]:
    extra, headers = auth.drive_auth()
    url = "https://www.googleapis.com/drive/v3/files"
    params = {
        "q": f"'{folder_id}' in parents and mimeType contains '{mime_prefix}'",
        "fields": "files(id, name)",
        "pageSize": 200,
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
        **extra,
    }
    resp = requests.get(url, params=params, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json().get("files", [])


def download_drive_file(file_id: str, dest: Path):
    extra, headers = auth.drive_auth()
    # the REST media endpoint works for private files and skips the old
    # drive.google.com/uc interstitial-cookie dance
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}"
    params = {"alt": "media", "supportsAllDrives": "true", **extra}
    with requests.get(url, params=params, headers=headers, stream=True, timeout=180) as response:
        response.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                check_cancel()
                if chunk:
                    f.write(chunk)


def read_drive_json(file_id: str) -> dict:
    extra, headers = auth.drive_auth()
    resp = requests.get(f"https://www.googleapis.com/drive/v3/files/{file_id}",
                        params={"alt": "media", **extra}, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


def read_drive_text(file_id: str) -> str:
    """Fetch a Drive text file as UTF-8.

    Decoded explicitly rather than via resp.text: requests guesses latin-1 for
    text/* without a charset, which turns every emoji into mojibake.
    """
    extra, headers = auth.drive_auth()
    resp = requests.get(f"https://www.googleapis.com/drive/v3/files/{file_id}",
                        params={"alt": "media", **extra}, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.content.decode("utf-8-sig")      # -sig strips a BOM if Sheets/Notepad added one


def folder_id_to_url(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}?usp=sharing"


def populate_stacked_categories():
    """Auto-populate STACKED_CATEGORIES and PLAYER_IMAGE_IDS from the main Drive folder structure."""
    global STACKED_CATEGORIES, PLAYER_IMAGE_IDS
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
                "captions_folder": subfolder_map.get("Captions"),
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
                "captions_folder": subfolder_map.get("Captions"),
                "players": common_players,
            }
        except Exception as e:
            print(f"Warning: Could not process combo '{cat_label}': {e}")

    STACKED_CATEGORIES = new_categories


# Captions live in <Category>/Captions/<a text file>, one caption per line.
_CAPTION_CACHE: dict[str, tuple[float, list[str]]] = {}
CAPTION_TTL = 10 * 60


def caption_lines(category_key: str) -> list[dict]:
    """Every caption for a category, each tagged with the file it came from.

    Read strictly from *this* category's own Captions folder — a combo like
    "My Best Layups and Blocks in VR" takes clips from other categories but its
    captions are its own, never inherited from a source.
    """
    cat = STACKED_CATEGORIES.get(category_key) or {}
    folder_id = cat.get("captions_folder")
    if not folder_id:
        raise ValueError(f"No Captions folder in '{cat.get('label', category_key)}' on Drive. "
                         f"Add one with a text file of captions, one per line.")

    hit = _CAPTION_CACHE.get(category_key)
    if hit and time.time() - hit[0] < CAPTION_TTL:
        return hit[1]

    files = sorted(list_drive_files(folder_id, "text/"), key=lambda f: f["name"])
    if not files:
        raise ValueError(f"No text files in the Captions folder for "
                         f"'{cat.get('label', category_key)}'.")

    pool: list[dict] = []
    for f in files:                       # every text file in the folder contributes
        try:
            for line in read_drive_text(f["id"]).splitlines():
                line = line.strip()
                if line:
                    pool.append({"text": line, "file": f["name"]})
        except Exception as e:
            print(f"Warning: could not read caption file {f['name']}: {e}")
    if not pool:
        raise ValueError(f"No caption lines found in the Captions folder for "
                         f"'{cat.get('label', category_key)}'.")
    _CAPTION_CACHE[category_key] = (time.time(), pool)
    return pool


def caption_sources(category_key: str) -> list[str]:
    """Which files a category's captions came from — shown at review time so a
    stray file in the folder is visible rather than mysterious."""
    try:
        return sorted({c["file"] for c in caption_lines(category_key)})
    except Exception:
        return []


def random_caption(category_key: str) -> dict:
    """One caption, with the file it came from."""
    return random.choice(caption_lines(category_key))


def prefetch_player_images():
    """Download all player images into memory so they serve instantly."""
    global PLAYER_IMAGE_CACHE
    extra, headers = auth.drive_auth()
    new_cache = {}
    for player_key, file_id in PLAYER_IMAGE_IDS.items():
        try:
            url = f"https://www.googleapis.com/drive/v3/files/{file_id}"
            resp = requests.get(url, params={"alt": "media", **extra}, headers=headers, timeout=30)
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
    return float(run_ffmpeg(cmd).decode().strip())


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
    run_ffmpeg(cmd)


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
    run_ffmpeg(cmd)
    list_file.unlink(missing_ok=True)


def build_crossfaded_audio(music_paths: list[Path], segment_durations: list[float], output_path: Path):
    """Crossfade multiple music tracks so each transition aligns with the segment boundary."""
    if len(music_paths) == 1:
        cmd = ["ffmpeg", "-y", "-i", str(music_paths[0]), "-c:a", "copy", str(output_path)]
        run_ffmpeg(cmd)
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
    run_ffmpeg(cmd)


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

        cmd += ["-vf", ",".join(dt_filters), "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22", "-threads", "1"]
    else:
        cmd += ["-c:v", "copy"]

    cmd += ["-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(output_path)]
    run_ffmpeg(cmd)


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


def clip_number(name: str) -> int | None:
    match = re.search(r"(\d{3})", name)
    return int(match.group(1)) if match else None


def pick_pairs(matched_pairs: list[tuple], count: int, seed_clip: int | None = None,
               avoid_first: set[int] | None = None) -> list[tuple]:
    """Choose `count` (vr, irl) pairs.

    A given seed_clip forces that pair first. Otherwise the first pair avoids any
    clip number in `avoid_first` — the first clips other renders of the same
    type/player already used — so two videos from one Standard Week can't open on
    the same clip, and therefore can't be the same video. If every clip has been
    used the constraint is dropped rather than failing.
    """
    if seed_clip is None:
        avoid = avoid_first or set()
        fresh = [p for p in matched_pairs if clip_number(p[0]["name"]) not in avoid]
        pool = fresh or matched_pairs           # all used up: start over rather than fail
        first = random.choice(pool)
        remaining = [p for p in matched_pairs if p is not first]
        rest = random.sample(remaining, count - 1) if count > 1 else []
        return [first] + rest

    first = next((pair for pair in matched_pairs if clip_number(pair[0]["name"]) == seed_clip), None)
    if first is None:
        raise ValueError(f"No clip found matching seed number {seed_clip}.")

    remaining = [pair for pair in matched_pairs if pair is not first]
    rest = random.sample(remaining, count - 1) if count > 1 else []
    return [first] + rest


def first_clips_in_flight(category_key: str, player_key: str, exclude_job: str) -> set[int]:
    """First clip numbers already taken by other renders of the same type+player.

    Scoped to the current queue, which is exactly the batch a Standard Week makes.
    The worker is serial, so by the time a job runs the earlier ones have recorded
    what they used.
    """
    used = set()
    with QUEUE_LOCK:
        jobs = [job_status[j] for j in JOB_ORDER if j in job_status]
    for job in jobs:
        if job["job_id"] == exclude_job or job.get("category") != category_key:
            continue
        if job.get("player") != player_key or not job.get("clips_used"):
            continue
        n = clip_number(job["clips_used"][0])    # entries are "<vr> + <irl>", first is the opener
        if n is not None:
            used.add(n)
    return used


def run_stacked_pipeline(job_id: str, category_key: str, player_key: str, vr_on_top: bool, seed_clip: int | None = None):
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

        chosen_pairs = pick_pairs(matched_pairs, NUM_PAIRS, seed_clip,
                                  first_clips_in_flight(category_key, player_key, job_id))
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
        pretty_name = f"{player_key}_{last_cat_word}_{clip_nums}.mp4"
        out_name = f"{job_id}_{pretty_name}"  # job_id keeps queued renders from colliding on disk
        title = cat["label"].upper() + " IN VR"
        cut_y = 2160 if vr_on_top else 1680
        # Render to temp first so a cancelled job never leaves a half-written file in outputs/
        final_tmp = TEMP_DIR / out_name
        temp_files.append(final_tmp)
        add_audio(silent_video, audio_path, final_tmp, title, cut_y)
        os.replace(final_tmp, OUTPUT_DIR / out_name)

        job_status[job_id].update({
            "status": "done", "message": "Ready!",
            "file": out_name, "download_name": pretty_name,
        })

    except JobCancelled:
        job_status[job_id].update({"status": "cancelled", "message": "Stopped."})
    except Exception as e:
        job_status[job_id].update({"status": "error", "message": str(e)})
    finally:
        job_status[job_id]["finished_at"] = time.time()
        for f in temp_files:
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass


def run_combo_pipeline(job_id: str, category_key: str, player_key: str, vr_on_top: bool, seed_clip: int | None = None):
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

            chosen_pairs = pick_pairs(
                matched_pairs, count,
                seed_clip if seg_idx == 0 else None,
                first_clips_in_flight(src_key, player_key, job_id) if seg_idx == 0 else None)
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
        pretty_name = f"{player_key}_{recipe_name}_{clip_nums}.mp4"
        out_name = f"{job_id}_{pretty_name}"  # job_id keeps queued renders from colliding on disk
        title = cat["label"].upper() + " IN VR"
        cut_y = 2160 if vr_on_top else 1680
        # Render to temp first so a cancelled job never leaves a half-written file in outputs/
        final_tmp = TEMP_DIR / out_name
        temp_files.append(final_tmp)
        add_audio(silent_video, audio_path, final_tmp, title, cut_y)
        os.replace(final_tmp, OUTPUT_DIR / out_name)

        job_status[job_id].update({
            "status": "done", "message": "Ready!",
            "file": out_name, "download_name": pretty_name,
        })

    except JobCancelled:
        job_status[job_id].update({"status": "cancelled", "message": "Stopped."})
    except Exception as e:
        job_status[job_id].update({"status": "error", "message": str(e)})
    finally:
        job_status[job_id]["finished_at"] = time.time()
        for f in temp_files:
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass


def render_worker():
    """Single worker thread — pulls job ids off the queue and renders them one at a time."""
    while True:
        job_id = WORK_QUEUE.get()
        try:
            job = job_status.get(job_id)
            if job is None:
                continue

            with QUEUE_LOCK:
                if job_id in CANCELLED:
                    job.update({"status": "cancelled", "message": "Stopped before it started.",
                                "finished_at": time.time()})
                    continue
                CURRENT["job_id"] = job_id
                CURRENT["proc"] = None

            job["started_at"] = time.time()
            try:
                cat = STACKED_CATEGORIES.get(job["category"], {})
                pipeline = run_combo_pipeline if cat.get("type") == "combo" else run_stacked_pipeline
                pipeline(job_id, job["category"], job["player"], job["vr_on_top"], job["seed"])
            except Exception as e:  # pipelines handle their own errors; this is a backstop
                job.update({"status": "error", "message": str(e), "finished_at": time.time()})
            finally:
                with QUEUE_LOCK:
                    CURRENT["job_id"] = None
                    CURRENT["proc"] = None
        finally:
            WORK_QUEUE.task_done()


# ─────────────────────────────────────────────
# STANDARD WEEK
# Blocks and Layups pull best, so they get two each and everything else one —
# which lands on 7 exactly when an influencer has all five types.
STANDARD_WEEK_SIZE = 7
WEEK_PRIORITY_COUNT = 2
# Matched as whole names, not substrings: "My Best Layups and Blocks" is its own
# video type and must not count as Layups or Blocks. Comma-separated; folder name
# or category key, punctuation and case ignored.
WEEK_PRIORITY = os.environ.get("WEEK_PRIORITY", "My Best Blocks,My Best Layups")


def _norm_type(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


WEEK_PRIORITY_NAMES = {_norm_type(n) for n in WEEK_PRIORITY.split(",") if n.strip()}


def _is_priority(cat_key: str, label: str) -> bool:
    return bool(WEEK_PRIORITY_NAMES & {_norm_type(cat_key), _norm_type(label)})


def standard_week_counts(types: list[dict], size: int = STANDARD_WEEK_SIZE) -> dict:
    """How many of each video type a Standard Week should contain.

    Priority types get two, everything else one. Short of `size`, top up at
    random; over it, trim the extras (non-priority first) so the week stays at
    `size`.
    """
    if not types:
        return {}
    priority = [t for t in types if _is_priority(t["key"], t.get("label", ""))]
    others = [t for t in types if t not in priority]

    counts = {t["key"]: 0 for t in types}
    # order the singles randomly so the same types aren't dropped every week
    shuffled_others = others[:]
    random.shuffle(shuffled_others)
    slots = ([t for t in priority for _ in range(WEEK_PRIORITY_COUNT)] + shuffled_others)

    for t in slots[:size]:
        counts[t["key"]] += 1

    total = sum(counts.values())
    while total < size:
        # pad the least-used type so a short roster stays balanced (2 types -> 4/3,
        # not 5/2) — which is also what keeps the week spaceable
        fewest = min(counts.values())
        candidates = [k for k, v in counts.items() if v == fewest]
        counts[random.choice(candidates)] += 1
        total += 1
    return {k: v for k, v in counts.items() if v}


def spread_week(counts: dict) -> list[str]:
    """Order the week so the same type never lands back-to-back.

    Renders reach the post queue in this order, so spacing here is what keeps two
    Blocks from publishing one after the other. Takes the most-remaining type each
    time, skipping whatever was just used.
    """
    remaining = dict(counts)
    out: list[str] = []
    while sum(remaining.values()) > 0:
        options = [k for k, v in remaining.items() if v > 0 and (not out or k != out[-1])]
        if not options:                      # only the previous type is left
            options = [k for k, v in remaining.items() if v > 0]
        random.shuffle(options)              # break ties differently each week
        pick = max(options, key=lambda k: remaining[k])
        out.append(pick)
        remaining[pick] -= 1
    return out


def enqueue_job(category_key: str, player_key: str, vr_on_top: bool, seed_clip: int | None) -> str:
    cat = STACKED_CATEGORIES[category_key]
    job_id = str(uuid.uuid4())[:8]
    with QUEUE_LOCK:
        job_status[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "message": "Waiting in queue...",
            "file": None,
            "download_name": None,
            "clips_used": [],
            "category": category_key,
            "category_label": cat["label"],
            "player": player_key,
            "player_label": cat["players"][player_key]["display"],
            "vr_on_top": vr_on_top,
            "seed": seed_clip,
            "created_at": time.time(),
            "post_status": None,        # None | posting | posted | partial | error
            "post_message": None,
            "post_results": [],
            "post_id": None,
            "media_token": None,
        }
        JOB_ORDER.append(job_id)
    WORK_QUEUE.put(job_id)
    return job_id


def queue_snapshot() -> dict:
    with QUEUE_LOCK:
        jobs = [job_status[j] for j in JOB_ORDER if j in job_status]
        running = CURRENT["job_id"]
    pending = sum(1 for j in jobs if j["status"] == "queued")
    return {
        "jobs": jobs,
        "running": running,
        "pending": pending,
        "done": sum(1 for j in jobs if j["status"] == "done"),
        "active": running is not None or pending > 0,
    }


# ─────────────────────────────────────────────
# PUBLIC MEDIA
# Buffer has no upload API: every asset must sit at a public, unauthenticated,
# stable HTTPS URL, and stay there until the post publishes (days later for a
# queued post). So a finished render is copied to a public directory and served
# by an unguessable token from a route that sits outside the login gate.
#
# It lives under the state dir because that's the Railway volume — outputs/ is
# ephemeral and a redeploy would leave Buffer fetching a 404 at publish time.
PUBLIC_MEDIA_DIR = auth.STATE_DIR / "public_media"
MEDIA_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
MEDIA_RETENTION_DAYS = int(os.environ.get("MEDIA_RETENTION_DAYS", "30"))


def public_base_url(request: Request) -> str:
    """Origin Buffer should fetch media from."""
    explicit = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    redirect = os.environ.get("OAUTH_REDIRECT_URI", "").strip()
    if redirect:
        parts = urlsplit(redirect)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    return str(request.base_url).rstrip("/")


def sweep_public_media():
    """Drop published media older than the retention window."""
    if not PUBLIC_MEDIA_DIR.exists():
        return
    cutoff = time.time() - MEDIA_RETENTION_DAYS * 86400
    for f in PUBLIC_MEDIA_DIR.glob("*.mp4"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


# Masters go to Buffer as-is. A downscale is available but OFF by default: it was
# added while chasing "Video could not be read from its URL", which turned out to
# be a HEAD 404 on the media route, not the file. Set POST_TRANSCODE=1 to turn it
# back on if Buffer ever struggles with an ~80MB fetch.
POST_TRANSCODE = os.environ.get("POST_TRANSCODE") == "1"
DELIVERY_WIDTH = 1080
DELIVERY_HEIGHT = 1920
DELIVERY_CRF = "23"
DELIVERY_MAXRATE = "8M"


def make_delivery_copy(src: Path, dest: Path):
    """Transcode a render to a social-delivery MP4."""
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vf", f"scale={DELIVERY_WIDTH}:{DELIVERY_HEIGHT}:force_original_aspect_ratio=decrease,"
               f"pad={DELIVERY_WIDTH}:{DELIVERY_HEIGHT}:(ow-iw)/2:(oh-ih)/2",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", DELIVERY_CRF,
        "-maxrate", DELIVERY_MAXRATE, "-bufsize", "16M",
        "-pix_fmt", "yuv420p",          # some networks reject anything else
        "-r", "30",
        "-c:a", "aac", "-b:a", "128k", "-ac", "2",
        # the destination is a .part temp file, so the container can't be
        # inferred from the extension — state it outright
        "-f", "mp4",
        # moov atom up front, so a fetcher can read the header without the whole file
        "-movflags", "+faststart",
        "-threads", "1",                # keep well clear of the render worker's memory
        str(dest),
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise ValueError("Could not prepare the video for posting: "
                         + proc.stderr.decode(errors="replace")[-300:])


def publish_media(job: dict) -> str:
    """Put a copy of a finished render where Buffer can fetch it."""
    if job.get("media_token"):
        existing = PUBLIC_MEDIA_DIR / f"{job['media_token']}.mp4"
        if existing.exists():
            return job["media_token"]          # already published, reuse the URL

    src = OUTPUT_DIR / (job.get("file") or "")
    if not job.get("file") or not src.exists():
        raise ValueError("The rendered file is no longer on the server — re-render it first.")

    PUBLIC_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    dest = PUBLIC_MEDIA_DIR / f"{token}.mp4"
    tmp = dest.with_suffix(".part")
    try:
        if POST_TRANSCODE:
            make_delivery_copy(src, tmp)
        else:
            shutil.copyfile(src, tmp)          # the master already has +faststart
        tmp.replace(dest)                      # atomic, so Buffer never sees a partial file
    finally:
        tmp.unlink(missing_ok=True)
    job["media_token"] = token
    sweep_public_media()
    return token


# ─────────────────────────────────────────────
# POST QUEUE
# Renders are staged here for review before anything reaches Buffer — the
# caption, the handles and the video are all resolved at staging time so the
# Posting tab shows exactly what will be sent.
#
# Persisted to the credential store (i.e. the Railway volume) because review
# happens minutes or hours after the render, possibly across a redeploy.
POST_QUEUE_SETTING = "post_queue"
POST_QUEUE: list[dict] = []
POST_QUEUE_LOCK = threading.RLock()


def load_post_queue():
    global POST_QUEUE
    try:
        POST_QUEUE = list(auth.get_setting(POST_QUEUE_SETTING, []) or [])
    except Exception as e:
        print(f"Warning: could not read the post queue ({e}); starting empty.")
        POST_QUEUE = []


def persist_post_queue():
    """Best effort: without SECRET_KEY the queue still works, just in memory."""
    try:
        auth.save_setting(POST_QUEUE_SETTING, POST_QUEUE)
    except Exception as e:
        print(f"Warning: could not persist the post queue: {e}")


def post_queue_snapshot(base: str = "") -> dict:
    with POST_QUEUE_LOCK:
        items = [dict(i) for i in POST_QUEUE]
    for i in items:
        # the exact URL handed to Buffer, so it can be opened and checked
        i["media_url"] = (f"{base}{PUBLIC_MEDIA_PREFIX}{i['media_token']}.mp4"
                          if base and i.get("media_token") else None)
        i["media_on_disk"] = bool(i.get("media_token")) and \
            (PUBLIC_MEDIA_DIR / f"{i['media_token']}.mp4").exists()
    return {
        "posts": items,
        "pending": sum(1 for i in items if i["status"] == "pending"),
    }


def find_post(post_id: str) -> dict | None:
    with POST_QUEUE_LOCK:
        return next((i for i in POST_QUEUE if i["post_id"] == post_id), None)


def check_media_url(url: str) -> dict:
    """Fetch our own public media URL the way Buffer would: no cookies, from
    outside. Exercises DNS, TLS, Railway routing and the login gate in one go."""
    try:
        # fetchers commonly probe with HEAD before downloading, so check that too
        head = requests.head(url, timeout=20, allow_redirects=False)
        resp = requests.get(url, headers={"Range": "bytes=0-2047"}, timeout=20,
                            allow_redirects=False)
    except requests.RequestException as e:
        return {"ok": False, "detail": f"Requesting it failed: {e}."}

    if head.status_code not in (200, 206):
        return {"ok": False, "detail": f"A HEAD request returned {head.status_code} while GET "
                                       f"returned {resp.status_code}. Fetchers probe with HEAD "
                                       f"first, so the route has to answer it."}

    ctype = resp.headers.get("content-type", "")
    if resp.status_code in (301, 302, 303, 307, 308):
        return {"ok": False, "detail": f"It redirects to {resp.headers.get('location')!r}; "
                                       f"Buffer needs a direct link."}
    if resp.status_code == 401:
        return {"ok": False, "detail": "It returned 401 — the login gate is covering it, so "
                                       "Buffer would be asked to sign in."}
    if resp.status_code == 404:
        return {"ok": False, "detail": "It returned 404 — the file isn't on this server. On "
                                       "Railway the state dir must be a mounted volume."}
    if resp.status_code not in (200, 206):
        return {"ok": False, "detail": f"It returned HTTP {resp.status_code}."}
    if "video" not in ctype and "octet-stream" not in ctype:
        return {"ok": False, "detail": f"It served content-type {ctype!r} rather than video."}
    return {"ok": True, "detail": f"HTTP {resp.status_code}, {ctype}"}


def send_staged_post(item: dict, base: str) -> dict:
    """Push one staged post to Buffer, one queued post per channel."""
    token = auth.buffer_token()
    if not token:
        raise ValueError("No Buffer API key saved. Add one in Connections.")
    if not item.get("channels"):
        raise ValueError(f"No Buffer channels for {item.get('influencer')}.")
    media = PUBLIC_MEDIA_DIR / f"{item.get('media_token')}.mp4"
    if not item.get("media_token") or not media.exists():
        raise ValueError("The video for this post is no longer on the server.")

    video_url = f"{base}{PUBLIC_MEDIA_PREFIX}{item['media_token']}.mp4"
    media_path = PUBLIC_MEDIA_DIR / f"{item['media_token']}.mp4"
    print(f"Posting {item.get('video_name')}: url={video_url} "
          f"on_disk={media_path.exists()} "
          f"size={media_path.stat().st_size if media_path.exists() else 0}")

    # Buffer fetches this URL itself, anonymously, from the public internet. If
    # it can't, every channel fails identically and the real reason is buried in
    # three copies of the same error — so check it once, up front.
    check = check_media_url(video_url)
    if not check["ok"]:
        raise ValueError(f"Buffer can't fetch the video. {check['detail']} URL: {video_url}")

    results = []
    for chan in item["channels"]:
        entry = {"platform": chan.get("service"), "handle": chan.get("name")}
        try:
            post = buffer_api.create_video_post(token, chan["id"], item["caption"],
                                                video_url, service=chan.get("service"))
            entry.update({"ok": True, "post_id": post.get("id"), "due_at": post.get("dueAt")})
        except buffer_api.BufferError as e:
            print(f"Buffer createPost failed for {chan.get('service')} "
                  f"{chan.get('name')}: {e}")
            # a timeout means "unknown", not "failed" — keep them apart so the UI
            # doesn't invite a retry that duplicates a post Buffer already made
            entry.update({"ok": False, "error": str(e),
                          "indeterminate": isinstance(e, buffer_api.BufferTimeout)})
        results.append(entry)

    ok = [r for r in results if r.get("ok")]
    failed = [r for r in results if not r.get("ok")]
    unknown = [r for r in failed if r.get("indeterminate")]
    message = (f"Queued to {len(ok)} of {len(results)} channels" if failed
               else f"Queued to {len(ok)} channel{'s' if len(ok) != 1 else ''}")
    if unknown:
        message += f" · {len(unknown)} unconfirmed"
    item.update({
        "results": results,
        "sent_at": time.time(),
        "status": "posted" if not failed else ("partial" if ok else "error"),
        "message": message,
    })
    mirror_post_status(item)
    return item


def mirror_post_status(item: dict):
    """Reflect a staged post's fate on its render-queue item, so the rail shows
    queued (yellow) vs posted (green) without the UI having to join the two."""
    job = job_status.get(item.get("job_id"))
    if job:
        job.update({"post_status": item["status"], "post_message": item.get("message"),
                    "post_id": item.get("post_id")})


def zip_influencer_name(jobs: list[dict]) -> str:
    """Influencer half of the download-all filename.

    One influencer in the batch gives their name; a mixed batch joins them,
    falling back to 'mixed' once that would run long.
    """
    names: list[str] = []
    for job in jobs:                       # keep queue order, drop repeats
        name = re.sub(r"[^a-z0-9_-]", "", (job.get("player") or "").lower())
        if name and name not in names:
            names.append(name)
    if not names:
        return "wow_moments"
    joined = "-".join(names)
    return joined if len(joined) <= 40 else "mixed"


def delete_output(job: dict):
    if job.get("file"):
        try:
            (OUTPUT_DIR / job["file"]).unlink(missing_ok=True)
        except Exception:
            pass


@app.on_event("startup")
async def startup_event():
    """Populate categories from Drive and start the render worker."""
    threading.Thread(target=render_worker, daemon=True).start()
    load_post_queue()

    def warm_drive():
        # off the startup path so the app serves immediately — Buffer may be
        # fetching /m/<token>.mp4 for an already-queued post during a redeploy
        try:
            populate_stacked_categories()
            prefetch_player_images()
        except Exception as e:
            print(f"Warning: Could not auto-populate categories from Drive: {e}")

    threading.Thread(target=warm_drive, daemon=True).start()


@app.get("/stacked-categories")
def get_stacked_categories():
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


def pairing_rows(vr_files: list[dict], irl_files: list[dict], player_key: str) -> tuple[list[dict], list[str]]:
    """One row per gameplay clip, flagged with whether this player has a matching IRL clip.

    Uses match_irl_clip — the same matcher the render pipelines use — so what this
    reports is exactly what a render would find.
    """
    rows = []
    paired_irl_ids = set()
    for vr in vr_files:
        irl = match_irl_clip(vr["name"], irl_files, player_key)
        if irl:
            paired_irl_ids.add(irl["id"])
        rows.append({
            "number": clip_number(vr["name"]),
            "gameplay": vr["name"],
            "irl": irl["name"] if irl else None,
            "matched": irl is not None,
        })
    # unnumbered clips can never pair, so they sort to the bottom
    rows.sort(key=lambda r: (r["number"] is None, r["number"] or 0, r["gameplay"]))

    # IRL clips no gameplay clip points at — usually a filename typo
    orphans = sorted(f["name"] for f in irl_files if f["id"] not in paired_irl_ids)
    return rows, orphans


def inventory_group(cat: dict, player_key: str, required: int, cache: dict) -> dict:
    """Pairing report for one plain category (a combo contributes one of these per segment)."""
    def videos(folder_url: str) -> list[dict]:
        # combos re-reference folders their standalone types already listed
        folder_id = extract_folder_id(folder_url)
        if folder_id not in cache:
            cache[folder_id] = list_drive_files(folder_id, "video/")
        return cache[folder_id]

    rows, orphans = pairing_rows(
        videos(cat["vr_folder"]),
        videos(cat["players"][player_key]["irl_folder"]),
        player_key,
    )
    return {
        "source_label": cat["label"],
        "required": required,
        "total": len(rows),
        "matched": sum(1 for r in rows if r["matched"]),
        "clips": rows,
        "orphan_irl": orphans,
    }


def category_inventory(cat_key: str, cat: dict, player_key: str, cache: dict) -> dict:
    """One video type, rolled up. A combo owns no clips, so it reports one group per
    recipe segment and totals them."""
    if cat.get("type") == "combo":
        groups = []
        for segment in cat["recipe"]["segments"]:
            src_key = segment["source"].lower().replace(" ", "_")
            src_cat = STACKED_CATEGORIES.get(src_key)
            if src_cat is None:
                raise ValueError(f"Combo source '{segment['source']}' is missing from Drive.")
            groups.append(inventory_group(src_cat, player_key, segment.get("count", 0), cache))
    else:
        groups = [inventory_group(cat, player_key, NUM_PAIRS, cache)]

    return {
        "key": cat_key,
        "label": cat["label"],
        "combo": cat.get("type") == "combo",
        "matched": sum(g["matched"] for g in groups),
        "total": sum(g["total"] for g in groups),
        "short": any(g["matched"] < g["required"] for g in groups),
        "groups": groups,
    }


@app.get("/influencer-inventory")
def influencer_inventory(player: str):
    """Pairing status for every video type this influencer appears in.

    Returns the per-clip rows too, so the UI can expand a type without another
    round trip to Drive.
    """
    cats = [(k, c) for k, c in STACKED_CATEGORIES.items() if player in c["players"]]
    if not cats:
        return JSONResponse(status_code=400, content={"error": f"Unknown player: {player}"})

    player_label = cats[0][1]["players"][player]["display"]
    cache: dict[str, list[dict]] = {}
    types = []
    for cat_key, cat in cats:
        try:
            types.append(category_inventory(cat_key, cat, player, cache))
        except (ValueError, KeyError, requests.HTTPError) as e:
            # one broken folder shouldn't blank out the whole report
            types.append({"key": cat_key, "label": cat["label"], "combo": cat.get("type") == "combo",
                          "matched": 0, "total": 0, "short": True, "groups": [], "error": str(e)})

    types.sort(key=lambda t: t["label"].lower())
    return {
        "player": player,
        "player_label": player_label,
        "matched": sum(t["matched"] for t in types),
        "total": sum(t["total"] for t in types),
        "types": types,
    }


# ─────────────────────────────────────────────
# AUTH — the app is gated because anyone with the URL would otherwise be able to
# use the credentials the server holds.
PUBLIC_PATHS = {"/login", "/auth/login", "/auth/callback", "/healthz"}
PUBLIC_MEDIA_PREFIX = "/m/"


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    # /m/<token>.mp4 is deliberately outside the gate — Buffer fetches media
    # anonymously and cannot present a cookie. The token is the only credential,
    # and it addresses exactly one file.
    if (path in PUBLIC_PATHS or path.startswith(PUBLIC_MEDIA_PREFIX)
            or not auth.enforcing() or auth.current_user(request)):
        return await call_next(request)
    # browsers get the sign-in page, fetch() gets a 401 it can act on
    if "text/html" in request.headers.get("accept", ""):
        return FileResponse("static/login.html")
    return JSONResponse(status_code=401, content={"error": "Not signed in."})


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# GET *and* HEAD: FastAPI's @app.get() does not also register HEAD the way plain
# Starlette does, and media fetchers (Buffer included) probe with HEAD first.
# A 404 there reads to them as "the video could not be read from its URL".
@app.api_route(PUBLIC_MEDIA_PREFIX + "{name}", methods=["GET", "HEAD"])
async def public_media(name: str):
    """Serve one published render to Buffer, unauthenticated.

    The token is a 32-char random string and names a single file; there is no
    listing, and anything that isn't a clean token is refused before it touches
    the filesystem.
    """
    token = name[:-4] if name.endswith(".mp4") else name
    if not MEDIA_TOKEN_RE.match(token):
        return JSONResponse(status_code=404, content={"error": "Not found"})
    path = (PUBLIC_MEDIA_DIR / f"{token}.mp4").resolve()
    if PUBLIC_MEDIA_DIR.resolve() not in path.parents or not path.exists():
        return JSONResponse(status_code=404, content={"error": "Not found"})
    return FileResponse(path, media_type="video/mp4")


@app.get("/login")
async def login_page():
    return FileResponse("static/login.html")


@app.get("/auth/login")
async def auth_login():
    if not auth.configured():
        return JSONResponse(status_code=503, content={
            "error": "Sign-in is not configured. Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and SECRET_KEY."})
    url, state = auth.login_url()
    resp = RedirectResponse(url)
    # state round-trips in a cookie so it survives restarts and multiple workers
    resp.set_cookie(auth.STATE_COOKIE, state, max_age=600, httponly=True,
                    secure=REQUIRE_SECURE_COOKIES, samesite="lax")
    return resp


@app.get("/auth/callback")
def auth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return RedirectResponse(f"/login?error={quote(error)}")
    expected = request.cookies.get(auth.STATE_COOKIE)
    if not expected or not secrets.compare_digest(state, expected):
        return RedirectResponse("/login?error=" + quote("Sign-in expired — please try again."))
    try:
        user = auth.complete_login(code)
    except auth.AuthError as e:
        return RedirectResponse(f"/login?error={quote(str(e))}")

    resp = RedirectResponse("/")
    resp.set_cookie(auth.SESSION_COOKIE, auth.make_session(user["email"], user["name"]),
                    max_age=auth.SESSION_DAYS * 86400, httponly=True,
                    secure=REQUIRE_SECURE_COOKIES, samesite="lax")
    resp.delete_cookie(auth.STATE_COOKIE)
    # the sign-in also (re)connected Drive, so pick up the folder tree now
    try:
        populate_stacked_categories()
        threading.Thread(target=prefetch_player_images, daemon=True).start()
    except Exception as e:
        print(f"Warning: Could not refresh categories after sign-in: {e}")
    return resp


@app.post("/auth/logout")
async def auth_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.SESSION_COOKIE)
    return resp


@app.get("/connections")
async def connections(request: Request):
    """Which integrations are wired up.

    Reports presence and health only — never the credential itself.
    """
    user = auth.current_user(request) or {}
    google = auth.google_connection()
    types = len(STACKED_CATEGORIES)

    if google:
        drive = {
            "connected": bool(types),
            "account": google["email"],
            "source": "signed-in account",
            "detail": (f"{types} video {'type' if types == 1 else 'types'} loaded from Drive."
                       if types else
                       "Signed in, but Drive returned no video types — check the folder is shared "
                       "with this account."),
        }
    elif os.environ.get("GOOGLE_API_KEY"):
        drive = {
            "connected": bool(types),
            "account": None,
            "source": "GOOGLE_API_KEY (local dev)",
            "detail": (f"{types} video {'type' if types == 1 else 'types'} loaded via API key."
                       if types else
                       "API key is set but Drive returned no video types — check the key and "
                       "folder sharing."),
        }
    else:
        drive = {"connected": False, "account": None, "source": None,
                 "detail": "Not connected. Sign in with Google to grant Drive access."}

    buffer_conn = auth.buffer_connection()
    env_buffer = bool(os.environ.get("BUFFER_ACCESS_TOKEN"))
    buffer = {
        # a stored key is not a working integration — publishing isn't built yet
        "connected": False,
        "has_key": bool(buffer_conn) or env_buffer,
        "hint": (buffer_conn or {}).get("hint"),
        "source": "saved in app" if buffer_conn else ("environment" if env_buffer else None),
        "detail": ("Key saved. Publishing isn't built yet, so nothing posts to Buffer."
                   if (buffer_conn or env_buffer)
                   else "No key saved. Buffer has no sign-in link — paste a personal access token."),
    }

    return {
        "user": {"email": user.get("email"), "name": user.get("name"), "dev": user.get("dev", False)},
        "auth_enforced": auth.enforcing(),
        "login_available": auth.configured(),
        "storage_ready": auth.storage_ready(),
        "services": [
            {"key": "google_drive", "label": "Google Drive",
             "purpose": "Reads the gameplay, IRL and music folders.",
             "auth_style": "oauth", **drive},
            {"key": "buffer", "label": "Buffer",
             "purpose": "Will schedule finished highlights to social.",
             "auth_style": "token", **buffer},
        ],
    }


def influencer_names() -> list[str]:
    """Influencer display names, from the same Drive data the Generate tab uses,
    so Analytics and Generate always agree on who exists."""
    names = {p["display"] for cat in STACKED_CATEGORIES.values()
             for p in cat["players"].values()}
    return sorted(names)


CHANNEL_MAP_SETTING = "buffer_channel_map"


@app.get("/analytics/posts")
def analytics_posts(range: str = "month", offset: int = 0,
                          force: bool = False, cached_only: bool = False):
    """Posts for one week / month / year.

    Syncing is manual: `cached_only=true` renders the last sync of that period
    without touching Buffer (what opening the tab does), `force=true` spends a
    request to re-read. Moving to a period we haven't fetched does read Buffer —
    that's an explicit navigation, not a background poll.
    """
    token = auth.buffer_token()
    if not token:
        return JSONResponse(status_code=400, content={
            "error": "No Buffer API key saved. Add one in Connections — create it at "
                     "publish.buffer.com/settings/api (organization owners only)."})
    names = influencer_names()
    overrides = auth.get_setting(CHANNEL_MAP_SETTING, {}) or {}
    try:
        period = buffer_api.period_bounds(range, offset)
    except buffer_api.BufferError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    if offset > 0:
        return JSONResponse(status_code=400, content={"error": "That period is in the future."})

    nav = {"kind": period["kind"], "offset": period["offset"], "label": period["label"],
           "nav_label": period["nav_label"], "is_current": period["is_current"]}

    if cached_only:
        cached = buffer_api.last_sync(token, names, overrides, period)
        if cached is None:
            return {"synced": False, "influencers": names, "period": nav}
        return {**cached, "synced": True, "cached": True, "influencers": names}

    try:
        data = buffer_api.posts_in_period(token, names, period,
                                          force=force, overrides=overrides)
        return {**data, "synced": True, "influencers": names}
    except buffer_api.BufferError as e:
        return JSONResponse(status_code=502, content={"error": str(e), "period": nav})


@app.post("/analytics/channel-map")
async def set_channel_map(request: Request):
    """Async because it reads the request body; the Buffer re-sync it triggers is
    handed to a thread so it can't block the event loop."""
    """Assign a Buffer channel to an influencer by hand.

    Buffer's API exposes no channel groups, so name matching is all we get
    automatically — and it fails whenever the channel spells the name
    differently ("ChristyPlaysVR" vs "Christie").
    """
    body = await request.json()
    channel_id = (body.get("channel_id") or "").strip()
    influencer = (body.get("influencer") or "").strip() or None
    range_kind = body.get("range") or "month"
    offset = int(body.get("offset") or 0)
    if not channel_id:
        return JSONResponse(status_code=400, content={"error": "channel_id is required."})
    if influencer and influencer not in influencer_names():
        return JSONResponse(status_code=400, content={
            "error": f"Unknown influencer: {influencer}"})

    mapping = dict(auth.get_setting(CHANNEL_MAP_SETTING, {}) or {})
    if influencer:
        mapping[channel_id] = influencer
    else:
        mapping.pop(channel_id, None)      # blank clears the assignment
    auth.save_setting(CHANNEL_MAP_SETTING, mapping)

    token = auth.buffer_token()
    if not token:
        return {"ok": True, "channel_map": mapping}
    try:
        # assigning is an explicit action, so re-syncing here is consistent with
        # manual-only refresh; channels are cached so it's one posts read
        period = buffer_api.period_bounds(range_kind, offset)
        data = await run_in_threadpool(
            buffer_api.posts_in_period, token, influencer_names(), period,
            force=True, overrides=mapping)
        return {**data, "synced": True, "influencers": influencer_names(),
                "channel_map": mapping}
    except buffer_api.BufferError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.get("/analytics/buffer-check")
def analytics_buffer_check(force: bool = False):
    """Diagnostic: what this Buffer schema actually exposes.

    The metrics queries are experimental, so this reports the real field and enum
    names instead of leaving a mismatch to surface as a broken table.
    """
    token = auth.buffer_token()
    if not token:
        return JSONResponse(status_code=400, content={"error": "No Buffer API key saved."})
    try:
        report = buffer_api.capability_report(token, force=force)
        org_id = buffer_api.organization_id(token)
        chans = buffer_api.channels(token, org_id)
        overrides = auth.get_setting(CHANNEL_MAP_SETTING, {}) or {}
        mapping, unmapped = buffer_api.map_channels(chans, influencer_names(), overrides)
        return {
            "schema": report,
            "channel_map_overrides": overrides,
            "channel_count": len(chans),
            "mapped": [{"channel": c["name"], "service": c["service"],
                        "group": c["group"], "influencer": mapping.get(c["id"])}
                       for c in chans],
            "unmapped_channels": unmapped,
            "influencers_known": influencer_names(),
        }
    except buffer_api.BufferError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.post("/connections/buffer")
async def save_buffer(request: Request):
    body = await request.json()
    try:
        auth.save_buffer_token(body.get("token") or "")
    except auth.AuthError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    return await connections(request)


@app.delete("/connections/{service}")
async def remove_connection(service: str, request: Request):
    try:
        auth.disconnect("google" if service == "google_drive" else service)
    except auth.AuthError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    return await connections(request)


@app.get("/player-image/{player_key}")
def get_player_image(player_key: str):
    if player_key not in PLAYER_IMAGE_IDS:
        return JSONResponse(status_code=404, content={"error": "No image for this player"})
    if player_key in PLAYER_IMAGE_CACHE:
        content, content_type = PLAYER_IMAGE_CACHE[player_key]
        return Response(content=content, media_type=content_type)
    extra, headers = auth.drive_auth()
    file_id = PLAYER_IMAGE_IDS[player_key]
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}"
    resp = requests.get(url, params={"alt": "media", **extra}, headers=headers,
                        stream=True, timeout=30)
    resp.raise_for_status()
    content_type = resp.headers.get("content-type", "image/jpeg")
    return StreamingResponse(resp.iter_content(chunk_size=65536), media_type=content_type)


@app.post("/generate-stacked")
async def generate_stacked(request: Request):
    """Add one or more render requests to the queue."""
    body = await request.json()
    category_key = body.get("category")
    player_key   = body.get("player")
    vr_on_top    = body.get("vr_on_top", True)
    seed_raw     = body.get("seed")
    count_raw    = body.get("count", 1)

    if category_key not in STACKED_CATEGORIES:
        return JSONResponse(status_code=400, content={"error": f"Unknown category: {category_key}"})
    if player_key not in STACKED_CATEGORIES[category_key]["players"]:
        return JSONResponse(status_code=400, content={"error": f"Unknown player: {player_key}"})

    seed_clip = None
    if seed_raw not in (None, ""):
        try:
            seed_clip = int(seed_raw)
        except (TypeError, ValueError):
            return JSONResponse(status_code=400, content={"error": f"Invalid seed value: {seed_raw}"})

    try:
        count = int(count_raw)
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"error": f"Invalid count: {count_raw}"})
    if count < 1 or count > MAX_BATCH:
        return JSONResponse(status_code=400, content={"error": f"Count must be between 1 and {MAX_BATCH}."})

    job_ids = [enqueue_job(category_key, player_key, vr_on_top, seed_clip) for _ in range(count)]
    return {"job_ids": job_ids, "job_id": job_ids[0], "queue": queue_snapshot()}


@app.post("/queue/{job_id}/post")
def stage_post(job_id: str):
    """Stage a finished render for review on the Posting tab.

    Nothing reaches Buffer here. The caption, the channels and the public video
    copy are all resolved now so the review table shows what will actually go
    out — and so a later CLEAR FINISHED can't pull the file out from under it.
    """
    job = job_status.get(job_id)
    if not job:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    if job.get("status") != "done":
        return JSONResponse(status_code=400, content={"error": "That render isn't finished."})

    def fail(msg: str, code: int = 400):
        job.update({"post_status": "error", "post_message": msg})
        return JSONResponse(status_code=code, content={
            "error": msg, "queue": queue_snapshot(), "post_queue": post_queue_snapshot()})

    existing = next((i for i in POST_QUEUE if i.get("job_id") == job_id
                     and i["status"] == "pending"), None)
    if existing:
        job.update({"post_status": "queued", "post_message": "Already in the post queue",
                    "post_id": existing["post_id"]})
        return {"queue": queue_snapshot(), "post_queue": post_queue_snapshot()}

    token = auth.buffer_token()
    if not token:
        return fail("No Buffer API key saved. Add one in Connections.")

    try:
        names = influencer_names()
        overrides = auth.get_setting(CHANNEL_MAP_SETTING, {}) or {}
        chans = buffer_api.channels_for_influencer(token, job["player_label"], names, overrides)
        if not chans:
            raise ValueError(f"No Buffer channels are assigned to {job['player_label']}. "
                             f"Check the channel assignments on the Analytics tab.")
        picked = random_caption(job["category"])
        caption, caption_file = picked["text"], picked["file"]
        media_token = publish_media(job)
    except (ValueError, buffer_api.BufferError) as e:
        return fail(str(e))
    except Exception as e:
        return fail(str(e), 500)

    item = {
        "post_id": str(uuid.uuid4())[:8],
        "job_id": job_id,
        "video_name": job.get("download_name") or job.get("file"),
        "media_token": media_token,
        "influencer": job["player_label"],
        "category": job["category"],
        "category_label": job["category_label"],
        "caption": caption,
        # which file in which category's Captions folder this line came from
        "caption_file": caption_file,
        "caption_sources": caption_sources(job["category"]),
        # YouTube takes the caption as its title and gets no description, so the
        # title is what actually publishes there — show it at review time
        "youtube_title": (buffer_api.youtube_title(caption)
                          if any((c.get("service") or "").lower() == "youtube" for c in chans)
                          else None),
        "channels": [{"id": c["id"], "service": c.get("service"), "name": c.get("name")}
                     for c in chans],
        "status": "pending",
        "message": None,
        "results": [],
        "created_at": time.time(),
    }
    with POST_QUEUE_LOCK:
        POST_QUEUE.append(item)
        persist_post_queue()
    job.update({"post_status": "queued", "post_message": "In the post queue",
                "post_id": item["post_id"]})
    return {"queue": queue_snapshot(), "post_queue": post_queue_snapshot()}


@app.post("/queue/post-all")
def stage_all_posts():
    """Send every finished, not-yet-queued render to the post queue."""
    with QUEUE_LOCK:
        candidates = [j for j in (job_status.get(i) for i in JOB_ORDER)
                      if j and j["status"] == "done" and not j.get("post_id")]
    staged, failed = 0, []
    for job in candidates:
        result = stage_post(job["job_id"])
        if isinstance(result, JSONResponse):
            failed.append(job.get("download_name") or job["job_id"])
        else:
            staged += 1
    out = {"queue": queue_snapshot(), "post_queue": post_queue_snapshot(), "staged": staged}
    if failed:
        out["error"] = (f"Queued {staged}; couldn't queue {len(failed)}: "
                        + ", ".join(failed[:3]) + ("…" if len(failed) > 3 else ""))
    return out


@app.post("/posts/clear")
def clear_post_queue():
    """Wipe the post queue regardless of status.

    Published media is left alone — Buffer fetches it when a queued post actually
    goes out, which can be days later. Retention sweeps it instead.
    """
    global POST_QUEUE
    with POST_QUEUE_LOCK:
        for item in POST_QUEUE:
            job = job_status.get(item.get("job_id"))
            if job:
                job.update({"post_status": None, "post_message": None, "post_id": None})
        POST_QUEUE = []
        persist_post_queue()
    return {"post_queue": post_queue_snapshot(), "queue": queue_snapshot()}


@app.get("/posts")
def get_posts(request: Request):
    return post_queue_snapshot(public_base_url(request))


@app.get("/posts/media-check")
def posts_media_check(request: Request):
    """Is the media URL Buffer would fetch actually reachable from outside?

    Checks the origin we'd hand Buffer and, if anything is staged, that exact URL.
    """
    base = public_base_url(request)
    with POST_QUEUE_LOCK:
        staged = [dict(i) for i in POST_QUEUE]
    out = {
        "base_url": base,
        "https": base.startswith("https://"),
        "public_base_url_env": os.environ.get("PUBLIC_BASE_URL") or None,
        "oauth_redirect_uri": os.environ.get("OAUTH_REDIRECT_URI") or None,
        "state_dir": str(auth.STATE_DIR.resolve()),
        "media_dir_exists": PUBLIC_MEDIA_DIR.exists(),
        "media_files_on_disk": len(list(PUBLIC_MEDIA_DIR.glob("*.mp4"))) if PUBLIC_MEDIA_DIR.exists() else 0,
        "checks": [],
    }
    for item in staged[:3]:
        url = f"{base}{PUBLIC_MEDIA_PREFIX}{item.get('media_token')}.mp4"
        on_disk = (PUBLIC_MEDIA_DIR / f"{item.get('media_token')}.mp4").exists()
        out["checks"].append({"video": item.get("video_name"), "url": url,
                              "on_disk": on_disk, **check_media_url(url)})
    return out


@app.post("/posts/{post_id}/send")
def send_post(post_id: str, request: Request):
    # deliberately a plain def: Buffer fetches /m/<token>.mp4 from this same
    # server while this call is in flight, so the event loop must stay free
    item = find_post(post_id)
    if not item:
        return JSONResponse(status_code=404, content={"error": "Post not found"})
    if item["status"] in ("posted", "posting"):
        return JSONResponse(status_code=409, content={"error": "That post has already gone out."})

    base = public_base_url(request)
    if not base.startswith("https://"):
        msg = (f"Buffer needs a public HTTPS URL for the video, but this server is reachable "
               f"at {base}. Posting works from the deployed app.")
        item.update({"status": "error", "message": msg})
        mirror_post_status(item)
        return JSONResponse(status_code=400, content={"error": msg,
                                                      "post_queue": post_queue_snapshot()})
    try:
        send_staged_post(item, base)
    except (ValueError, buffer_api.BufferError) as e:
        item.update({"status": "error", "message": str(e)})
        mirror_post_status(item)
        return JSONResponse(status_code=400, content={"error": str(e),
                                                      "post_queue": post_queue_snapshot()})
    finally:
        with POST_QUEUE_LOCK:
            persist_post_queue()
    return post_queue_snapshot()


@app.post("/posts/send-all")
def send_all_posts(request: Request):
    """Send every pending post. One failure doesn't stop the rest."""
    base = public_base_url(request)
    if not base.startswith("https://"):
        return JSONResponse(status_code=400, content={
            "error": f"Buffer needs a public HTTPS URL for the video, but this server is "
                     f"reachable at {base}. Posting works from the deployed app.",
            "post_queue": post_queue_snapshot()})

    with POST_QUEUE_LOCK:
        pending = [i for i in POST_QUEUE if i["status"] in ("pending", "error", "partial")]
    for item in pending:
        try:
            send_staged_post(item, base)
        except (ValueError, buffer_api.BufferError) as e:
            item.update({"status": "error", "message": str(e)})
            mirror_post_status(item)
    with POST_QUEUE_LOCK:
        persist_post_queue()
    return post_queue_snapshot()


@app.delete("/posts/{post_id}")
async def remove_post(post_id: str):
    global POST_QUEUE
    with POST_QUEUE_LOCK:
        item = next((i for i in POST_QUEUE if i["post_id"] == post_id), None)
        POST_QUEUE = [i for i in POST_QUEUE if i["post_id"] != post_id]
        persist_post_queue()
    # let the render item offer Post again
    if item and (job := job_status.get(item.get("job_id"))):
        job.update({"post_status": None, "post_message": None, "post_id": None})
    return post_queue_snapshot()


@app.post("/standard-week")
async def standard_week(request: Request):
    """Queue a week's worth of renders for one influencer, spaced by type."""
    body = await request.json()
    player_key = body.get("player")
    vr_on_top = body.get("vr_on_top", True)

    types = [{"key": k, "label": c["label"]}
             for k, c in STACKED_CATEGORIES.items() if player_key in c.get("players", {})]
    if not types:
        return JSONResponse(status_code=400, content={
            "error": "No video types found for that influencer."})

    counts = standard_week_counts(types)
    order = spread_week(counts)
    for cat_key in order:
        enqueue_job(cat_key, player_key, vr_on_top, None)

    labels = {t["key"]: t["label"] for t in types}
    return {
        "queue": queue_snapshot(),
        "plan": [{"key": k, "label": labels.get(k, k), "count": v} for k, v in counts.items()],
        "order": [labels.get(k, k) for k in order],
    }


@app.get("/queue")
async def get_queue():
    return queue_snapshot()


@app.post("/queue/{job_id}/cancel")
async def cancel_job(job_id: str):
    """Stop a single job — kills its ffmpeg pass if it's the one rendering."""
    job = job_status.get(job_id)
    if not job:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    with QUEUE_LOCK:
        if job["status"] in ("done", "error", "cancelled"):
            return queue_snapshot()
        CANCELLED.add(job_id)
        job["message"] = "Stopping..."
        is_running = CURRENT["job_id"] == job_id
        if not is_running:
            job.update({"status": "cancelled", "message": "Stopped before it started.",
                        "finished_at": time.time()})
    if is_running:
        kill_current_proc()
    return queue_snapshot()


@app.post("/queue/stop")
async def stop_queue():
    """Stop the render in progress and drop everything still waiting."""
    with QUEUE_LOCK:
        running = CURRENT["job_id"]
        for job_id in JOB_ORDER:
            job = job_status.get(job_id)
            if not job or job["status"] in ("done", "error", "cancelled"):
                continue
            CANCELLED.add(job_id)
            if job_id == running:
                job["message"] = "Stopping..."
            else:
                job.update({"status": "cancelled", "message": "Stopped before it started.",
                            "finished_at": time.time()})
    if running:
        kill_current_proc()
    return queue_snapshot()


@app.post("/queue/{job_id}/remove")
def remove_job(job_id: str):
    """Drop a finished job from the list and delete its file.

    Its staged post goes too — the video it would publish is being deleted here,
    so leaving the row behind would only fail later.
    """
    global POST_QUEUE
    with QUEUE_LOCK:
        job = job_status.get(job_id)
        if not job:
            return JSONResponse(status_code=404, content={"error": "Job not found"})
        if job["status"] not in ("done", "error", "cancelled"):
            return JSONResponse(status_code=400, content={"error": "Stop the job before removing it."})
        delete_output(job)
        job_status.pop(job_id, None)
        CANCELLED.discard(job_id)
        if job_id in JOB_ORDER:
            JOB_ORDER.remove(job_id)

    with POST_QUEUE_LOCK:
        if any(i.get("job_id") == job_id for i in POST_QUEUE):
            POST_QUEUE = [i for i in POST_QUEUE if i.get("job_id") != job_id]
            persist_post_queue()
    return {**queue_snapshot(), "post_queue": post_queue_snapshot()}


def job_is_clearable(job: dict) -> bool:
    """A render can be cleared once it can't be posted or already has been.

    A finished render that is still waiting to be posted — or sitting in the post
    queue unsent — is kept, since clearing it deletes the file it would post.
    """
    status = job.get("status")
    if status in ("error", "cancelled"):
        return True                       # never postable
    if status != "done":
        return False                      # still rendering or queued
    return job.get("post_status") == "posted"


@app.post("/queue/clear")
def clear_queue():
    """Clear finished entries that are done with. Anything still postable stays."""
    with QUEUE_LOCK:
        for job_id in list(JOB_ORDER):
            job = job_status.get(job_id)
            if not job or not job_is_clearable(job):
                continue
            delete_output(job)
            job_status.pop(job_id, None)
            CANCELLED.discard(job_id)
            JOB_ORDER.remove(job_id)

    # and drop the matching sent posts from the post queue
    global POST_QUEUE
    with POST_QUEUE_LOCK:
        POST_QUEUE = [i for i in POST_QUEUE if i["status"] != "posted"]
        persist_post_queue()
    return {**queue_snapshot(), "post_queue": post_queue_snapshot()}


@app.get("/status/{job_id}")
async def get_status(job_id: str):
    return job_status.get(job_id, {"error": "Job not found"})


@app.get("/download/{filename}")
async def download(filename: str):
    path = (OUTPUT_DIR / filename).resolve()
    if OUTPUT_DIR.resolve() not in path.parents or not path.exists():
        return JSONResponse(status_code=404, content={"error": "File not found"})
    job = next((j for j in job_status.values() if j.get("file") == filename), None)
    nice_name = (job or {}).get("download_name") or filename
    return FileResponse(path, media_type="video/mp4", filename=nice_name)


@app.get("/download-all")
def download_all():
    """Zip every finished render in the queue into one download."""
    with QUEUE_LOCK:
        finished = [job_status[j] for j in JOB_ORDER
                    if job_status.get(j, {}).get("status") == "done" and job_status[j].get("file")]
    ready = [j for j in finished if (OUTPUT_DIR / j["file"]).exists()]
    if not ready:
        return JSONResponse(status_code=404, content={"error": "No finished renders to download yet."})

    # on-disk name stays unique so two downloads can't collide mid-zip
    zip_path = TEMP_DIR / f"wow_moments_{int(time.time())}.zip"
    download_name = f"{time.strftime('%Y-%m-%d')}_{zip_influencer_name(ready)}.zip"
    used_names: set[str] = set()
    # ZIP_STORED: mp4s don't compress, and Railway's memory budget is tight
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        for i, job in enumerate(ready, start=1):
            name = job.get("download_name") or job["file"]
            if name in used_names:
                stem, ext = os.path.splitext(name)
                name = f"{stem}_{i}{ext}"
            used_names.add(name)
            zf.write(OUTPUT_DIR / job["file"], arcname=name)

    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=download_name,
        background=BackgroundTask(lambda: zip_path.unlink(missing_ok=True)),
    )


app.mount("/", StaticFiles(directory="static", html=True), name="static")
