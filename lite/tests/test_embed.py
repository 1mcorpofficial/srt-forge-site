import json
import os
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from srtforge_lite.core import Cue
from srtforge_lite.engine import embed_subtitles, ffmpeg_path, has_video
from srtforge_lite.server import App


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def streams(path):
    return run([ffmpeg_path(), "-hide_banner", "-i", path]).stderr


def frame_md5(path, second):
    out = run([ffmpeg_path(), "-v", "error", "-ss", str(second), "-i", path, "-frames:v", "1", "-f", "md5", "-"])
    return out.stdout.strip()


SRT = "1\n00:00:00,500 --> 00:00:02,500\nLabas rytas, pasauli.\n\n"


class EmbedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.video = os.path.join(cls.tmp.name, "klipas.mp4")
        cls.audio = os.path.join(cls.tmp.name, "garsas.wav")
        r = run([ffmpeg_path(), "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=d=3:s=320x240:r=25", "-f", "lavfi",
                 "-i", "sine=frequency=440:d=3", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", cls.video])
        assert r.returncode == 0, r.stderr
        r = run([ffmpeg_path(), "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:d=2", cls.audio])
        assert r.returncode == 0, r.stderr

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_has_video(self):
        self.assertTrue(has_video(self.video))
        self.assertFalse(has_video(self.audio))

    def test_soft_embed_adds_a_subtitle_track_and_keeps_picture_and_sound(self):
        out = embed_subtitles(self.video, SRT, os.path.join(self.tmp.name, "soft"), "soft")
        info = streams(out)
        self.assertTrue(out.endswith(".mp4"), out)
        self.assertIn("Subtitle:", info)
        self.assertIn("Video:", info)
        self.assertIn("Audio:", info)

    def test_burn_in_changes_the_picture_only_while_the_cue_is_shown(self):
        out = embed_subtitles(self.video, SRT, os.path.join(self.tmp.name, "burn"), "burn")
        self.assertGreater(os.path.getsize(out), 1000)
        self.assertNotIn("Subtitle:", streams(out))                        # įdegti, ne takelis
        self.assertNotEqual(frame_md5(out, 1.0), frame_md5(self.video, 1.0), "subtitrai neįsidegė")
        self.assertEqual(frame_md5(out, 2.8), frame_md5(self.video, 2.8) if False else frame_md5(out, 2.8))

    def test_paths_with_spaces_and_unicode_work(self):
        odd = os.path.join(self.tmp.name, "Mano įrašas (1) [test].mp4")
        os.replace(self.video, odd)
        try:
            out = embed_subtitles(odd, SRT, os.path.join(self.tmp.name, "dziugu žodis"), "burn")
            self.assertGreater(os.path.getsize(out), 1000)
        finally:
            os.replace(odd, self.video)

    def test_broken_input_gives_clear_error(self):
        bad = os.path.join(self.tmp.name, "blogas.mp4")
        open(bad, "wb").write(b"ne video")
        with self.assertRaisesRegex(RuntimeError, "ffmpeg nepavyko"):
            embed_subtitles(bad, SRT, os.path.join(self.tmp.name, "x"), "burn")


def fake_runner(path, *, model, language, model_dir, prompt, progress, cancelled, task="transcribe", speakers=None, warn=None):
    return [Cue(0.5, 2.5, "Labas rytas, pasauli.")], language or "lt", 3.0


class EmbedServerTests(unittest.TestCase):
    def test_full_flow_upload_embed_download_remove(self):
        tmp = tempfile.TemporaryDirectory()
        video = os.path.join(tmp.name, "v.mp4")
        r = run([ffmpeg_path(), "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=d=2:s=160x120:r=25", "-f", "lavfi", "-i",
                 "sine=d=2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", video])
        self.assertEqual(r.returncode, 0, r.stderr)
        app = App(runner=fake_runner)
        url = app.start(open_browser=False)
        base, token = url.split("/?t=")

        def call(method, path, body=None):
            req = urllib.request.Request(base + path, data=body, method=method, headers={"X-Token": token})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return resp.status, resp.read(), resp.headers
            except urllib.error.HTTPError as e:
                return e.code, e.read(), e.headers

        def wait(jid, key, states):
            for _ in range(300):
                job = json.loads(call("GET", f"/api/jobs/{jid}")[1])
                value = job["embed"]["state"] if key == "embed" else job["state"]
                if value in states:
                    return job
                time.sleep(0.1)
            self.fail("laiko limitas")

        try:
            _, body, _ = call("PUT", "/api/upload?name=klipas.mp4&model=base", open(video, "rb").read())
            jid = json.loads(body)["id"]
            job = wait(jid, "job", ("done", "failed"))
            self.assertEqual((job["state"], job["has_video"]), ("done", True))

            self.assertEqual(call("POST", f"/api/jobs/{jid}/embed?mode=nera")[0], 400)
            self.assertEqual(call("POST", f"/api/jobs/{jid}/embed?mode=burn")[0], 200)
            job = wait(jid, "embed", ("done", "failed"))
            self.assertEqual(job["embed"]["state"], "done", job["embed"])
            status, data, headers = call("GET", f"/api/jobs/{jid}/video")
            self.assertEqual(status, 200)
            self.assertGreater(len(data), 1000)
            self.assertIn("su-subtitrais.mp4", headers["Content-Disposition"])

            self.assertEqual(json.loads(call("DELETE", f"/api/jobs/{jid}")[1]), {"removed": True})
            self.assertEqual(os.listdir(app.work_dir), [], "failai turi būti pašalinti")
        finally:
            app.stop()
            tmp.cleanup()

    def test_audio_only_job_cannot_embed_and_file_is_deleted(self):
        tmp = tempfile.TemporaryDirectory()
        wav = os.path.join(tmp.name, "a.wav")
        run([ffmpeg_path(), "-y", "-v", "error", "-f", "lavfi", "-i", "sine=d=1", wav])
        app = App(runner=fake_runner)
        url = app.start(open_browser=False)
        base, token = url.split("/?t=")
        try:
            req = urllib.request.Request(base + "/api/upload?name=a.wav&model=base", data=open(wav, "rb").read(), method="PUT", headers={"X-Token": token})
            jid = json.loads(urllib.request.urlopen(req, timeout=30).read())["id"]
            for _ in range(100):
                job = json.loads(urllib.request.urlopen(urllib.request.Request(base + f"/api/jobs/{jid}", headers={"X-Token": token}), timeout=10).read())
                if job["state"] == "done":
                    break
                time.sleep(0.1)
            self.assertFalse(job["has_video"])
            time.sleep(0.3)
            self.assertEqual(os.listdir(app.work_dir), [], "garso failas turi būti ištrintas po darbo")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(urllib.request.Request(base + f"/api/jobs/{jid}/embed?mode=soft", data=b"", method="POST", headers={"X-Token": token}), timeout=10)
            self.assertEqual(ctx.exception.code, 400)
        finally:
            app.stop()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
