"""Vertimas: saugumas (visada) ir tikras M2M100 (jei modelis yra SRTFORGE_TRANSLATE_DIR)."""
import hashlib
import os
import tempfile
import unittest

from srtforge_lite import translate as tr
from srtforge_lite.core import Cue

DIR = os.environ.get("SRTFORGE_TRANSLATE_DIR")


class DownloadSafety(unittest.TestCase):
    def test_hash_mismatch_rejected_and_partial_removed(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            open(os.path.join(src, "a.bin"), "wb").write(b"turinys")
            with self.assertRaisesRegex(tr.TranslationError, "sha256 nesutampa"):
                tr.ensure_model(dst, base_url="file://" + src + "/", files={"a.bin": "0" * 64}, allow_local=True)
            self.assertEqual(os.listdir(dst), [])

    def test_valid_files_saved_and_not_downloaded_twice(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            data = os.urandom(2048)
            open(os.path.join(src, "a.bin"), "wb").write(data)
            files = {"a.bin": hashlib.sha256(data).hexdigest()}
            tr.ensure_model(dst, base_url="file://" + src + "/", files=files, allow_local=True)
            self.assertEqual(open(os.path.join(dst, "a.bin"), "rb").read(), data)
            os.remove(os.path.join(src, "a.bin"))                     # antras kvietimas neturi bandyti siųstis
            tr.ensure_model(dst, base_url="file://" + src + "/", files=files, allow_local=True)

    def test_http_is_refused(self):
        with self.assertRaisesRegex(tr.TranslationError, "https"):
            tr.ensure_model("/tmp/x-no", base_url="http://evil.example/")

    def test_pinned_files_have_valid_hashes_and_https(self):
        self.assertTrue(tr.BASE_URL.startswith("https://"))
        self.assertEqual(set(tr.FILES), {"model.bin", "config.json", "shared_vocabulary.json", "sentencepiece.bpe.model"})
        self.assertTrue(all(len(h) == 64 for h in tr.FILES.values()))

    def test_translate_cues_keeps_timing_speakers_and_rewraps(self):
        class T:
            def translate(self, texts, s, t):
                return ["Hello, today we will talk about the prices of food in the shops of the whole country"] * len(texts)

        cues = [Cue(1.0, 4.0, "Sveiki", speaker=1)]
        out = tr.translate_cues(cues, T(), "lt", "en")
        self.assertEqual((out[0].start, out[0].end, out[0].speaker), (1.0, 4.0, 1))
        self.assertIn("\n", out[0].text)                                  # >42 simb. -> dvi eilutės
        self.assertEqual(cues[0].text, "Sveiki")                           # originalas nepaliestas


def have():
    return bool(DIR) and tr.model_ready(DIR)


@unittest.skipUnless(have(), "nėra vertimo modelio (SRTFORGE_TRANSLATE_DIR)")
class RealTranslation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t = tr.Translator(DIR)

    def test_lithuanian_to_english_is_sensible(self):
        out = self.t.translate(["Pieno kaina per metus pakilo dešimčia procentų."], "lt", "en")[0].lower()
        self.assertTrue("milk" in out and "price" in out, out)

    def test_several_targets_give_different_languages(self):
        text = ["Sveiki, šiandien kalbėsime apie maisto kainas."]
        outs = {l: self.t.translate(text, "lt", l)[0] for l in ("en", "de", "pl", "ru")}
        self.assertEqual(len(set(outs.values())), 4, outs)
        self.assertRegex(outs["ru"], "[А-Яа-я]")

    def test_latvian_does_not_loop(self):
        out = self.t.translate(["Seimas vakar nubalsavo už naują biudžeto projektą."], "lt", "lv")[0]
        words = out.lower().split()
        self.assertLessEqual(max(words.count(w) for w in set(words)), 3, out)

    def test_same_language_and_empty_text(self):
        self.assertEqual(self.t.translate(["Labas"], "lt", "lt"), ["Labas"])
        self.assertEqual(self.t.translate([""], "lt", "en"), [""])
        with self.assertRaises(tr.TranslationError):
            self.t.translate(["x"], "lt", "xx")


if __name__ == "__main__":
    unittest.main()
