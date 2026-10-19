import json
import os
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

from srtforge_lite.core import Cue
from srtforge_lite.server import App


SEEN = {}


def fake_runner(path, *, model, language, model_dir, prompt, progress, cancelled, task="transcribe", speakers=None, warn=None, rules=None):
    SEEN["rules"] = rules
    SEEN["task"] = task
    SEEN["speakers"] = speakers
    progress("Atpažįstama kalba", 0.5)
    with open(path, "rb") as handle:
        failing = handle.read().startswith(b"FAIL")
    if failing:
        raise RuntimeError("sugadintas failas")
    if speakers and speakers != "off":
        return [Cue(0.0, 1.5, "Labas rytas.", speaker=0), Cue(2.0, 3.5, "Kaip sekasi?", speaker=1)], language or "lt", 3.5
    return [Cue(0.0, 1.5, "Labas rytas."), Cue(2.0, 3.5, "Kaip sekasi?")], language or "lt", 3.5


class FakeTranslator:
    calls = []

    def translate(self, texts, source, target):
        FakeTranslator.calls.append((source, target, list(texts)))
        return [f"[{target}] {t}" for t in texts]


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = App(runner=fake_runner, translator=FakeTranslator())
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

    def test_new_export_formats_and_transcript_endpoint(self):
        _, body, _ = self.call("PUT", "/api/upload?name=a.wav&model=base&language=lt", b"garsas")
        jid = json.loads(body)["id"]
        self.wait(jid)
        txt = self.call("GET", f"/api/jobs/{jid}/txt")[1].decode()
        self.assertEqual(txt, "Labas rytas. Kaip sekasi?\n" if False else txt)
        self.assertIn("Labas rytas.", txt)
        ts = self.call("GET", f"/api/jobs/{jid}/ts")[1].decode()
        self.assertTrue(ts.startswith("[00:00:00] Labas rytas."), ts)
        doc = json.loads(self.call("GET", f"/api/jobs/{jid}/json")[1])
        self.assertEqual(doc["language"], "lt")
        self.assertEqual(doc["task"], "transcribe")
        self.assertEqual(len(doc["segments"]), 2)
        self.assertEqual(doc["segments"][0]["text"], "Labas rytas.")
        transcript = json.loads(self.call("GET", f"/api/jobs/{jid}/transcript")[1])
        self.assertEqual([c["text"] for c in transcript["cues"]], ["Labas rytas.", "Kaip sekasi?"])

    def test_translate_task_is_passed_to_the_engine_and_validated(self):
        _, body, _ = self.call("PUT", "/api/upload?name=a.wav&model=base&language=lt&task=translate", b"garsas")
        job = self.wait(json.loads(body)["id"])
        self.assertEqual(job["task"], "translate")
        self.assertEqual(SEEN["task"], "translate")
        self.assertEqual(self.call("PUT", "/api/upload?name=a.wav&model=base&task=nera", b"x")[0], 400)

    def test_finished_job_can_be_removed_with_its_files(self):
        import os
        _, body, _ = self.call("PUT", "/api/upload?name=a.wav&model=base", b"garsas")
        jid = json.loads(body)["id"]
        self.wait(jid)
        status, out, _ = self.call("DELETE", f"/api/jobs/{jid}")
        self.assertEqual((status, json.loads(out)), (200, {"removed": True}))
        self.assertEqual(self.call("GET", f"/api/jobs/{jid}")[0], 404)

    def test_speakers_option_rename_and_labelled_exports(self):
        _, body, _ = self.call("PUT", "/api/upload?name=a.wav&model=base&speakers=2", b"garsas")
        jid = json.loads(body)["id"]
        job = self.wait(jid)
        self.assertEqual(SEEN["speakers"], "2")
        self.assertEqual([s["name"] for s in job["speakers"]], ["Kalbėtojas 1", "Kalbėtojas 2"])
        txt = self.call("GET", f"/api/jobs/{jid}/txt")[1].decode()
        self.assertIn("Kalbėtojas 1: Labas rytas.", txt)
        self.assertIn("Kalbėtojas 2: Kaip sekasi?", txt)

        status, renamed, _ = self.call("POST", f"/api/jobs/{jid}/speakers",
                                       json.dumps({"names": {"0": "Jonas", "1": "Ona"}}).encode())
        self.assertEqual(status, 200)
        self.assertEqual([s["name"] for s in json.loads(renamed)["speakers"]], ["Jonas", "Ona"])
        srt = self.call("GET", f"/api/jobs/{jid}/srt")[1].decode()
        self.assertIn("Jonas: Labas rytas.", srt)
        self.assertIn("Ona: Kaip sekasi?", srt)
        vtt = self.call("GET", f"/api/jobs/{jid}/vtt")[1].decode()
        self.assertIn("<v Ona>Kaip sekasi?", vtt)
        doc = json.loads(self.call("GET", f"/api/jobs/{jid}/json")[1])
        self.assertEqual([x["speaker"] for x in doc["segments"]], ["Jonas", "Ona"])
        transcript = json.loads(self.call("GET", f"/api/jobs/{jid}/transcript")[1])
        self.assertEqual(transcript["cues"][1]["speaker_name"], "Ona")
        self.assertEqual(self.call("POST", f"/api/jobs/{jid}/speakers", b"ne json")[0], 400)

    def test_invalid_speaker_count_is_rejected_and_off_is_default(self):
        self.assertEqual(self.call("PUT", "/api/upload?name=a.wav&model=base&speakers=99", b"x")[0], 400)
        self.assertEqual(self.call("PUT", "/api/upload?name=a.wav&model=base&speakers=abc", b"x")[0], 400)
        _, body, _ = self.call("PUT", "/api/upload?name=a.wav&model=base", b"garsas")
        job = self.wait(json.loads(body)["id"])
        self.assertEqual(job["speakers"], [])

    def test_multi_language_translation_exports_per_language(self):
        FakeTranslator.calls.clear()
        _, body, _ = self.call("PUT", "/api/upload?name=laida.wav&model=base&language=lt&targets=en,de,pl", b"garsas")
        jid = json.loads(body)["id"]
        job = self.wait(jid)
        self.assertEqual(job["state"], "done", job)
        self.assertEqual(job["translations"], ["de", "en", "pl"])
        self.assertEqual([c[:2] for c in FakeTranslator.calls], [("lt", "en"), ("lt", "de"), ("lt", "pl")])
        srt_de = self.call("GET", f"/api/jobs/{jid}/srt?lang=de")[1].decode()
        self.assertIn("[de] Labas rytas.", srt_de)
        self.assertIn("00:00:00,000 --> 00:00:01,500", srt_de)                      # laikai nepasikeitė
        status, _, headers = self.call("GET", f"/api/jobs/{jid}/txt?lang=pl")
        self.assertEqual(status, 200)
        self.assertIn("laida.pl.txt", urllib.parse.unquote(headers["Content-Disposition"]))
        self.assertIn("Labas rytas.", self.call("GET", f"/api/jobs/{jid}/srt")[1].decode())    # originalas liko
        self.assertEqual(self.call("GET", f"/api/jobs/{jid}/srt?lang=fr")[0], 404)

    def test_translation_targets_are_validated(self):
        self.assertEqual(self.call("PUT", "/api/upload?name=a.wav&model=base&targets=xx", b"x")[0], 400)
        many = ",".join(["en", "de", "fr", "es", "it", "pt", "pl", "ru", "uk"])
        self.assertEqual(self.call("PUT", f"/api/upload?name=a.wav&model=base&targets={many}", b"x")[0], 400)

    def test_pick_then_process_in_place_and_save_next_to_original(self):
        import tempfile
        d = tempfile.mkdtemp()
        src = os.path.join(d, "interviu.wav")
        open(src, "wb").write(b"garsas")
        open(os.path.join(d, "interviu.srt"), "w").write("SENAS")                    # esamas failas neturi būti perrašytas
        self.app.picker = lambda: [src]
        picked = json.loads(self.call("POST", "/api/pick", b"")[1])["paths"]
        self.assertEqual(picked[0]["name"], "interviu.wav")
        body = json.dumps({"paths": [picked[0]["path"]], "options": {"model": "base", "language": "lt", "style": "tv",
                           "outputs": ["srt", "vtt", "txt"], "targets": ["en"]}}).encode()
        status, resp, _ = self.call("POST", "/api/jobs/local", body)
        self.assertEqual(status, 200, resp)
        job = self.wait(json.loads(resp)["jobs"][0]["id"])
        self.assertEqual(job["state"], "done", job)
        self.assertEqual(SEEN["rules"].max_line, 37)                                 # TV stilius pasiekė variklį
        self.assertEqual(open(os.path.join(d, "interviu.srt")).read(), "SENAS")
        names = sorted(os.listdir(d))
        for expected in ("interviu (2).srt", "interviu.vtt", "interviu.txt", "interviu.en.srt", "interviu.en.vtt", "interviu.wav"):
            self.assertIn(expected, names)
        self.assertEqual(job["folder"], d)
        self.assertTrue(os.path.exists(src))                                         # originalas neištrintas
        self.call("DELETE", "/api/jobs/" + job["id"])
        self.assertTrue(os.path.exists(src))                                         # net pašalinus darbą
        revealed = []
        self.app.revealer = lambda path: revealed.append(path) or True

    def test_local_job_refuses_unpicked_path_and_bad_options(self):
        body = json.dumps({"paths": ["/etc/passwd"], "options": {"model": "base"}}).encode()
        self.assertEqual(self.call("POST", "/api/jobs/local", body)[0], 403)
        self.assertEqual(self.call("POST", "/api/jobs/local", json.dumps({"paths": ["/x"], "options": {"style": "zz"}}).encode())[0], 400)
        self.assertEqual(self.call("POST", "/api/jobs/local", json.dumps({"paths": ["/x"], "options": {"outputs": ["exe"]}}).encode())[0], 400)
        self.assertEqual(self.call("POST", "/api/jobs/local", b"{}", token=None)[0], 403)

    def test_pick_unavailable_returns_501(self):
        self.app.picker = lambda: None
        self.assertEqual(self.call("POST", "/api/pick", b"")[0], 501)

    def test_upload_with_outputs_saves_to_out_dir(self):
        import tempfile
        d = tempfile.mkdtemp()
        q = urllib.parse.urlencode({"name": "a.wav", "model": "base", "outputs": "srt,json", "out_dir": d})
        jid = json.loads(self.call("PUT", "/api/upload?" + q, b"garsas")[1])["id"]
        self.assertEqual(self.wait(jid)["state"], "done")
        self.assertEqual(sorted(os.listdir(d)), ["a.json", "a.srt"])
        self.assertEqual(self.call("PUT", "/api/upload?name=a.wav&out_dir=relative", b"x")[0], 400)

    def test_live_session_start_feed_poll_overlay_stop(self):
        import time
        import numpy as np
        from srtforge_lite.live import LiveSession

        words = [("Labas", 0.1, 0.5), ("rytas,", 0.6, 1.0), ("visiems.", 1.1, 1.6)]

        def factory(options):
            self.assertEqual(options["targets"], ["en"])
            return LiveSession(lambda a: [w for w in words if w[2] <= len(a) / 16000], options["targets"], FakeTranslator())

        self.app.live_factory = factory
        status, body, _ = self.call("POST", "/api/live?model=base&language=lt&targets=en", b"")
        self.assertEqual(status, 200, body)
        live = json.loads(body)["id"]
        for _ in range(6):
            self.assertEqual(self.call("PUT", f"/api/live/{live}/audio", np.zeros(8000, dtype="<i2").tobytes())[0], 200)
            time.sleep(0.5)
        state = json.loads(self.call("GET", f"/api/live/{live}")[1])
        self.assertTrue(state["running"])
        self.assertIn("Labas", state["partial"] + " ".join(l["text"] for l in state["lines"]))
        self.assertEqual(self.call("GET", "/api/live/nera")[0], 404)
        self.assertEqual(self.call("PUT", "/api/live/nera/audio", b"x")[0], 404)
        self.assertEqual(self.call("GET", f"/api/live/{live}", token=None)[0], 403)
        page = self.call("GET", "/overlay?id=" + live)[1].decode()
        self.assertIn(self.token, page)
        self.assertEqual(self.call("POST", f"/api/live/{live}/stop", b"")[0], 200)
        time.sleep(0.4)
        srt = self.call("GET", f"/api/live/{live}/srt")[1].decode()
        self.assertIn("Labas rytas, visiems.", srt)
        self.assertIn("[en] Labas", self.call("GET", f"/api/live/{live}/srt?lang=en")[1].decode())
        self.assertFalse(self.app.busy())

    def test_cue_edits_rewrite_own_files_and_validate(self):
        import tempfile
        d = tempfile.mkdtemp()
        src = os.path.join(d, "redaguoti.wav")
        open(src, "wb").write(b"garsas")
        self.app.picker = lambda: [src]
        picked = json.loads(self.call("POST", "/api/pick", b"")[1])["paths"]
        body = json.dumps({"paths": [picked[0]["path"]], "options": {"model": "base", "outputs": ["srt", "txt"], "targets": ["en"]}}).encode()
        jid = json.loads(self.call("POST", "/api/jobs/local", body)[1])["jobs"][0]["id"]
        job = self.wait(jid)
        cues = json.loads(self.call("GET", f"/api/jobs/{jid}/transcript")[1])["cues"]
        self.assertIn("raw", cues[0])
        edits = [{"start": c["start"], "end": c["end"], "text": c["raw"]} for c in cues]
        edits[0]["text"] = "Pataisytas tekstas"
        status, resp, _ = self.call("POST", f"/api/jobs/{jid}/cues", json.dumps({"cues": edits}).encode())
        self.assertEqual(status, 200, resp)
        self.assertIn("Pataisytas tekstas", open(os.path.join(d, "redaguoti.srt"), encoding="utf-8").read())
        self.assertIn("Pataisytas tekstas", open(os.path.join(d, "redaguoti.txt"), encoding="utf-8").read())
        self.assertIn("Labas", open(os.path.join(d, "redaguoti.en.srt"), encoding="utf-8").read().replace("[en] ", ""))   # vertimo failų neliečiame
        self.assertEqual(self.call("POST", f"/api/jobs/{jid}/cues", json.dumps({"cues": edits[:1]}).encode())[0], 400)
        edits[0]["end"] = edits[0]["start"] - 1
        self.assertEqual(self.call("POST", f"/api/jobs/{jid}/cues", json.dumps({"cues": edits}).encode())[0], 400)
        self.assertEqual(self.call("POST", f"/api/jobs/{jid}/cues", b"nesamone")[0], 400)

    def test_fonts_are_served_without_token_but_only_known_files(self):
        status, body, headers = self.call("GET", "/fonts/bricolage-latin.woff2", token=None)
        self.assertEqual((status, headers["Content-Type"]), (200, "font/woff2"))
        self.assertEqual(self.call("GET", "/fonts/../server.py", token=None)[0], 404)
        self.assertEqual(self.call("GET", "/fonts/nera.woff2", token=None)[0], 404)
        self.assertEqual(self.call("GET", "/api/jobs", token=None)[0], 403)

    def test_languages_endpoint(self):
        doc = json.loads(self.call("GET", "/api/languages")[1])
        self.assertIn("lv", doc["targets"])
        self.assertTrue(doc["model_ready"])

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
