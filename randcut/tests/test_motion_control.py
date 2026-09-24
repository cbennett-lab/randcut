"""Run from randcut/: ../.venv/bin/python -m unittest discover -s tests -v.

All network access is mocked; ffmpeg integration cases use tiny synthetic clips.
"""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
import auth
import main
import motion_control as mc


def response(data=None, status=200, headers=None):
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(data or {}).encode()
    r.headers.update(headers or {})
    return r


IMAGES = [{"id": "image1", "name": "portrait.png"}]


def spec(**extra):
    return dict(player="elara", category="layups", gameplay_id="vr019", gameplay="layup_019.mp4",
                motion_id="carrington019", motion_name="Carrington_layup_019.mp4",
                irl_folder_id="irl-elara", filename="elara_layup_019.mp4", **extra)


class MotionQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.q = mc.MotionQueue(self.root, Mock(), Mock(return_value="reserved-id"), Mock())
        self.q.load()
        self.q.started = True  # exercise transitions deterministically without a worker

    def job(self):
        return self.q.get(self.q.enqueue([spec()], IMAGES)[0])

    def test_duplicate_batch_and_combo_sources_only_enqueue_once(self):
        ids = self.q.enqueue([spec(), spec()], IMAGES)
        self.assertEqual(len(ids), 1)
        self.assertEqual(self.q.enqueue([spec()], IMAGES), [])
        self.assertEqual(self.q.snapshot()[0]["position"], 1)
        self.q.update(self.q.get(ids[0]), status="pending")
        self.assertEqual(self.q.enqueue([spec()], IMAGES), [])

    def test_batch_uses_every_image_before_repeating_and_preserves_choices_on_restart(self):
        images = [{"id": f"image{i}", "name": f"portrait{i}.png"} for i in range(6)]
        for count in [1, 3, 6, 9, 14]:
            with self.subTest(clips=count):
                specs = [{**spec(), "gameplay_id": f"vr{count}-{i}"} for i in range(count)]
                ids = self.q.enqueue(specs, images)
                chosen = [self.q.get(job_id)["image_id"] for job_id in ids]
                self.assertEqual(len(chosen), count)
                for offset in range(0, count, len(images)):
                    cycle = chosen[offset:offset + len(images)]
                    self.assertEqual(len(set(cycle)), len(cycle))
                    self.assertTrue(set(cycle) <= {image["id"] for image in images})
                restored = mc.MotionQueue(self.root, Mock(), Mock(), Mock())
                restored.load()
                self.assertEqual([restored.get(job_id)["image_id"] for job_id in ids], chosen)
                for job_id in ids:
                    job = restored.get(job_id)
                    self.assertEqual(job["image_name"], next(image["name"] for image in images
                                                            if image["id"] == job["image_id"]))

    def test_skipped_jobs_and_duplicate_combo_sources_do_not_consume_images(self):
        queued = self.job()
        pending_spec = {**spec(), "gameplay_id": "already-pending"}
        pending = self.q.get(self.q.enqueue([pending_spec], IMAGES)[0])
        self.q.update(pending, status="pending")
        specs = [{**spec(), "gameplay_id": f"new{i}"} for i in range(6)]
        images = [{"id": f"image{i}", "name": f"portrait{i}.png"} for i in range(6)]
        candidates = [specs[0], specs[0], spec(), pending_spec, *specs[1:]]
        with patch.object(mc.random, "shuffle", side_effect=lambda pool: None):
            ids = self.q.enqueue(candidates, images)
        self.assertEqual(len(ids), 6)
        self.assertEqual({self.q.get(job_id)["image_id"] for job_id in ids},
                         {image["id"] for image in images})
        self.assertEqual(queued["image_name"], "portrait.png")
        self.assertEqual(pending["image_name"], "portrait.png")
        self.assertEqual(pending["status"], "pending")

    def test_no_missing_clips_is_a_noop_but_missing_images_cannot_queue(self):
        self.assertEqual(self.q.enqueue([], []), [])
        with self.assertRaisesRegex(mc.MotionError, "No character images"):
            self.q.enqueue([spec()], [])
        self.assertEqual(self.q.snapshot(), [])

    def test_restart_preserves_pending_and_resumes_known_task_but_not_submission(self):
        job = self.job()
        for state, expected in [("pending", "pending"), ("submitting", "unconfirmed"),
                                ("processing", "queued"), ("approving", "pending")]:
            self.q.update(job, status=state, task_id="existing-task")
            restored = mc.MotionQueue(self.root, Mock(), Mock(), Mock())
            restored.load()
            self.assertEqual(restored.get(job["id"])["status"], expected)
            self.assertEqual(restored.get(job["id"])["task_id"], "existing-task")

    def test_corrupt_queue_blocks_new_paid_jobs(self):
        (self.root / "queue.json").write_text("bad json")
        restored = mc.MotionQueue(self.root, Mock(), Mock(), Mock())
        with self.assertRaises(mc.MotionError):
            restored.enqueue([spec()], IMAGES)
        self.assertEqual((self.root / "queue.json").read_text(), "bad json")

    def test_discard_removes_local_preview_and_allows_regeneration(self):
        job = self.job()
        self.q.update(job, status="pending")
        folder = self.root / job["id"]
        folder.mkdir()
        (folder / "generated.mp4").write_bytes(b"preview")
        self.q.discard(job["id"])
        self.assertFalse(folder.exists())
        self.assertEqual(self.q.snapshot(), [])
        self.assertEqual(len(self.q.enqueue([spec()], IMAGES)), 1)

    def test_active_and_uncertain_upload_cannot_be_discarded(self):
        job = self.job()
        with self.assertRaises(mc.MotionError):
            self.q.discard(job["id"])
        self.q.update(job, status="pending", upload_attempted=True)
        with self.assertRaises(mc.MotionError):
            self.q.discard(job["id"])

    @patch.object(mc, "download_result")
    @patch.object(mc, "task_detail", return_value={"state": "success", "resultJson": '{"resultUrls":["https://example.com/video.mp4"]}'})
    @patch.object(mc, "create_task")
    def test_resume_downloads_result_without_new_paid_request(self, create, detail, download):
        job = self.job()
        self.q.update(job, task_id="already-paid", original_duration=2.5, target_duration=2.4)
        self.q.process(job)
        create.assert_not_called()
        self.q.download.assert_not_called()
        self.assertEqual(job["status"], "pending")
        self.assertEqual(download.call_count, 1)

    @patch.object(mc, "task_detail", return_value={"state": "fail", "failMsg": "Invalid reference"})
    def test_failed_task_becomes_retryable_and_keeps_reason(self, _):
        job = self.job()
        self.q.update(job, task_id="failed-task")
        self.q.process(job)
        self.assertEqual(job["status"], "error")
        self.assertEqual(job["message"], "Invalid reference")
        self.assertEqual(len(self.q.enqueue([spec()], IMAGES)), 1)

    @patch.object(mc, "task_detail", return_value={"param": '{"model":"other"}'})
    def test_recovery_rejects_unrelated_task(self, _):
        job = self.job()
        self.q.update(job, status="unconfirmed", image_url="https://image", video_url="https://video")
        with self.assertRaises(mc.MotionError):
            self.q.resume(job["id"], "wrong-task")
        self.assertEqual(job["status"], "unconfirmed")

    def test_storage_failure_rolls_back_status_before_submitting(self):
        job = self.job()
        with patch.object(self.q, "persist", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.q.update(job, status="submitting")
        self.assertEqual(job["status"], "queued")

    def test_worker_sends_carrington_video_and_trims_using_its_original_length(self):
        job = self.job()
        def download(file_id, path):
            path.write_bytes(b"test reference")
        self.q.download.side_effect = download
        with patch.object(mc, "prepare_video", return_value=(self.root / "prepared.mp4", 2.5, True)) as prepare, \
             patch.object(mc, "upload_reference", side_effect=["https://image", "https://motion"]) as upload, \
             patch.object(mc, "create_task", return_value="new-task") as create, \
             patch.object(mc, "task_detail", return_value={"state": "fail", "failMsg": "Test completion"}):
            self.q.process(job)
        self.assertEqual([c.args[0] for c in self.q.download.call_args_list], ["carrington019", "image1"])
        self.assertEqual(prepare.call_args.args[0].name, "motion.mp4")
        self.assertEqual(upload.call_args_list[1].args[0], self.root / "prepared.mp4")
        create.assert_called_once_with("https://image", "https://motion")
        self.assertEqual(job["original_duration"], 2.5)
        self.assertEqual(job["target_duration"], 2.4)
        self.assertEqual(self.q.snapshot()[0]["motion_name"], "Carrington_layup_019.mp4")

    def test_legacy_unsent_gameplay_job_never_reaches_kie(self):
        job = self.job()
        job.pop("motion_id")
        job.pop("motion_name")
        with patch.object(mc, "create_task") as create:
            with self.assertRaisesRegex(mc.MotionError, "no Carrington motion reference"):
                self.q.process(job)
        create.assert_not_called()
        self.q.download.assert_not_called()

    def test_recovery_accepts_kies_nested_json_input(self):
        job = self.job()
        self.q.update(job, status="unconfirmed", image_url="https://image", video_url="https://motion")
        params = {"model": "kling-2.6/motion-control", "input": json.dumps({
            "input_urls": ["https://image"], "video_urls": ["https://motion"]})}
        with patch.object(mc, "task_detail", return_value={"param": json.dumps(params)}):
            self.q.resume(job["id"], "recovered-task")
        self.assertEqual(job["task_id"], "recovered-task")
        self.assertEqual(job["status"], "queued")


class KieContractTests(unittest.TestCase):
    @patch.object(auth, "kie_token", return_value="test-key")
    @patch.object(mc.requests, "post", return_value=response({"code": 200, "data": {"taskId": "task1"}}))
    def test_motion_request_uses_documented_fields_and_exact_prompt(self, post, _):
        self.assertEqual(mc.create_task("https://image", "https://video"), "task1")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload, {"model": "kling-2.6/motion-control", "input": {
            "input_urls": ["https://image"], "video_urls": ["https://video"],
            "character_orientation": "video", "mode": "720p", "prompt": mc.PROMPT}})

    @patch.object(auth, "kie_token", return_value="test-key")
    def test_timeout_bad_json_and_server_error_are_not_safe_to_resubmit(self, _):
        for value in [requests.Timeout(), response(status=502), response({"code": 500}), response({"code": 200})]:
            with patch.object(mc.requests, "post", **({"side_effect": value} if isinstance(value, Exception) else {"return_value": value})):
                with self.assertRaises(mc.SubmissionUncertain):
                    mc.create_task("https://image", "https://video")
        malformed = response()
        malformed._content = b"not json"
        with patch.object(mc.requests, "post", return_value=malformed):
            with self.assertRaises(mc.SubmissionUncertain):
                mc.create_task("https://image", "https://video")

    @patch.object(auth, "kie_token", return_value="test-key")
    @patch.object(mc.requests, "post", return_value=response({"code": 422, "msg": "Invalid image"}))
    def test_explicit_rejection_is_retryable(self, *_):
        with self.assertRaises(mc.MotionError) as error:
            mc.create_task("https://image", "https://video")
        self.assertNotIsInstance(error.exception, mc.SubmissionUncertain)


