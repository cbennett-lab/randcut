"""Real ASGI routes with isolated state, without external requests or startup."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import auth
import main
import motion_control as mc
from test_motion_control import IMAGES, spec


def call(method, path, payload=None):
    async def run():
        sent = []
        requested = False
        complete = asyncio.Event()
        body = json.dumps(payload).encode() if payload is not None else b""
        async def receive():
            nonlocal requested
            if requested:
                await complete.wait()
                return {"type": "http.disconnect"}
            requested = True
            return {"type": "http.request", "body": body, "more_body": False}
        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                complete.set()
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
                 "scheme": "http", "path": path, "raw_path": path.encode(), "root_path": "", "query_string": b"",
                 "headers": [(b"content-type", b"application/json")], "server": ("testserver", 80), "client": ("127.0.0.1", 1000)}
        await main.app(scope, receive, send)
        start = next(m for m in sent if m["type"] == "http.response.start")
        data = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        return start["status"], data, dict(start["headers"])
    return asyncio.run(run())


class MotionRoutesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.q = mc.MotionQueue(Path(self.temp.name), Mock(), Mock(), Mock())
        self.q.load()
        self.q.started = True
        p = patch.object(main, "MOTION", self.q)
        p.start()
        self.addCleanup(p.stop)

    def test_generation_and_preview_are_protected_by_login_gate(self):
        with patch.object(auth, "enforcing", return_value=True), patch.object(auth, "current_user", return_value=None):
            for method, path in [("GET", "/motion"), ("GET", "/motion/job/preview"),
                                 ("HEAD", "/motion/job/preview"), ("POST", "/motion/generate"),
                                 ("POST", "/motion/job/approve"), ("DELETE", "/motion/job")]:
                self.assertEqual(call(method, path)[0], 401)

    def test_preview_head_get_and_discard(self):
        job = self.q.get(self.q.enqueue([spec()], IMAGES)[0])
        self.q.update(job, status="pending")
        folder = self.q.root / job["id"]
        folder.mkdir()
        (folder / "generated.mp4").write_bytes(b"local-preview")
        with patch.object(auth, "enforcing", return_value=False):
            url = f"/motion/{job['id']}/preview"
            status, body, headers = call("GET", url)
            self.assertEqual((status, body), (200, b"local-preview"))
            self.assertEqual(headers[b"cache-control"], b"private, no-store")
            self.assertEqual(call("HEAD", url)[:2], (200, b""))
            self.assertEqual(call("DELETE", f"/motion/{job['id']}")[0], 200)
            self.assertEqual(json.loads(call("GET", "/motion")[1]), {"jobs": []})

    def test_generate_deduplicates_repeated_requests(self):
        with patch.object(auth, "enforcing", return_value=False), \
             patch.object(main, "motion_specs", return_value=([spec()], IMAGES)):
            payload = {"player": "elara", "category": "layups", "gameplay_id": "vr019"}
            first = call("POST", "/motion/generate", payload)
            second = call("POST", "/motion/generate", payload)
            self.assertEqual(first[0], 200)
            self.assertEqual(json.loads(first[1])["queued"], 1)
            self.assertEqual(json.loads(second[1])["queued"], 0)

    def test_bad_inputs_never_reach_worker(self):
        with patch.object(auth, "enforcing", return_value=False):
            self.assertEqual(call("POST", "/motion/generate", {"player": ""})[0], 422)
            self.assertEqual(call("POST", "/motion/missing/approve")[0], 400)
            self.assertEqual(self.q.snapshot(), [])


if __name__ == "__main__":
    unittest.main()
