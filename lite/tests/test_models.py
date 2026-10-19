import os
import tempfile
import unittest

from srtforge_lite import models
from srtforge_lite.engine import MODELS


def make_model(root, repo, snapshot="abc123", complete=True, size=2_000_000):
    path = os.path.join(root, "models--" + repo.replace("/", "--"), "snapshots", snapshot)
    os.makedirs(path)
    for name in ("config.json", "tokenizer.json", "vocabulary.json"):
        open(os.path.join(path, name), "w").write("{}")
    with open(os.path.join(path, "model.bin"), "wb") as handle:
        handle.write(b"\0" * (size if complete else 10))
    return path


class Discovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hub = self.tmp.name
        self.saved = {k: os.environ.get(k) for k in ("HF_HOME", "HUGGINGFACE_HUB_CACHE", "SRTFORGE_MODEL_DIR", "SRTFORGE_EXTRA_MODEL_DIRS", "HOME")}
        for key in ("HF_HOME", "HUGGINGFACE_HUB_CACHE", "SRTFORGE_MODEL_DIR", "SRTFORGE_EXTRA_MODEL_DIRS"):
            os.environ.pop(key, None)
        os.environ["HOME"] = self.tmp.name            # tikra namų talpykla netrukdo testui
        os.environ["HUGGINGFACE_HUB_CACHE"] = self.hub

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_finds_complete_models_and_ignores_partial_downloads(self):
        make_model(self.hub, "Systran/faster-whisper-small")
        make_model(self.hub, "Systran/faster-whisper-base", complete=False)       # nutrauktas atsisiuntimas
        found = models.find_models()
        self.assertEqual(sorted(found), ["small"])
        self.assertEqual(found["small"]["size"], 2_000_000)

    def test_missing_vocabulary_or_tokenizer_means_incomplete(self):
        path = make_model(self.hub, "Systran/faster-whisper-medium")
        os.remove(os.path.join(path, "tokenizer.json"))
        self.assertEqual(models.find_models(), {})

    def test_turbo_variants_and_extra_dirs(self):
        make_model(self.hub, "mobiuslabsgmbh/faster-whisper-large-v3-turbo")
        with tempfile.TemporaryDirectory() as extra:
            make_model(extra, "Systran/faster-whisper-tiny")
            found = models.find_models([extra])
            self.assertEqual(sorted(found), ["large-v3-turbo", "tiny"])

    def test_report_prefers_installed_model_that_fits_ram(self):
        make_model(self.hub, "Systran/faster-whisper-small")
        make_model(self.hub, "Systran/faster-whisper-large-v3")
        report = models.system_report(MODELS, ram_gb=4, recommended="small")
        self.assertEqual(report["recommended"], "small")           # large-v3 turimas, bet netelpa į 4 GB
        self.assertTrue(report["recommended_installed"])
        by_name = {m["name"]: m for m in report["models"]}
        self.assertTrue(by_name["large-v3"]["installed"] and not by_name["large-v3"]["fits_ram"])
        self.assertFalse(by_name["base"]["installed"])

    def test_report_with_nothing_installed_falls_back_to_ram_recommendation(self):
        report = models.system_report(MODELS, ram_gb=16, recommended="large-v3")
        self.assertEqual(report["recommended"], "large-v3")
        self.assertFalse(report["recommended_installed"])
        self.assertGreater(report["disk_free_gb"], 0)

    def test_detects_whisper_cpp_models_but_marks_them_unusable(self):
        ggml = os.path.join(self.tmp.name, "Library", "Application Support", "SRT Forge", "Models")
        os.makedirs(ggml)
        with open(os.path.join(ggml, "ggml-large-v3.bin"), "wb") as handle:
            handle.write(b"\0" * 2_000_000)
        other = models.find_other_engines()
        self.assertEqual([o["format"] for o in other], ["whisper.cpp (ggml)"])
        self.assertEqual(models.find_models(), {})                 # nesumaišoma su naudojamais modeliais

    def test_load_model_uses_local_path_instead_of_downloading(self):
        import sys
        import types
        from srtforge_lite import engine

        path = make_model(self.hub, "Systran/faster-whisper-small")
        calls = []
        fake = types.ModuleType("faster_whisper")
        fake.WhisperModel = lambda target, **kw: calls.append((target, kw)) or object()
        sys.modules["faster_whisper"] = fake
        try:
            engine._MODEL_CACHE.clear()
            engine.load_model("small")
            engine._MODEL_CACHE.clear()
            engine.load_model("base")
        finally:
            del sys.modules["faster_whisper"]
            engine._MODEL_CACHE.clear()
        self.assertEqual(calls[0][0], path)                         # turimas -> vietinis kelias, jokio download_root
        self.assertNotIn("download_root", calls[0][1])
        self.assertEqual(calls[1][0], "base")                       # neturimas -> atsisiuntimas pagal vardą


if __name__ == "__main__":
    unittest.main()
