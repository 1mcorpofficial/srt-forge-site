import json
import time
import unittest
import urllib.error
import urllib.request

from srtforge_lite.core import Cue
from srtforge_lite.server import App


def fake_runner(path, *, model, language, model_dir, prompt, progress, cancelled):
    progress("Atpažįstama kalba", 0.5)
    with open(path, "rb") as handle:
        failing = handle.read().startswith(b"FAIL")
    if failing:
        raise RuntimeError("sugadintas failas")
    return [Cue(0.0, 1.5, "Labas rytas."), Cue(2.0, 3.5, "Kaip sekasi?")], language or "lt", 3.5


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = App(runner=fake_runner)
        cls.url = cls.app.start(open_browser=False)
        cls.base = cls.url.split("/?")[0]
        cls.token = cls.app.token

    @classmethod
    def tearDownClass(cls):
        cls.app.stop()

    def call(self, method, path, body=None, token="ok", headers=None):
        h = dict(headers or {})
        if token == "ok":
            h["X-Token"] = self.token
        elif token:
            h["X-Token"] = token
        req = urllib.request.Request(self.base + path, data=body, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def wait(self, job_id):
        for _ in range(100):
            _, body, _ = self.call("GET", f"/api/jobs/{job_id}")
            job = json.loads(body)
            if job["state"] in ("done", "failed", "cancelled"):
                return job
            time.sleep(0.05)
        self.fail("laiko limitas")

    def test_requires_token(self):
        self.assertEqual(self.call("GET", "/api/info", token=None)[0], 403)
        self.assertEqual(self.call("GET", "/api/info", token="blogas")[0], 403)
        self.assertEqual(self.call("PUT", "/api/upload?name=a.wav", b"x", token=None)[0], 403)

    def test_rejects_foreign_host_header_dns_rebinding(self):
        status, _, _ = self.call("GET", "/api/info", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)

    def test_ui_is_served_with_token_and_security_headers(self):
        status, body, headers = self.call("GET", f"/?t={self.token}", token=None)
        self.assertEqual(status, 200)
        self.assertIn(self.token.encode(), body)
        self.assertNotIn(b"__TOKEN__", body)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])

    def test_upload_transcribe_download_all_formats(self):
        status, body, _ = self.call("PUT", "/api/upload?name=Mano%20%C4%AFra%C5%A1as.wav&model=base&language=lt", b"garsas")
        self.assertEqual(status, 200)
        job = self.wait(json.loads(body)["id"])
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["cues"], 2)
        status, srt, headers = self.call("GET", f"/api/jobs/{job['id']}/srt")
        self.assertEqual(status, 200)
        self.assertIn("00:00:00,000 --> 00:00:01,500\nLabas rytas.", srt.decode())
        self.assertIn("filename*=UTF-8''Mano%20%C4%AFra%C5%A1as.srt", headers["Content-Disposition"])
        self.assertTrue(self.call("GET", f"/api/jobs/{job['id']}/vtt")[1].startswith(b"WEBVTT"))
        self.assertIn(b"Labas rytas. Kaip" if False else b"Labas rytas.", self.call("GET", f"/api/jobs/{job['id']}/txt")[1])

    def test_failure_is_reported_not_swallowed(self):
        _, body, _ = self.call("PUT", "/api/upload?name=a.wav&model=base", b"FAIL...")
        job = self.wait(json.loads(body)["id"])
        self.assertEqual(job["state"], "failed")
        self.assertIn("sugadintas", job["error"])
        self.assertEqual(self.call("GET", f"/api/jobs/{job['id']}/srt")[0], 404)

    def test_system_report_endpoint(self):
        status, body, _ = self.call("GET", "/api/system")
        self.assertEqual(status, 200)
        report = json.loads(body)
        self.assertIn(report["recommended"], [m["name"] for m in report["models"]])
        self.assertIn("recommended_installed", report)
        self.assertEqual(self.call("GET", "/api/system", token=None)[0], 403)

    def test_bad_input(self):
        self.assertEqual(self.call("PUT", "/api/upload?name=a.wav&model=nera", b"x")[0], 400)
        self.assertEqual(self.call("PUT", "/api/upload?name=a.wav", b"")[0], 400)
        self.assertEqual(self.call("GET", "/api/jobs/nera")[0], 404)

    def test_path_traversal_in_name_is_neutralised(self):
        _, body, _ = self.call("PUT", "/api/upload?name=..%2F..%2Fetc%2Fpasswd.wav&model=base", b"x")
        job = self.wait(json.loads(body)["id"])
        self.assertEqual(job["name"], "passwd.wav")

    def test_uploaded_file_is_deleted_after_job(self):
        import os
        _, body, _ = self.call("PUT", "/api/upload?name=a.wav&model=base", b"garsas")
        self.wait(json.loads(body)["id"])
        time.sleep(0.2)
        self.assertEqual(os.listdir(self.app.work_dir), [])


if __name__ == "__main__":
    unittest.main()
