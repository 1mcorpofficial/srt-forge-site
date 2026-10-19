"""Tikras ffmpeg + faster-whisper. Praleidžiama, jei nėra faster_whisper arba modelio kataloge nėra `base`."""
import os
import unittest

try:
    import faster_whisper  # noqa: F401
    HAVE = True
except Exception:  # noqa: BLE001
    HAVE = False

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODEL_DIR = os.environ.get("SRTFORGE_MODEL_DIR") or os.path.join(ROOT, ".venv-whisperx", "models")
CLIP = os.path.join(ROOT, "Tests", "SRTForgeCoreTests", "Fixtures", "speech_a.wav")


@unittest.skipUnless(HAVE and os.path.isdir(MODEL_DIR) and os.path.exists(CLIP), "nėra faster_whisper/modelio/failo")
class RealEngine(unittest.TestCase):
    def test_transcribe_wav_gives_ordered_nonoverlapping_cues_matching_silencedetect(self):
        from srtforge_lite.engine import transcribe

        stages = []
        cues, language, duration = transcribe(CLIP, model="base", language="lt", model_dir=MODEL_DIR,
                                              progress=lambda s, v: stages.append(v))
        self.assertEqual(language, "lt")
        self.assertAlmostEqual(duration, 4.1, delta=0.1)
        self.assertTrue(cues)
        # ffmpeg silencedetect: kalba 0–3,71 s
        self.assertLess(cues[0].start, 0.5)
        self.assertLess(cues[-1].end, 4.0 + 1.0)
        for a, b in zip(cues, cues[1:]):
            self.assertLessEqual(a.end, b.start + 1e-9)
        self.assertEqual(stages, sorted(stages), "progresas turi tik augti")
        self.assertGreaterEqual(stages[-1], 0.95)

    def test_video_container_input_and_cancel(self):
        import subprocess
        import tempfile
        from srtforge_lite.engine import ffmpeg_path, transcribe

        with tempfile.TemporaryDirectory() as tmp:
            mp4 = os.path.join(tmp, "v.mp4")
            subprocess.run([ffmpeg_path(), "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=4.1", "-i", CLIP,
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", mp4], check=True)
            cues, _lang, _d = transcribe(mp4, model="base", language="lt", model_dir=MODEL_DIR)
            self.assertTrue(cues)
            with self.assertRaises(InterruptedError):
                transcribe(mp4, model="base", language="lt", model_dir=MODEL_DIR, cancelled=lambda: True)

    def test_unreadable_input_fails_with_clear_error(self):
        import tempfile
        from srtforge_lite.engine import transcribe

        with tempfile.NamedTemporaryFile(suffix=".mp3") as bad:
            bad.write(b"ne garsas")
            bad.flush()
            with self.assertRaisesRegex(RuntimeError, "ffmpeg nepavyko"):
                transcribe(bad.name, model="base", model_dir=MODEL_DIR)


if __name__ == "__main__":
    unittest.main()
