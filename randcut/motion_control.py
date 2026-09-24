"""Persistent, serial Kling Motion Control jobs and local review artifacts.

Kie contract: https://docs.kie.ai/market/kling/motion-control
No public app media route is needed: reference files use Kie's upload service.
"""
import json
import math
import mimetypes
import os
from pathlib import Path
import queue
import random
import shutil
import subprocess
import threading
import time
import uuid
from urllib.parse import urlsplit

import requests
import auth

# Shared with the highlight pipeline: never run two memory-heavy ffmpeg passes.
MEDIA_LOCK = threading.Lock()
PROMPT = (
    "The character is playing a fun social VR game on their Quest 3 VR headset. "
    "Match the expressions on the reference video as closely as possible. "
    "sometimes the reference video will go into slow motion or speed up. "
    "match these time shifts so they look realistic. add motion blur as needed. "
    "motion should be smooth. no jerking/shuddering, even if tracking is lost on "
    "reference video going out of frame. camera in fixed position. realistic motion blur."
)
ACTIVE = {"queued", "preparing", "submitting", "processing", "downloading", "approving"}


class MotionError(Exception):
    pass


class SubmissionUncertain(MotionError):
    """A paid request may have succeeded; never automatically create it again."""


def error_message(error):
    if isinstance(error, requests.RequestException):
        # requests includes full URLs, which can contain API keys / asset tokens.
        status = error.response.status_code if error.response is not None else None
        return (f"Remote service returned HTTP {status}. Check Connections and try again." if status else
                "Connection to the remote service failed or timed out. Try again.")
    return str(error)[:600]


def _data(response):
    try:
        body = response.json()
    except ValueError:
        raise MotionError(f"Kie returned an unreadable response (HTTP {response.status_code}).")
    if not response.ok or body.get("code") != 200 or body.get("success") is False:
        raise MotionError(f"Kie: {str(body.get('msg') or body.get('message') or response.status_code)[:400]}")
    return body.get("data") or {}


def kie_headers():
    token = auth.kie_token()
    if not token:
        raise MotionError("Add your Kie.ai API key in Connections first.")
    return {"Authorization": f"Bearer {token}"}


def upload_reference(path: Path) -> str:
    with path.open("rb") as f:
        data = _data(requests.post(
            "https://kieai.redpandaai.co/api/file-stream-upload",
            headers=kie_headers(), files={"file": (path.name, f, mimetypes.guess_type(path.name)[0])},
            data={"uploadPath": "randcut/motion", "fileName": uuid.uuid4().hex + path.suffix},
            timeout=(15, 180)))
    url = data.get("downloadUrl")
    if not url or urlsplit(url).scheme != "https":
        raise MotionError("Kie did not return a secure reference file URL.")
    return url


def create_task(image_url: str, video_url: str) -> str:
    # Standard is mode=720p; there is no separate 'standard' model input.
    payload = {"model": "kling-2.6/motion-control", "input": {
        "input_urls": [image_url], "video_urls": [video_url],
        "character_orientation": "video", "mode": "720p", "prompt": PROMPT,
    }}
    try:
        response = requests.post("https://api.kie.ai/api/v1/jobs/createTask",
                                 headers=kie_headers(), json=payload, timeout=(15, 180))
    except requests.RequestException:
        raise SubmissionUncertain("No confirmation from Kie. Check Kie Logs and attach its task ID; do not generate again until resolved.")
    if response.status_code >= 500:
        raise SubmissionUncertain("Kie had a server error during submission. Check Kie Logs before generating again.")
    try:
        data = _data(response)
    except MotionError:
        # A well-formed rejection is safe to retry. A malformed 2xx is not.
        if response.ok:
            try:
                body = response.json()
                if body.get("code") == 200 or body.get("code", 500) >= 500:
                    raise SubmissionUncertain("Kie's submission was not confirmed. Check Kie Logs for its task ID.")
            except (ValueError, TypeError):
                raise SubmissionUncertain("Kie's submission was not confirmed. Check Kie Logs for its task ID.")
        raise
    if not data.get("taskId"):
        raise SubmissionUncertain("Kie accepted the request without a task ID. Check Kie Logs before generating again.")
    return data["taskId"]


def task_detail(task_id: str) -> dict:
    return _data(requests.get("https://api.kie.ai/api/v1/jobs/recordInfo",
                             headers=kie_headers(), params={"taskId": task_id}, timeout=30))


def run_media(args):
    with MEDIA_LOCK:
        try:
            return subprocess.run(args, capture_output=True, check=True, timeout=300).stdout
        except subprocess.CalledProcessError as e:
            raise MotionError("Video processing failed: " + e.stderr.decode(errors="replace")[-500:])
        except subprocess.TimeoutExpired:
            raise MotionError("Video processing timed out. The source file may be damaged.")