class DriveIntegrationTests(unittest.TestCase):
    def test_old_readonly_grant_cannot_upload(self):
        with patch.object(auth, "_read_store", return_value={"google": {"scopes": ["https://www.googleapis.com/auth/drive.readonly"]}}):
            self.assertFalse(auth.google_connection()["can_write"])
            with self.assertRaises(auth.AuthError):
                auth.drive_write_headers()
        self.assertIn("https://www.googleapis.com/auth/drive", auth.SCOPES)

    @patch.object(auth, "kie_token", return_value="test-key")
    def test_combo_resolves_source_destination_and_correct_filename(self, _):
        source = {"label": "Layups", "vr_folder": main.folder_id_to_url("gameplay"),
                  "players": {"elara": {"display": "Elara", "irl_folder": main.folder_id_to_url("elara-irl")},
                              "carrington": {"display": "Carrington", "irl_folder": main.folder_id_to_url("carrington-irl")}}}
        combo = {"label": "Combo", "type": "combo", "players": source["players"],
                 "recipe": {"segments": [{"source": "Layups", "count": 1}]}}
        files = {"gameplay": [{"id": "vr019", "name": "layup_019.mp4"}, {"id": "vr020", "name": "layup_020.mp4"}],
                 "elara-irl": [{"id": "irl20", "name": "elara_layup_020.mp4"}],
                 "carrington-irl": [{"id": "wrong-number", "name": "Carrington_layup_001.mp4"},
                                    {"id": "motion19", "name": "Carrington_layup_019.mp4"}],
                 "images": [{"id": "image1", "name": "one.png"}, {"id": "image2", "name": "two.jpg"}]}
        with patch.object(main, "STACKED_CATEGORIES", {"layups": source, "combo": combo}), \
             patch.object(main, "PLAYER_IMAGE_FOLDERS", {"elara": "images"}), \
             patch.object(main, "list_drive_files", side_effect=lambda folder, _: files[folder]):
            specs, images = main.motion_specs(main.MotionRequest(player="elara", category="combo"))
            # Missing references must not fall back to Gameplay or another number.
            files["carrington-irl"] = files["carrington-irl"][:1]
            with self.assertRaisesRegex(mc.MotionError, "No matching Carrington"):
                main.motion_specs(main.MotionRequest(player="elara", category="combo"))
            source["players"].pop("carrington")
            with self.assertRaisesRegex(mc.MotionError, "no IRL/Carrington folder"):
                main.motion_specs(main.MotionRequest(player="elara", category="combo"))
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["filename"], "elara_layup_019.mp4")
        self.assertEqual(specs[0]["irl_folder_id"], "elara-irl")
        self.assertEqual(specs[0]["category"], "layups")
        self.assertEqual(specs[0]["motion_id"], "motion19")
        self.assertEqual(specs[0]["motion_name"], "Carrington_layup_019.mp4")
        self.assertEqual(images, files["images"])

    @patch.object(auth, "drive_auth", return_value=({}, {}))
    def test_inventory_paginates_and_ignores_trashed_files(self, _):
        with patch.object(main.requests, "get", side_effect=[response({"files": [{"id": "1"}], "nextPageToken": "page2"}),
                                                           response({"files": [{"id": "2"}]})]) as get:
            self.assertEqual(len(main.list_drive_files("folder", "video/")), 2)
            self.assertIn("trashed = false", get.call_args.kwargs["params"]["q"])
            self.assertEqual(get.call_count, 2)

    @patch.object(auth, "drive_write_headers", return_value={"Authorization": "Bearer test"})
    def test_approval_retry_recognizes_already_uploaded_file_without_post(self, _):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "clip.mp4"
            path.write_bytes(b"same video")
            job = {**spec(), "drive_file_id": "reserved-id"}
            metadata = {"id": "reserved-id", "name": job["filename"], "parents": [job["irl_folder_id"]],
                        "md5Checksum": hashlib.md5(path.read_bytes()).hexdigest()}
            with patch.object(main.requests, "get", return_value=response(metadata)), patch.object(main.requests, "post") as post:
                main.upload_approved_clip(path, job, Mock())
                post.assert_not_called()


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg required")
class RealMediaTests(unittest.TestCase):
    def test_pingpong_and_approval_trim_with_and_without_audio(self):
        for length, audio in [(2.5, True), (0.4, False), (3.2, False)]:
            with self.subTest(length=length, audio=audio), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                source = root / "original.mp4"
                args = ["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc2=size=160x120:rate=30:duration={length}"]
                if audio:
                    args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={length}", "-c:a", "aac"]
                mc.run_media(args + ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)])
                before = source.read_bytes()
                prepared, original, extended = mc.prepare_video(source, root)
                self.assertEqual(source.read_bytes(), before)
                self.assertEqual(extended, length < 3)
                self.assertGreaterEqual(mc.duration(prepared), 3)
                q = mc.MotionQueue(root / "queue", Mock(), Mock(return_value="reserved"), Mock())
                q.load()
                q.started = True
                job = q.get(q.enqueue([spec()], IMAGES)[0])
                target = original - (0.1 if extended else 0)
                q.update(job, status="pending", original_duration=original, target_duration=target)
                dest = q.root / job["id"]
                dest.mkdir()
                shutil.copyfile(prepared, dest / "generated.mp4")
                calls = []
                def upload(path, record, mark):
                    calls.append((mc.duration(path), record["drive_file_id"]))
                q.upload = upload
                with patch.object(auth, "drive_write_headers", return_value={}):
                    q.approve(job["id"])
                    q.approve(job["id"])
                self.assertEqual(len(calls), 1)
                self.assertAlmostEqual(calls[0][0], target, delta=.04)
                self.assertEqual(job["status"], "approved")
                self.assertFalse(dest.exists())


if __name__ == "__main__":
    unittest.main()
