"""Tikra diarizacija (sherpa-onnx + ONNX modeliai). Praleidžiama, jei nėra sherpa_onnx arba modelių
(SRTFORGE_DIAR_DIR arba duomenų katalogas); CI dūmų testas modelius atsisiunčia pats."""
import os
import unittest

try:
    import sherpa_onnx  # noqa: F401
    HAVE = True
except Exception:  # noqa: BLE001
    HAVE = False

from srtforge_lite import diarize as dz

HERE = os.path.dirname(os.path.abspath(__file__))
CLIP = os.path.join(HERE, "data", "two_speakers.wav")
DIR = os.environ.get("SRTFORGE_DIAR_DIR")
MODEL_DIR = os.environ.get("SRTFORGE_MODEL_DIR") or os.path.join(os.path.dirname(os.path.dirname(HERE)), ".venv-whisperx", "models")


def have_models():
    return bool(DIR) and all(os.path.isfile(os.path.join(DIR, n)) for n in (dz.SEGMENTATION_NAME, dz.EMBEDDING_NAME))


@unittest.skipUnless(HAVE and have_models(), "nėra sherpa_onnx arba diarizacijos modelių (SRTFORGE_DIAR_DIR)")
class RealDiarization(unittest.TestCase):
    # two_speakers.wav sukonstruotas: A 0–7,3 | B 8,1–14,4 | A 15,2–22,1 | B 22,9–27,5 s
    TRUTH = [("A", 0.0, 7.3), ("B", 8.1, 14.4), ("A", 15.2, 22.1), ("B", 22.9, 27.5)]

    def test_two_voices_alternate_and_match_ground_truth(self):
        turns = dz.diarize(CLIP, num_speakers=2, directory=DIR)
        self.assertEqual({t.speaker for t in turns}, {0, 1})

        def speaker_at(second):
            hits = [t for t in turns if t.start <= second <= t.end]
            return hits[0].speaker if hits else None

        labels = {}
        for who, start, end in self.TRUTH:
            mid = speaker_at((start + end) / 2)
            self.assertIsNotNone(mid, f"{who} viduryje nerasta kalbos")
            labels.setdefault(who, set()).add(mid)
        self.assertEqual(len(labels["A"]), 1, labels)
        self.assertEqual(len(labels["B"]), 1, labels)
        self.assertNotEqual(labels["A"], labels["B"], "abu balsai gavo tą patį kalbėtoją")

    def test_auto_count_finds_two(self):
        turns = dz.diarize(CLIP, num_speakers=None, directory=DIR)
        self.assertEqual(len({t.speaker for t in turns}), 2)

    def test_wrong_format_wav_fails_clearly(self):
        import tempfile
        import wave
        with tempfile.NamedTemporaryFile(suffix=".wav") as handle:
            with wave.open(handle.name, "wb") as w:
                w.setnchannels(2); w.setsampwidth(2); w.setframerate(44100); w.writeframes(b"\0" * 4000)
            with self.assertRaisesRegex(dz.DiarizationError, "16 kHz mono"):
                dz.diarize(handle.name, directory=DIR)


class ModelDownloadSafety(unittest.TestCase):
    def test_hash_mismatch_is_rejected_and_nothing_is_left(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "evil.onnx")
            open(src, "wb").write(b"ne modelis")
            target = os.path.join(tmp, "out.onnx")
            with self.assertRaisesRegex(dz.DiarizationError, "sha256 nesutampa"):
                dz._download("file://" + src, target, "0" * 64, allow_local=True)
            self.assertEqual(sorted(os.listdir(tmp)), ["evil.onnx"])

    def test_http_url_is_refused(self):
        with self.assertRaisesRegex(dz.DiarizationError, "https"):
            dz._download("http://evil.example/m.onnx", "/tmp/x", "0" * 64)

    def test_valid_download_is_saved_atomically(self):
        import hashlib, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "m.onnx")
            data = os.urandom(4096)
            open(src, "wb").write(data)
            target = os.path.join(tmp, "saved.onnx")
            dz._download("file://" + src, target, hashlib.sha256(data).hexdigest(), allow_local=True)
            self.assertEqual(open(target, "rb").read(), data)


@unittest.skipUnless(HAVE and have_models() and os.path.isdir(MODEL_DIR), "nėra modelių / Whisper talpyklos")
class RealEndToEnd(unittest.TestCase):
    def test_transcribe_with_speakers_labels_both_voices_in_order(self):
        from srtforge_lite import engine, models

        original = dz.models_dir
        dz.models_dir = lambda base=None: DIR          # naudoti jau turimus modelius
        try:
            cues, _lang, _d = engine.transcribe(CLIP, model="base", language="lt", model_dir=MODEL_DIR, speakers="2")
        finally:
            dz.models_dir = original
        speakers = [c.speaker for c in cues]
        self.assertEqual({s for s in speakers if s is not None}, {0, 1}, speakers)
        # A kalba pirma ir trečia, B antra ir ketvirta: pirmo ir paskutinio subtitrų kalbėtojai skiriasi
        self.assertNotEqual(cues[0].speaker, cues[-1].speaker)
        changes = sum(1 for a, b in zip(speakers, speakers[1:]) if a != b)
        self.assertGreaterEqual(changes, 3, speakers)


if __name__ == "__main__":
    unittest.main()