def duration(path: Path) -> float:
    data = json.loads(run_media(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                "-show_entries", "stream=duration:format=duration", "-of", "json", str(path)]))
    if not data.get("streams"):
        raise MotionError("The file has no video stream.")
    value = data["streams"][0].get("duration")
    value = float(value if value and value != "N/A" else data.get("format", {}).get("duration", 0))
    if not math.isfinite(value) or value <= 0:
        raise MotionError("The video has no valid duration.")
    return value


def prepare_video(source: Path, folder: Path) -> tuple[Path, float, bool]:
    original = duration(source)
    if original > 30:
        raise MotionError(f"Kling accepts at most 30 seconds; this clip is {original:.2f}s.")
    if original < 0.2:
        raise MotionError("The motion reference clip is too short to trim safely.")
    extended = original < 3
    if extended:
        # The reference script's forward + slow reverse, repeated for even tiny
        # sources. Work on copies; never delete or change the original in Drive.
        cycle = folder / "cycle.mp4"
        run_media(["ffmpeg", "-y", "-i", str(source), "-filter_complex",
                   "[0:v]scale=w='min(1280,iw)':h='min(1280,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,"
                   "fps=30,setpts=PTS-STARTPTS,split[f][r];[r]reverse,setpts=2*(PTS-STARTPTS)[rev];"
                   "[f][rev]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-an", "-c:v", "libx264",
                   "-threads", "2", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p", str(cycle)])
        prepared = folder / "reference.mp4"
        run_media(["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(cycle), "-t", "3.1",
                   "-an", "-c:v", "libx264", "-threads", "2", "-preset", "fast", "-crf", "18",
                   "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(prepared)])
        cycle.unlink(missing_ok=True)
        if duration(prepared) < 3:
            raise MotionError("Could not extend the motion reference past Kling's 3-second minimum.")
    else:
        prepared = source
        if source.suffix.lower() not in {".mp4", ".mov"}:
            prepared = folder / "reference.mp4"
            run_media(["ffmpeg", "-y", "-i", str(source), "-map", "0:v:0", "-an", "-c:v", "libx264",
                       "-threads", "2", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p", str(prepared)])
    if prepared.stat().st_size > 100 * 1024 * 1024:
        raise MotionError("The reference video exceeds Kling's 100MB limit.")
    return prepared, original, extended


def download_result(url: str, dest: Path):
    # Only URLs returned by the authenticated Kie status endpoint enter here.
    if urlsplit(url).scheme != "https":
        raise MotionError("Kie returned an invalid result URL.")
    tmp = dest.with_suffix(".part")
    with requests.get(url, stream=True, timeout=(15, 180)) as response:
        response.raise_for_status()
        size = 0
        with tmp.open("wb") as out:
            for chunk in response.iter_content(1024 * 1024):
                size += len(chunk)
                if size > 500 * 1024 * 1024:
                    raise MotionError("Kie's generated video exceeds the 500MB download limit.")
                out.write(chunk)
    # Validate before publishing the local preview.
    duration(tmp)
    tmp.replace(dest)


class MotionQueue:
    def __init__(self, root: Path, download, reserve_id, upload):
        self.root = root
        self.download = download
        self.reserve_id = reserve_id
        self.upload = upload
        self.lock = threading.RLock()
        self.jobs = {}
        self.work = queue.Queue()
        self.started = False
        self.loaded = False
        self.load_error = None

    def load(self):
        with self.lock:
            if self.loaded:
                return
            self.root.mkdir(parents=True, exist_ok=True)
            path = self.root / "queue.json"
            if path.exists():
                try:
                    self.jobs = json.loads(path.read_text())
                except (ValueError, OSError) as e:
                    self.load_error = "Could not read the saved generation queue; restore queue.json before submitting more jobs."
                    raise MotionError(self.load_error) from e
            for job in self.jobs.values():
                state = job["status"]
                if state == "submitting":
                    job.update(status="unconfirmed", message="Restarted during submission. Check Kie Logs and attach the task ID.")
                elif state == "approving":
                    job.update(status="pending", message="Approval was interrupted. Approve again to safely finish the upload.")
                elif state in ACTIVE:
                    job.update(status="queued", message="Resuming saved job…")
                    self.work.put(job["id"])
            self.persist()
            self.loaded = True

    def persist(self):
        # Caller holds lock. Write first, then atomic rename: never partial JSON.
        path = self.root / "queue.json"
        tmp = path.with_suffix(".tmp")
        with tmp.open("w") as f:
            os.chmod(tmp, 0o600)
            json.dump(self.jobs, f)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)

    def start(self):
        with self.lock:
            self.load()
            if not self.started:
                threading.Thread(target=self.worker, daemon=True, name="motion-control").start()
                self.started = True

    def update(self, job, **values):
        with self.lock:
            previous = dict(job)
            job.update(values, updated_at=time.time())
            try:
                self.persist()
            except Exception:
                job.clear()
                job.update(previous)
                raise

    @staticmethod
    def identity(spec):
        # Combos share their source category's destination, so can't double-bill.
        return (spec["player"], spec["irl_folder_id"], spec["gameplay_id"])

    def enqueue(self, specs, images):
        if specs and not images:
            raise MotionError("No character images available for generation.")
        self.start()
        added = []
        remaining_images = []
        with self.lock:
            for spec in specs:
                existing = next((j for j in reversed(list(self.jobs.values()))
                                 if self.identity(j) == self.identity(spec)), None)
                if existing and existing["status"] not in {"error", "approved"}:
                    continue
                # Only newly queued clips consume an image. Each batch uses every
                # image once in shuffled order before starting another cycle.
                if not remaining_images:
                    remaining_images = list(images)
                    random.shuffle(remaining_images)
                image = remaining_images.pop()
                job = {**spec, "id": uuid.uuid4().hex, "status": "queued", "message": "Waiting to start",
                       "image_id": image["id"], "image_name": image["name"],
                       "created_at": time.time(), "updated_at": time.time()}
                # Retire resolved history for this clip when explicitly regenerating.
                if existing:
                    self.jobs.pop(existing["id"])
                self.jobs[job["id"]] = job
                try:
                    self.persist()
                except Exception:
                    self.jobs.pop(job["id"])
                    if existing:
                        self.jobs[existing["id"]] = existing
                    raise
                if existing:
                    shutil.rmtree(self.root / existing["id"], ignore_errors=True)
                self.work.put(job["id"])
                added.append(job["id"])
        return added

    def snapshot(self, player=None):
        self.load()
        with self.lock:
            positions = {j["id"]: i + 1 for i, j in enumerate(
                sorted((j for j in self.jobs.values() if j["status"] == "queued"),
                       key=lambda j: j.get("queued_at", j["created_at"])))}
            fields = ("id", "player", "category", "gameplay_id", "gameplay", "filename", "status",
                      "message", "original_duration", "extended", "target_duration", "image_name",
                      "motion_name",
                      "task_id", "progress", "created_at", "updated_at", "drive_file_id")
            return [{**{k: j.get(k) for k in fields}, "position": positions.get(j["id"]),
                     "preview_url": f"/motion/{j['id']}/preview" if j["status"] in {"pending", "approving"} else None}
                    for j in self.jobs.values() if player is None or j["player"] == player]

    def get(self, job_id):
        self.load()
        with self.lock:
            if job_id not in self.jobs:
                raise MotionError("Generation not found.")
            return self.jobs[job_id]

    def worker(self):
        while True:
            job_id = self.work.get()
            try:
                job = self.jobs.get(job_id)
                if job and job["status"] == "queued":
                    self.process(job)
            except Exception as e:
                if job:
                    uncertain = job.get("task_id") or job["status"] == "submitting" or isinstance(e, SubmissionUncertain)
                    try:
                        self.update(job, status="unconfirmed" if uncertain else "error", progress=None,
                                    message=error_message(e) + (" Check status to resume this task." if job.get("task_id") else ""))
                    except Exception as persist_error:
                        print(f"Motion queue could not save failure: {persist_error}")
            finally:
                self.work.task_done()

    def process(self, job):
        folder = self.root / job["id"]
        folder.mkdir(exist_ok=True)
        if not job.get("task_id"):
            # Old queued jobs used first-person gameplay. Require a fresh lookup
            # instead of silently submitting that obsolete reference after reload.
            if not job.get("motion_id") or not job.get("motion_name"):
                raise MotionError("This job has no Carrington motion reference. Retry generation to resolve the correct clip.")
            self.update(job, status="preparing", message="Downloading Carrington motion reference and character image…")
            source = folder / ("motion" + Path(job["motion_name"]).suffix.lower())
            image = folder / ("character" + Path(job["image_name"]).suffix.lower())
            self.download(job["motion_id"], source)
            self.download(job["image_id"], image)
            if image.stat().st_size > 10 * 1024 * 1024:
                raise MotionError("Character image exceeds Kling's 10MB limit.")
            self.update(job, message="Checking clip length / preparing motion reference…")
            prepared, original, extended = prepare_video(source, folder)
            self.update(job, original_duration=original, extended=extended,
                        target_duration=round(original - (0.1 if extended else 0), 6),
                        message="Uploading references to Kie…")
            image_url = upload_reference(image)
            video_url = upload_reference(prepared)
            self.update(job, status="submitting", image_url=image_url, video_url=video_url,
                        message="Submitting to Kling 2.6…")
            try:
                task_id = create_task(image_url, video_url)
            except SubmissionUncertain:
                raise
            except MotionError as e:
                self.update(job, status="error", message=str(e))
                return
            self.update(job, task_id=task_id, status="processing", message="Kling is generating…")
            # References are now hosted at Kie; don't retain large source copies.
            for path in folder.iterdir():
                path.unlink()
        self.update(job, status="processing", message="Checking Kling generation…", progress=None)
        deadline = time.monotonic() + 15 * 60
        delay = 5
        while not job.get("result_url"):
            data = task_detail(job["task_id"])
            if data.get("state") == "fail":
                self.update(job, status="error", message=str(data.get("failMsg") or "Kling generation failed.")[:600], progress=None)
                return
            if data.get("state") == "success":
                result = data.get("resultJson") or "{}"
                result = json.loads(result) if isinstance(result, str) else result
                urls = result.get("resultUrls") or []
                if not urls:
                    raise MotionError("Kie finished without a video URL.")
                self.update(job, result_url=urls[0])
                break
            if time.monotonic() >= deadline:
                raise MotionError("Kling is taking longer than 15 minutes.")
            progress = data.get("progress")
            if not isinstance(progress, (int, float)) or not 0 <= progress <= 100:
                progress = None
            self.update(job, message="Kling: " + str(data.get("state") or "processing"), progress=progress)
            time.sleep(delay)
            delay = min(30, delay * 1.5)
        self.update(job, status="downloading", message="Saving generated video for review…", progress=None)
        download_result(job["result_url"], folder / "generated.mp4")
        self.update(job, status="pending", message="Ready for review")

    def resume(self, job_id, task_id=""):
        self.start()
        with self.lock:
            job = self.get(job_id)
            if job["status"] != "unconfirmed":
                raise MotionError("Only unconfirmed jobs need a status check.")
            task_id = job.get("task_id") or task_id.strip()
            if not task_id or len(task_id) > 200:
                raise MotionError("Paste the task ID from Kie Logs to recover this generation.")
            if any(j.get("task_id") == task_id and j["id"] != job_id for j in self.jobs.values()):
                raise MotionError("That Kie task is already attached to another clip.")
            if not job.get("task_id"):
                data = task_detail(task_id)
                params = data.get("param") or "{}"
                params = json.loads(params) if isinstance(params, str) else params
                inputs = params.get("input", {})
                # Kie's live recordInfo response can encode input as JSON too.
                inputs = json.loads(inputs) if isinstance(inputs, str) else inputs
                if (params.get("model") != "kling-2.6/motion-control"
                        or inputs.get("input_urls") != [job.get("image_url")]
                        or inputs.get("video_urls") != [job.get("video_url")]):
                    raise MotionError("That task's references do not match this clip. Copy its exact task ID from Kie Logs.")
            self.update(job, task_id=task_id, result_url=None, status="queued", queued_at=time.time(),
                        message="Queued to check existing task")
            self.work.put(job_id)

    def discard(self, job_id):
        with self.lock:
            job = self.get(job_id)
            if job["status"] not in {"pending", "error"}:
                raise MotionError("Only a finished preview or failed generation can be discarded.")
            if job.get("upload_attempted"):
                raise MotionError("A Drive upload may already exist. Approve again to resolve it before discarding.")
            self.jobs.pop(job_id)
            try:
                self.persist()
            except Exception:
                self.jobs[job_id] = job
                raise
            shutil.rmtree(self.root / job_id, ignore_errors=True)

    def approve(self, job_id):
        with self.lock:
            job = self.get(job_id)
            if job["status"] == "approved":
                return
            if job["status"] != "pending":
                raise MotionError("This clip is not ready for approval.")
            auth.drive_write_headers()  # fail before encoding if reconnection is needed
            self.update(job, status="approving", message="Trimming and saving to Google Drive…")
        try:
            folder = self.root / job_id
            source, output = folder / "generated.mp4", folder / "approved.mp4"
            target = job["target_duration"]
            if duration(source) < target - 0.1:
                raise MotionError("Generated video is shorter than the motion reference. Discard it and generate again.")
            if not output.exists():
                tmp = folder / "approved.tmp.mp4"
                run_media(["ffmpeg", "-y", "-i", str(source), "-t", str(target), "-map", "0:v:0",
                           "-map", "0:a?", "-c:v", "libx264", "-threads", "2", "-preset", "fast", "-crf", "18",
                           "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", str(tmp)])
                if abs(duration(tmp) - target) > 0.11:
                    raise MotionError("Trimmed video duration does not match the expected length.")
                tmp.replace(output)
            if not job.get("drive_file_id"):
                self.update(job, drive_file_id=self.reserve_id())
            self.upload(output, job, lambda attempted: self.update(job, upload_attempted=attempted))
            self.update(job, status="approved", message="Saved to Google Drive")
            shutil.rmtree(folder, ignore_errors=True)
        except Exception as e:
            self.update(job, status="pending", message="Approval failed: " + error_message(e))
            raise
